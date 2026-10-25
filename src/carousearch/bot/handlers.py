"""python-telegram-bot handlers. Thin: read the update, call the pure state/keyboard/messages
functions, call Carousell, send messages.

A chat has at most one SearchSession at a time. While one is active, /search and new keywords are
refused with a reminder to /end it, and plain text is read only when the current step asks for it.
Only the buttons on the session's current message (`SearchSession.stage_message_id`) are live.

Everything a handler needs comes from the `BotServices` in `Application.bot_data`, and every
outgoing Telegram call goes through `context.bot`, so tests can pass a fake bot and a fake client.
"""

import asyncio
import functools
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from carousearch import storage
from carousearch.bot import messages
from carousearch.bot.keyboard import (
    KeyboardLayout,
    build_keyboard_for_stage,
    build_listing_card_keyboard,
    build_saved_searches_keyboard,
    describe_stage_position,
    render_stage_text,
)
from carousearch.bot.session_store import SessionStore
from carousearch.bot.state import (
    RESULTS_PER_PAGE,
    Action,
    ActionKind,
    SearchSession,
    Stage,
    apply_action,
    apply_text_input,
    decode_action,
    expects_text_input,
    first_page_request,
    next_page_request,
    record_page_shown,
    start_session,
    start_session_at_summary,
)
from carousearch.carousell.client import CarousellClient
from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest, SearchResult

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"
DETAIL_CACHE_MAX_AGE = timedelta(hours=12)
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BotServices:
    """Everything the handlers need. Created once (app.create_bot_services) and kept in bot_data."""

    carousell_client: CarousellClient
    database: sqlite3.Connection
    session_store: SessionStore
    owner_user_id: int
    clock: Callable[[], datetime] = field(default=utc_now)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return context.bot_data[SERVICES_KEY]


# --- decorators applied to every registered handler -----------------------------------


def report_errors_to_user(handler: Handler) -> Handler:
    """Log any exception and tell the user briefly, so one failing update never stops the bot."""

    @functools.wraps(handler)
    async def handler_reporting_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await handler(update, context)
        except Exception as error:
            if isinstance(error, CarousellError):
                logger.warning("Carousell request failed: %s: %s", type(error).__name__, error)
            else:
                logger.exception("Handler %s failed", handler.__name__)
            await _send_error_message(update, context, error)

    return handler_reporting_errors


def owner_only(handler: Handler) -> Handler:
    """Run the handler only for the configured owner; everyone else gets a one-line refusal."""

    @functools.wraps(handler)
    async def handler_for_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id != get_services(context).owner_user_id:
            logger.info("Refused an update from user id %s", user.id if user else "unknown")
            await _send_refusal(update, context)
            return
        await handler(update, context)

    return handler_for_owner


# --- registered handlers --------------------------------------------------------------


@report_errors_to_user
@owner_only
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: the help text, or the lock reminder when a search is in progress."""
    chat_id = update.effective_chat.id
    session = get_services(context).session_store.get(chat_id)
    if session is not None:
        await send_lock_reminder(context.bot, chat_id, session)
        return
    await context.bot.send_message(chat_id=chat_id, text=messages.HELP_TEXT)


@report_errors_to_user
@owner_only
async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(chat_id=update.effective_chat.id, text=messages.HELP_TEXT)


@report_errors_to_user
@owner_only
async def handle_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keywords = " ".join(context.args or [])
    await start_session_from_keywords(context.bot, get_services(context), update.effective_chat.id, keywords)


@report_errors_to_user
@owner_only
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keywords for a new search when none is active; the answer to the current step if it asked
    for text; otherwise the lock reminder."""
    services = get_services(context)
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    session = services.session_store.get(chat_id)
    if session is None:
        await start_session_from_keywords(context.bot, services, chat_id, text)
    elif expects_text_input(session):
        await apply_typed_input(context.bot, services, chat_id, session, text)
    else:
        await send_lock_reminder(context.bot, chat_id, session)


@report_errors_to_user
@owner_only
async def handle_end_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    chat_id = update.effective_chat.id
    session = services.session_store.get(chat_id)
    services.session_store.clear(chat_id)
    if session is None:
        await context.bot.send_message(chat_id=chat_id, text=messages.NO_SESSION_TEXT)
        return
    if session.stage_message_id is not None:
        await remove_buttons(context.bot, chat_id, session.stage_message_id)
    await context.bot.send_message(chat_id=chat_id, text=messages.SESSION_ENDED_TEXT)


@report_errors_to_user
@owner_only
async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = get_services(context).session_store.get(chat_id)
    if session is None:
        await context.bot.send_message(chat_id=chat_id, text=messages.NO_SESSION_TEXT)
        return
    status = messages.render_session_status(describe_stage_position(session), session.request)
    await context.bot.send_message(chat_id=chat_id, text=status)


@report_errors_to_user
@owner_only
async def handle_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    chat_id = update.effective_chat.id
    name = " ".join(context.args or []).strip()
    session = services.session_store.get(chat_id)
    if not name:
        await context.bot.send_message(chat_id=chat_id, text=messages.SAVE_USAGE_TEXT)
        return
    if session is None or session.request is None:
        await context.bot.send_message(chat_id=chat_id, text=messages.NOTHING_TO_SAVE_TEXT)
        return
    try:
        storage.save_search(services.database, name, session.request, services.clock())
    except sqlite3.IntegrityError:
        await context.bot.send_message(chat_id=chat_id, text=f'A saved search called "{name}" already exists.')
        return
    await context.bot.send_message(chat_id=chat_id, text=f'Saved as "{name}". Use /saved to load it later.')


@report_errors_to_user
@owner_only
async def handle_saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    saved_searches = storage.list_saved_searches(get_services(context).database)
    if not saved_searches:
        await context.bot.send_message(chat_id=chat_id, text=messages.NO_SAVED_SEARCHES_TEXT)
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=messages.render_saved_searches_text(saved_searches),
        reply_markup=to_inline_keyboard(build_saved_searches_keyboard(saved_searches)),
    )


@report_errors_to_user
@owner_only
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every inline-keyboard tap: decode the action and route it."""
    bot = context.bot
    services = get_services(context)
    query = update.callback_query
    chat_id = update.effective_chat.id
    message_id = query.message.message_id
    try:
        action = decode_action(query.data or "")
    except ValueError:
        await bot.answer_callback_query(query.id, text=messages.INVALID_BUTTON_TEXT)
        return

    if action.kind == ActionKind.SHOW_DETAILS:
        await bot.answer_callback_query(query.id)
        await send_listing_details(bot, services, chat_id, action.value)
        return
    if action.kind == ActionKind.LOAD_SAVED_SEARCH:
        await bot.answer_callback_query(query.id)
        await load_saved_search(bot, services, chat_id, action.value)
        return

    session = services.session_store.get(chat_id)
    if session is None:
        expired = services.session_store.remove_if_expired(chat_id)
        await bot.answer_callback_query(
            query.id, text=messages.SESSION_EXPIRED_BUTTON_TEXT if expired else messages.NO_SESSION_TEXT
        )
        return
    if message_id != session.stage_message_id:
        await bot.answer_callback_query(query.id, text=messages.INVALID_BUTTON_TEXT)
        await remove_buttons(bot, chat_id, message_id)
        return
    if action.kind == ActionKind.RUN_SEARCH:
        await run_search(bot, services, chat_id, message_id, query.id, session)
    elif action.kind == ActionKind.NEXT_PAGE:
        await show_next_page(bot, services, chat_id, message_id, query.id, session)
    else:
        await apply_session_button(bot, services, chat_id, message_id, query.id, session, action)


async def log_unhandled_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Application-level error handler, for errors outside the handlers (polling, network)."""
    logger.error("Unhandled error while processing an update", exc_info=context.error)


# --- starting sessions and the lock -------------------------------------------------------


async def start_session_from_keywords(bot: Bot, services: BotServices, chat_id: int, keywords: str) -> None:
    """Start a session for `keywords` (asking for them when blank), unless one is already active."""
    if await refuse_when_session_active(bot, services, chat_id):
        return
    await show_current_stage(bot, services, chat_id, start_session(keywords, services.clock()))


async def show_filter_panel_for_request(bot: Bot, services: BotServices, chat_id: int, request: SearchRequest) -> None:
    """Start a session at the summary for a ready-made request, unless one is already active.

    The entry point for anything that produces a whole SearchRequest at once: a saved search now,
    and next the LLM prompt parser, which is how a plain-English prompt skips the wizard.
    """
    if await refuse_when_session_active(bot, services, chat_id):
        return
    await show_current_stage(bot, services, chat_id, start_session_at_summary(request, services.clock()))


async def refuse_when_session_active(bot: Bot, services: BotServices, chat_id: int) -> bool:
    """Send the lock reminder and return True if the chat already has an active session.

    An expired session is dropped with a notice instead, which leaves the chat free for a new one.
    """
    session = services.session_store.get(chat_id)
    if session is not None:
        await send_lock_reminder(bot, chat_id, session)
        return True
    if services.session_store.remove_if_expired(chat_id):
        await bot.send_message(chat_id=chat_id, text=messages.SESSION_EXPIRED_TEXT)
    return False


async def send_lock_reminder(bot: Bot, chat_id: int, session: SearchSession) -> None:
    reminder = messages.render_lock_reminder(session.query, describe_stage_position(session))
    await bot.send_message(chat_id=chat_id, text=reminder)


# --- showing and changing the current stage ------------------------------------------------


async def show_current_stage(
    bot: Bot, services: BotServices, chat_id: int, session: SearchSession, message_id: int | None = None
) -> None:
    """Show the session's stage and store the session as the chat's active one.

    Edits message `message_id` when given (a button on it was tapped); otherwise sends a new message.
    """
    text = render_stage_text(session)
    reply_markup = to_inline_keyboard(build_keyboard_for_stage(session))
    if message_id is None:
        sent_message = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        message_id = sent_message.message_id
    else:
        await edit_message(bot, chat_id, message_id, text, reply_markup)
    services.session_store.set(chat_id, replace(session, stage_message_id=message_id))


async def apply_session_button(
    bot: Bot,
    services: BotServices,
    chat_id: int,
    message_id: int,
    query_id: str,
    session: SearchSession,
    action: Action,
) -> None:
    """A wizard, summary or results button: apply it, then redraw the same message (or end the session)."""
    try:
        updated_session = apply_action(session, action, services.clock())
    except ValueError as error:
        logger.info("Button %s rejected at stage %s: %s", action, session.stage, error)
        await bot.answer_callback_query(query_id, text=messages.INVALID_BUTTON_TEXT)
        return
    await bot.answer_callback_query(query_id)
    if updated_session is None:
        services.session_store.clear(chat_id)
        await edit_message(bot, chat_id, message_id, messages.SESSION_ENDED_TEXT, reply_markup=None)
        return
    await show_current_stage(bot, services, chat_id, updated_session, message_id)


async def apply_typed_input(bot: Bot, services: BotServices, chat_id: int, session: SearchSession, text: str) -> None:
    """Typed keywords, price range or collection id: re-ask on a problem, otherwise show the next stage."""
    updated_session, problem = apply_text_input(session, text, services.clock())
    if problem is not None:
        services.session_store.set(chat_id, updated_session)
        await bot.send_message(chat_id=chat_id, text=problem)
        return
    # The typed answer sits below the step's message, so the next stage goes in a new message under it.
    if session.stage_message_id is not None:
        await remove_buttons(bot, chat_id, session.stage_message_id)
    await show_current_stage(bot, services, chat_id, updated_session)


# --- results ----------------------------------------------------------------------------


async def run_search(
    bot: Bot, services: BotServices, chat_id: int, summary_message_id: int, query_id: str, session: SearchSession
) -> None:
    if session.stage != Stage.SUMMARY:
        await bot.answer_callback_query(query_id, text=messages.INVALID_BUTTON_TEXT)
        return
    await bot.answer_callback_query(query_id, text="Searching…")
    await send_results_page(bot, services, chat_id, session, first_page_request(session))
    # Only once the search worked: if it fails, the summary keeps its Search button for a retry.
    await remove_buttons(bot, chat_id, summary_message_id)


async def show_next_page(
    bot: Bot, services: BotServices, chat_id: int, controls_message_id: int, query_id: str, session: SearchSession
) -> None:
    if session.stage != Stage.RESULTS:
        await bot.answer_callback_query(query_id, text=messages.INVALID_BUTTON_TEXT)
        return
    request = next_page_request(session)
    if request is None:
        await bot.answer_callback_query(query_id, text=messages.NO_MORE_RESULTS_TEXT)
        return
    await bot.answer_callback_query(query_id, text="Loading…")
    await send_results_page(bot, services, chat_id, session, request)
    # The new page has its own controls; only the newest offer Next.
    await remove_buttons(bot, chat_id, controls_message_id)


async def send_results_page(
    bot: Bot, services: BotServices, chat_id: int, session: SearchSession, request: SearchRequest
) -> None:
    """Run one page of the search, send up to RESULTS_PER_PAGE cards and then the results controls."""
    result = await search_carousell(services.carousell_client, request)
    listings = result.listings[:RESULTS_PER_PAGE]
    now = services.clock()
    first_time_listing_ids = set(storage.record_seen_listings(services.database, listings, now))
    for listing in listings:
        await send_listing_card(bot, chat_id, listing, already_seen=listing.id not in first_time_listing_ids)
    updated_session = record_page_shown(session, request.page_cursor, result.next_page, now)
    controls = await bot.send_message(
        chat_id=chat_id,
        text=messages.render_results_footer(result, len(updated_session.page_cursors), len(listings)),
        reply_markup=to_inline_keyboard(build_keyboard_for_stage(updated_session)),
    )
    services.session_store.set(chat_id, replace(updated_session, stage_message_id=controls.message_id))


async def send_listing_card(bot: Bot, chat_id: int, listing: Listing, already_seen: bool) -> None:
    caption = messages.render_listing_caption(listing, already_seen)
    reply_markup = to_inline_keyboard(build_listing_card_keyboard(listing.id))
    if listing.thumbnail_url:
        try:
            await bot.send_photo(chat_id=chat_id, photo=listing.thumbnail_url, caption=caption, reply_markup=reply_markup)
            return
        except BadRequest as error:
            logger.warning("Telegram could not send photo %s: %s", listing.thumbnail_url, error)
    await bot.send_message(chat_id=chat_id, text=caption, reply_markup=reply_markup, link_preview_options=NO_LINK_PREVIEW)


async def send_listing_details(bot: Bot, services: BotServices, chat_id: int, listing_id: str) -> None:
    detail = await load_listing_detail(services, listing_id)
    await bot.send_message(
        chat_id=chat_id, text=messages.render_detail_message(detail), link_preview_options=NO_LINK_PREVIEW
    )


async def load_saved_search(bot: Bot, services: BotServices, chat_id: int, saved_search_id: str) -> None:
    saved_searches = storage.list_saved_searches(services.database)
    saved = next((search for search in saved_searches if str(search.id) == saved_search_id), None)
    if saved is None:
        await bot.send_message(chat_id=chat_id, text=messages.SAVED_SEARCH_MISSING_TEXT)
        return
    await show_filter_panel_for_request(bot, services, chat_id, saved.request)


# --- editing messages ---------------------------------------------------------------------


async def edit_message(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup | None
) -> None:
    """Redraw a message (with no buttons when `reply_markup` is None)."""
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
    except BadRequest as error:
        # Telegram refuses an edit that changes nothing, e.g. tapping "Any" when it is already selected.
        if "not modified" not in str(error).lower():
            raise


async def remove_buttons(bot: Bot, chat_id: int, message_id: int) -> None:
    """Drop a message's buttons once they are no longer live, keeping its text."""
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except BadRequest as error:
        logger.info("Could not remove buttons from message %s: %s", message_id, error)


# --- Carousell calls (the client is synchronous, so it runs in a worker thread) ------------


async def search_carousell(client: CarousellClient, request: SearchRequest) -> SearchResult:
    started = time.monotonic()
    filters = request.model_dump(mode="json", exclude_defaults=True, exclude={"page_cursor", "page_size"})
    logger.info("Carousell search %s (%s)", filters, "next page" if request.page_cursor else "first page")
    result = await asyncio.to_thread(client.search, request)
    logger.info("Carousell search returned %d listings in %.1fs", len(result.listings), time.monotonic() - started)
    return result


async def load_listing_detail(services: BotServices, listing_id: str) -> ListingDetail:
    """The cached detail if it is fresh enough, otherwise a Carousell call (then cached)."""
    now = services.clock()
    cached = storage.load_cached_listing_detail(services.database, listing_id, now, DETAIL_CACHE_MAX_AGE)
    if cached is not None:
        logger.info("Listing detail %s served from cache", listing_id)
        return cached
    logger.info("Carousell listing detail %s", listing_id)
    detail = await asyncio.to_thread(services.carousell_client.get_listing, listing_id)
    storage.cache_listing_detail(services.database, detail, now)
    return detail


# --- Telegram adapters ----------------------------------------------------------------------


def to_inline_keyboard(layout: KeyboardLayout) -> InlineKeyboardMarkup:
    """The one place a pure keyboard layout becomes python-telegram-bot markup."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button.text, callback_data=button.callback_data) for button in row] for row in layout]
    )


async def _send_refusal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is not None:
        await context.bot.answer_callback_query(update.callback_query.id, text=messages.REFUSAL_TEXT)
    elif update.effective_chat is not None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=messages.REFUSAL_TEXT)


async def _send_error_message(update: Update, context: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
    if update.effective_chat is None:
        return
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=messages.describe_error_for_user(error))
    except Exception:
        logger.exception("Could not send the error message to chat %s", update.effective_chat.id)
