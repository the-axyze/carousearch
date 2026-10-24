"""python-telegram-bot handlers. Thin: read the update, call the pure state/keyboard/messages
functions, call Carousell, send messages.

Everything a handler needs comes from the `BotServices` in `Application.bot_data`, and every
outgoing Telegram call goes through `context.bot`, so tests can pass a fake bot and a fake client.
"""

import asyncio
import functools
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from carousearch import storage
from carousearch.bot import messages
from carousearch.bot.draft_store import DraftStore
from carousearch.bot.keyboard import (
    KeyboardLayout,
    build_filter_keyboard,
    build_listing_card_keyboard,
    build_results_keyboard,
    build_saved_searches_keyboard,
    render_filter_summary,
)
from carousearch.bot.state import (
    FILTER_ACTION_KINDS,
    RESULTS_PER_PAGE,
    Action,
    ActionKind,
    AwaitingInput,
    ChatDraft,
    apply_action,
    apply_text_input,
    decode_action,
    first_page_request,
    next_page_request,
    record_page_shown,
    start_draft,
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
    draft_store: DraftStore
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
async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(chat_id=update.effective_chat.id, text=messages.HELP_TEXT)


@report_errors_to_user
@owner_only
async def handle_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keywords = " ".join(context.args or [])
    await start_search_from_keywords(context.bot, get_services(context), update.effective_chat.id, keywords)


@report_errors_to_user
@owner_only
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Typed price or category when the panel asked for one; otherwise keywords for a new search."""
    services = get_services(context)
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    draft = services.draft_store.get(chat_id)
    if draft is not None and draft.awaiting_input is not None:
        await apply_typed_filter(context.bot, services, chat_id, draft, text)
    else:
        await start_search_from_keywords(context.bot, services, chat_id, text)


@report_errors_to_user
@owner_only
async def handle_save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    chat_id = update.effective_chat.id
    name = " ".join(context.args or []).strip()
    draft = services.draft_store.get(chat_id)
    if not name:
        await context.bot.send_message(chat_id=chat_id, text=messages.SAVE_USAGE_TEXT)
        return
    if draft is None:
        await context.bot.send_message(chat_id=chat_id, text=messages.NO_DRAFT_TEXT)
        return
    try:
        storage.save_search(services.database, name, draft.request, services.clock())
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

    draft = services.draft_store.get(chat_id)
    if draft is None:
        await bot.answer_callback_query(query.id, text=messages.NO_DRAFT_TEXT)
        return
    if action.kind in FILTER_ACTION_KINDS:
        await apply_filter_button(bot, services, chat_id, message_id, query.id, draft, action)
    elif action.kind == ActionKind.RUN_SEARCH:
        await bot.answer_callback_query(query.id, text="Searching…")
        await send_results_page(bot, services, chat_id, draft, first_page_request(draft))
    elif action.kind == ActionKind.NEXT_PAGE:
        await show_next_page(bot, services, chat_id, message_id, query.id, draft)
    elif action.kind == ActionKind.BACK_TO_FILTERS:
        await bot.answer_callback_query(query.id)
        await edit_filter_panel(bot, chat_id, message_id, draft.request)


async def log_unhandled_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Application-level error handler, for errors outside the handlers (polling, network)."""
    logger.error("Unhandled error while processing an update", exc_info=context.error)


# --- the filter panel ---------------------------------------------------------------------


async def show_filter_panel_for_request(bot: Bot, services: BotServices, chat_id: int, request: SearchRequest) -> None:
    """Make `request` this chat's active draft and send its filter panel.

    The single entry point for anything that produces a SearchRequest: typed keywords, a saved
    search, and (next) the LLM prompt parser.
    """
    draft = start_draft(request, services.draft_store.get(chat_id))
    services.draft_store.set(chat_id, draft)
    await send_filter_panel(bot, chat_id, draft.request)


async def start_search_from_keywords(bot: Bot, services: BotServices, chat_id: int, keywords: str) -> None:
    if not keywords.strip():
        await bot.send_message(chat_id=chat_id, text=messages.SEARCH_USAGE_TEXT)
        return
    await show_filter_panel_for_request(bot, services, chat_id, SearchRequest(query=keywords))


async def send_filter_panel(bot: Bot, chat_id: int, request: SearchRequest) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=render_filter_summary(request),
        reply_markup=to_inline_keyboard(build_filter_keyboard(request)),
    )


async def edit_filter_panel(bot: Bot, chat_id: int, message_id: int, request: SearchRequest) -> None:
    """Redraw an existing message as the filter panel for `request`."""
    try:
        await bot.edit_message_text(
            text=render_filter_summary(request),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=to_inline_keyboard(build_filter_keyboard(request)),
        )
    except BadRequest as error:
        # Telegram refuses an edit that changes nothing, e.g. tapping the price already selected.
        if "not modified" not in str(error).lower():
            raise


async def apply_filter_button(
    bot: Bot, services: BotServices, chat_id: int, message_id: int, query_id: str, draft: ChatDraft, action: Action
) -> None:
    try:
        updated_draft = apply_action(draft, action)
    except ValueError as error:
        logger.info("Filter action %s rejected: %s", action, error)
        await bot.answer_callback_query(query_id, text=messages.CHANGE_NOT_APPLIED_TEXT)
        return
    services.draft_store.set(chat_id, updated_draft)
    await bot.answer_callback_query(query_id)
    await edit_filter_panel(bot, chat_id, message_id, updated_draft.request)
    if updated_draft.awaiting_input == AwaitingInput.PRICE_RANGE:
        await bot.send_message(chat_id=chat_id, text=messages.PRICE_PROMPT_TEXT)
    elif updated_draft.awaiting_input == AwaitingInput.CATEGORY_ID:
        await bot.send_message(chat_id=chat_id, text=messages.CATEGORY_PROMPT_TEXT)


async def apply_typed_filter(bot: Bot, services: BotServices, chat_id: int, draft: ChatDraft, text: str) -> None:
    try:
        updated_draft = apply_text_input(draft, text)
    except ValueError as error:
        await bot.send_message(chat_id=chat_id, text=messages.render_input_problem(error))
        return
    services.draft_store.set(chat_id, updated_draft)
    await send_filter_panel(bot, chat_id, updated_draft.request)


# --- results ----------------------------------------------------------------------------


async def send_results_page(
    bot: Bot, services: BotServices, chat_id: int, draft: ChatDraft, request: SearchRequest
) -> None:
    """Run one page of the search, then send up to RESULTS_PER_PAGE cards and a paging message."""
    result = await search_carousell(services.carousell_client, request)
    listings = result.listings[:RESULTS_PER_PAGE]
    storage.record_seen_listings(services.database, listings, services.clock())
    updated_draft = record_page_shown(
        draft, request.page_cursor, [listing.id for listing in listings], result.next_page
    )
    services.draft_store.set(chat_id, updated_draft)
    for listing in listings:
        await send_listing_card(bot, chat_id, listing, already_seen=listing.id in draft.shown_listing_ids)
    await bot.send_message(
        chat_id=chat_id,
        text=messages.render_results_footer(result, len(updated_draft.page_cursors), len(listings)),
        reply_markup=to_inline_keyboard(build_results_keyboard(has_next_page=result.next_page is not None)),
    )


async def show_next_page(
    bot: Bot, services: BotServices, chat_id: int, footer_message_id: int, query_id: str, draft: ChatDraft
) -> None:
    request = next_page_request(draft)
    if request is None:
        await bot.answer_callback_query(query_id, text=messages.NO_MORE_RESULTS_TEXT)
        return
    await bot.answer_callback_query(query_id, text="Loading…")
    await remove_buttons(bot, chat_id, footer_message_id)
    await send_results_page(bot, services, chat_id, draft, request)


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


async def remove_buttons(bot: Bot, chat_id: int, message_id: int) -> None:
    """Drop the old paging buttons so only the newest page offers "Next"."""
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except BadRequest as error:
        logger.info("Could not remove buttons from message %s: %s", message_id, error)


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
