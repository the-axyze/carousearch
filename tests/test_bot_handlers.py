"""Handler tests with a fake Telegram bot and the fixture-backed FakeCarousell. No network."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from telegram import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from carousearch import storage
from carousearch.bot import handlers, messages
from carousearch.bot.app import build_application, describe_senders
from carousearch.bot.draft_store import DraftStore
from carousearch.bot.handlers import SERVICES_KEY, BotServices
from carousearch.config import TelegramSettings
from carousearch.filters.schema import Condition

from fake_carousell import FakeCarousell

OWNER_ID = 1001
STRANGER_ID = 2002
CHAT_ID = 5005
PANEL_MESSAGE_ID = 77


class FakeBot:
    """Records every Bot API call the handlers make. `failures` maps a method name to an error to raise."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures = failures or {}
        self._next_message_id = 1000

    def calls_to(self, method_name: str) -> list[dict[str, Any]]:
        return [arguments for name, arguments in self.calls if name == method_name]

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def _record(self, method_name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((method_name, arguments))
        if method_name in self.failures:
            raise self.failures[method_name]
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)

    async def send_message(self, **arguments: Any) -> SimpleNamespace:
        return await self._record("send_message", arguments)

    async def send_photo(self, **arguments: Any) -> SimpleNamespace:
        return await self._record("send_photo", arguments)

    async def edit_message_text(self, **arguments: Any) -> SimpleNamespace:
        return await self._record("edit_message_text", arguments)

    async def edit_message_reply_markup(self, **arguments: Any) -> SimpleNamespace:
        return await self._record("edit_message_reply_markup", arguments)

    async def answer_callback_query(self, callback_query_id: str, **arguments: Any) -> SimpleNamespace:
        return await self._record("answer_callback_query", {"callback_query_id": callback_query_id, **arguments})


class ExplodingClient:
    def search(self, request: object) -> None:
        raise RuntimeError("unexpected bug")


@pytest.fixture
def carousell() -> FakeCarousell:
    return FakeCarousell()


@pytest.fixture
def services(tmp_path: Path, carousell: FakeCarousell) -> BotServices:
    return BotServices(
        carousell_client=carousell.client(),
        database=storage.open_database(tmp_path / "bot.db"),
        draft_store=DraftStore(),
        owner_user_id=OWNER_ID,
    )


def make_context(bot: FakeBot, services: BotServices, args: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(bot=bot, bot_data={SERVICES_KEY: services}, args=args or [])


def make_user(user_id: int) -> User:
    return User(id=user_id, first_name="Test", is_bot=False)


def make_message(message_id: int, text: str | None = None, user_id: int = OWNER_ID) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type=Chat.PRIVATE),
        from_user=make_user(user_id),
        text=text,
    )


def text_update(text: str, user_id: int = OWNER_ID) -> Update:
    return Update(update_id=1, message=make_message(10, text, user_id))


def button_update(callback_data: str, message_id: int = PANEL_MESSAGE_ID, user_id: int = OWNER_ID) -> Update:
    query = CallbackQuery(
        id="query-1",
        from_user=make_user(user_id),
        chat_instance="instance",
        data=callback_data,
        message=make_message(message_id),
    )
    return Update(update_id=2, callback_query=query)


def send_text(bot: FakeBot, services: BotServices, text: str, user_id: int = OWNER_ID) -> None:
    asyncio.run(handlers.handle_text_message(text_update(text, user_id), make_context(bot, services)))


def tap(bot: FakeBot, services: BotServices, callback_data: str, message_id: int = PANEL_MESSAGE_ID) -> None:
    asyncio.run(handlers.handle_button(button_update(callback_data, message_id), make_context(bot, services)))


def run_command(handler: handlers.Handler, bot: FakeBot, services: BotServices, args: list[str]) -> None:
    asyncio.run(handler(text_update("/command"), make_context(bot, services, args)))


def button_texts(reply_markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in reply_markup.inline_keyboard for button in row]


def search_bodies(carousell: FakeCarousell) -> list[dict]:
    return [json.loads(request.content) for request in carousell.api_requests if request.url.path.startswith("/ds/search")]


# --- owner check -------------------------------------------------------------------------


def test_stranger_is_refused_and_ignored(services: BotServices) -> None:
    bot = FakeBot()

    send_text(bot, services, "iphone 15", user_id=STRANGER_ID)

    assert bot.calls == [("send_message", {"chat_id": CHAT_ID, "text": messages.REFUSAL_TEXT})]
    assert services.draft_store.get(CHAT_ID) is None


def test_stranger_button_tap_is_refused(services: BotServices) -> None:
    bot = FakeBot()

    asyncio.run(handlers.handle_button(button_update("go:", user_id=STRANGER_ID), make_context(bot, services)))

    assert bot.calls_to("answer_callback_query") == [{"callback_query_id": "query-1", "text": messages.REFUSAL_TEXT}]
    assert bot.method_names() == ["answer_callback_query"]


# --- filter panel -------------------------------------------------------------------------


def test_text_message_creates_a_draft_and_shows_the_panel(services: BotServices) -> None:
    bot = FakeBot()

    send_text(bot, services, "iphone 15")

    assert services.draft_store.get(CHAT_ID).request.query == "iphone 15"
    [panel] = bot.calls_to("send_message")
    assert panel["text"].startswith("Search: iphone 15\n")
    assert "Search" in button_texts(panel["reply_markup"])


def test_search_command_without_keywords_explains_usage(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_search_command, bot, services, [])

    assert bot.calls_to("send_message")[0]["text"] == messages.SEARCH_USAGE_TEXT
    assert services.draft_store.get(CHAT_ID) is None


def test_condition_toggle_edits_the_panel_in_place(services: BotServices) -> None:
    bot = FakeBot()
    run_command(handlers.handle_search_command, bot, services, ["iphone", "15"])
    bot.calls.clear()

    tap(bot, services, "co:like_new")

    assert services.draft_store.get(CHAT_ID).request.conditions == [Condition.LIKE_NEW]
    assert bot.method_names() == ["answer_callback_query", "edit_message_text"]
    [edit] = bot.calls_to("edit_message_text")
    assert (edit["chat_id"], edit["message_id"]) == (CHAT_ID, PANEL_MESSAGE_ID)
    assert "Condition: Like new" in edit["text"]
    assert "✓ Like new" in button_texts(edit["reply_markup"])


def test_unchanged_panel_edit_is_not_an_error(services: BotServices) -> None:
    bot = FakeBot(failures={"edit_message_text": BadRequest("Message is not modified: specified new message content")})
    send_text(bot, services, "sofa")

    tap(bot, services, "pr:")  # "Any price" is already selected

    assert bot.calls_to("send_message")[-1]["text"].startswith("Search: sofa")  # no error message sent


def test_custom_price_is_asked_for_then_typed(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")

    tap(bot, services, "pa:")
    assert bot.calls_to("send_message")[-1]["text"] == messages.PRICE_PROMPT_TEXT

    send_text(bot, services, "cheap")
    assert "Try again" in bot.calls_to("send_message")[-1]["text"]

    send_text(bot, services, "150-450")
    request = services.draft_store.get(CHAT_ID).request
    assert (request.query, request.price_min, request.price_max) == ("sofa", 150, 450)
    assert "Price: S$150 to S$450" in bot.calls_to("send_message")[-1]["text"]


def test_stale_button_without_a_draft_is_answered(services: BotServices) -> None:
    bot = FakeBot()

    tap(bot, services, "co:like_new")

    assert bot.calls == [("answer_callback_query", {"callback_query_id": "query-1", "text": messages.NO_DRAFT_TEXT})]


# --- search and paging ------------------------------------------------------------------


def test_search_sends_five_cards_and_a_paging_message(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()
    send_text(bot, services, "iphone 15")
    bot.calls.clear()

    tap(bot, services, "go:")

    cards = bot.calls_to("send_photo")
    assert len(cards) == 5
    assert cards[0]["photo"].startswith("https://media.karousell.com/")
    assert "https://www.carousell.sg/p/" in cards[0]["caption"]
    assert messages.SEEN_TAG not in cards[0]["caption"]
    assert button_texts(cards[0]["reply_markup"]) == ["Details"]
    [footer] = bot.calls_to("send_message")
    assert footer["text"].startswith("Page 1:")
    assert button_texts(footer["reply_markup"]) == ["Next 5", "Back to filters"]
    assert bot.method_names()[-1] == "send_message"
    [body] = search_bodies(carousell)
    assert body["count"] == 5 and "session" not in body
    assert storage.get_seen_listing(services.database, "1469923094") is not None


def test_next_uses_the_cursor_and_tags_seen_listings(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()
    send_text(bot, services, "iphone 15")
    tap(bot, services, "go:")
    first_page_cursor = services.draft_store.get(CHAT_ID).next_page_cursor
    bot.calls.clear()

    tap(bot, services, "nx:", message_id=999)

    assert search_bodies(carousell)[-1]["session"] == first_page_cursor
    assert bot.calls_to("edit_message_reply_markup") == [{"chat_id": CHAT_ID, "message_id": 999, "reply_markup": None}]
    cards = bot.calls_to("send_photo")
    assert len(cards) == 5
    # FakeCarousell serves the same fixture listings again, so they were all shown before.
    assert all(card["caption"].startswith(messages.SEEN_TAG) for card in cards)
    assert bot.calls_to("send_message")[-1]["text"].startswith("Page 2:")
    assert services.draft_store.get(CHAT_ID).page_cursors == (None, first_page_cursor)


def test_back_to_filters_redraws_the_paging_message_as_the_panel(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "iphone 15")
    bot.calls.clear()

    tap(bot, services, "bf:", message_id=999)

    [edit] = bot.calls_to("edit_message_text")
    assert edit["message_id"] == 999 and edit["text"].startswith("Search: iphone 15")


def test_photo_rejected_by_telegram_falls_back_to_text(services: BotServices) -> None:
    bot = FakeBot(failures={"send_photo": BadRequest("Wrong file identifier/http url specified")})
    send_text(bot, services, "iphone 15")
    bot.calls.clear()

    tap(bot, services, "go:")

    texts = [call["text"] for call in bot.calls_to("send_message")]
    assert len(texts) == 6  # five text cards and the paging message
    assert "https://www.carousell.sg/p/" in texts[0]


# --- errors -----------------------------------------------------------------------------


def test_blocked_search_sends_a_friendly_message(services: BotServices, tmp_path: Path) -> None:
    services.carousell_client = FakeCarousell(api_responses=[httpx.Response(403), httpx.Response(403)]).client()
    bot = FakeBot()
    send_text(bot, services, "iphone 15")

    tap(bot, services, "go:")  # must not raise

    assert "Carousell is blocking" in bot.calls_to("send_message")[-1]["text"]
    assert bot.calls_to("send_photo") == []


def test_schema_change_points_at_the_canary(services: BotServices) -> None:
    services.carousell_client = FakeCarousell(api_responses=[httpx.Response(200, json={"data": {}})]).client()
    bot = FakeBot()
    send_text(bot, services, "iphone 15")

    tap(bot, services, "go:")

    assert "carousearch canary" in bot.calls_to("send_message")[-1]["text"]


def test_unexpected_error_is_logged_and_reported_briefly(services: BotServices, caplog) -> None:
    services.carousell_client = ExplodingClient()  # type: ignore[assignment]
    bot = FakeBot()
    send_text(bot, services, "iphone 15")

    tap(bot, services, "go:")

    assert bot.calls_to("send_message")[-1]["text"] == messages.describe_error_for_user(RuntimeError())
    assert "unexpected bug" in caplog.text


# --- details and saved searches -------------------------------------------------------


def test_details_are_fetched_once_then_served_from_the_cache(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()

    tap(bot, services, "dt:1469923094")
    tap(bot, services, "dt:1469923094")

    details = bot.calls_to("send_message")
    assert len(details) == 2
    assert "Seller:" in details[0]["text"] and "Description:" in details[0]["text"]
    assert details[0]["text"] == details[1]["text"]
    detail_requests = [request for request in carousell.api_requests if "listing-detail" in request.url.path]
    assert len(detail_requests) == 1


def test_save_list_and_load_a_search(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "aeron chair")
    tap(bot, services, "pr:-800")

    run_command(handlers.handle_save_command, bot, services, ["work", "chair"])
    assert bot.calls_to("send_message")[-1]["text"].startswith('Saved as "work chair"')

    run_command(handlers.handle_save_command, bot, services, ["work", "chair"])
    assert "already exists" in bot.calls_to("send_message")[-1]["text"]

    run_command(handlers.handle_saved_command, bot, services, [])
    saved_list = bot.calls_to("send_message")[-1]
    assert "work chair: aeron chair" in saved_list["text"]
    [load_button] = [button for row in saved_list["reply_markup"].inline_keyboard for button in row]
    assert load_button.text == "Load: work chair"

    send_text(bot, services, "something else")
    tap(bot, services, load_button.callback_data)
    request = services.draft_store.get(CHAT_ID).request
    assert (request.query, request.price_max) == ("aeron chair", 800)
    assert bot.calls_to("send_message")[-1]["text"].startswith("Search: aeron chair")


def test_save_without_a_draft_or_name(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_save_command, bot, services, [])
    run_command(handlers.handle_save_command, bot, services, ["name"])

    assert [call["text"] for call in bot.calls_to("send_message")] == [messages.SAVE_USAGE_TEXT, messages.NO_DRAFT_TEXT]


# --- wiring -------------------------------------------------------------------------------


def test_build_application_registers_handlers_and_services(services: BotServices) -> None:
    application = build_application(TelegramSettings(bot_token="123456:TEST-TOKEN", owner_user_id=OWNER_ID), services)

    registered = application.handlers[0]
    assert application.bot_data[SERVICES_KEY] is services
    assert sum(isinstance(handler, CommandHandler) for handler in registered) == 4
    assert any(isinstance(handler, CallbackQueryHandler) for handler in registered)
    assert any(isinstance(handler, MessageHandler) for handler in registered)


def test_describe_senders_lists_each_user_once() -> None:
    updates = [text_update("hi", OWNER_ID), text_update("again", OWNER_ID), text_update("hello", STRANGER_ID)]

    assert describe_senders(updates) == [f"{OWNER_ID}  Test", f"{STRANGER_ID}  Test"]
