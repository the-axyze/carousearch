"""Handler tests with a fake Telegram bot, a fake clock and the fixture-backed FakeCarousell. No network."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
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
from carousearch.bot.handlers import SERVICES_KEY, BotServices
from carousearch.bot.session_store import SessionStore
from carousearch.bot.state import SearchSession, Stage
from carousearch.config import TelegramSettings
from carousearch.filters.schema import Condition, SearchRequest

from fake_carousell import FakeCarousell
from fixture_files import load_json_fixture

OWNER_ID = 1001
STRANGER_ID = 2002
CHAT_ID = 5005
OLD_MESSAGE_ID = 77
START_TIME = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


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

    def last_text(self) -> str:
        return self.calls_to("send_message")[-1]["text"]

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


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


class ExplodingClient:
    def search(self, request: object) -> None:
        raise RuntimeError("unexpected bug")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START_TIME)


@pytest.fixture
def carousell() -> FakeCarousell:
    return FakeCarousell()


@pytest.fixture
def services(tmp_path: Path, carousell: FakeCarousell, clock: FakeClock) -> BotServices:
    return BotServices(
        carousell_client=carousell.client(),
        database=storage.open_database(tmp_path / "bot.db"),
        session_store=SessionStore(clock),
        owner_user_id=OWNER_ID,
        clock=clock,
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


def button_update(callback_data: str, message_id: int, user_id: int = OWNER_ID) -> Update:
    query = CallbackQuery(
        id="query-1",
        from_user=make_user(user_id),
        chat_instance="instance",
        data=callback_data,
        message=make_message(message_id),
    )
    return Update(update_id=2, callback_query=query)


def current_session(services: BotServices) -> SearchSession | None:
    return services.session_store.get(CHAT_ID)


def send_text(bot: FakeBot, services: BotServices, text: str, user_id: int = OWNER_ID) -> None:
    asyncio.run(handlers.handle_text_message(text_update(text, user_id), make_context(bot, services)))


def tap(bot: FakeBot, services: BotServices, callback_data: str, message_id: int | None = None) -> None:
    """Tap a button on `message_id`; by default on the session's current message."""
    if message_id is None:
        message_id = current_session(services).stage_message_id
    asyncio.run(handlers.handle_button(button_update(callback_data, message_id), make_context(bot, services)))


def run_command(handler: handlers.Handler, bot: FakeBot, services: BotServices, args: list[str]) -> None:
    asyncio.run(handler(text_update("/command"), make_context(bot, services, args)))


def start_at_summary(bot: FakeBot, services: BotServices, **request_fields: object) -> None:
    """The entry point the LLM prompt parser will use: a whole request, straight to the summary."""
    request = SearchRequest(**{"query": "iphone 15", **request_fields})
    asyncio.run(handlers.show_filter_panel_for_request(bot, services, CHAT_ID, request))


def button_texts(reply_markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in reply_markup.inline_keyboard for button in row]


def search_bodies(carousell: FakeCarousell) -> list[dict]:
    return [json.loads(request.content) for request in carousell.api_requests if request.url.path.startswith("/ds/search")]


# --- owner check -------------------------------------------------------------------------


def test_stranger_is_refused_and_ignored(services: BotServices) -> None:
    bot = FakeBot()

    send_text(bot, services, "iphone 15", user_id=STRANGER_ID)

    assert bot.calls == [("send_message", {"chat_id": CHAT_ID, "text": messages.REFUSAL_TEXT})]
    assert current_session(services) is None


def test_stranger_button_tap_is_refused(services: BotServices) -> None:
    bot = FakeBot()

    update = button_update("go:", OLD_MESSAGE_ID, user_id=STRANGER_ID)
    asyncio.run(handlers.handle_button(update, make_context(bot, services)))

    assert bot.calls_to("answer_callback_query") == [{"callback_query_id": "query-1", "text": messages.REFUSAL_TEXT}]
    assert bot.method_names() == ["answer_callback_query"]


# --- starting a session and the wizard ------------------------------------------------------


def test_text_message_starts_a_session_at_the_price_step(services: BotServices) -> None:
    bot = FakeBot()

    send_text(bot, services, "iphone 15")

    session = current_session(services)
    assert (session.stage, session.request.query) == (Stage.PRICE, "iphone 15")
    [step_message] = bot.calls_to("send_message")
    assert step_message["text"].startswith("Step 2/6: Price\nSo far: iphone 15\n")
    assert "✓ Any" in button_texts(step_message["reply_markup"])
    assert session.stage_message_id == 1001  # the id FakeBot gave that message


def test_search_without_keywords_asks_for_them_then_reads_the_typed_keywords(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_search_command, bot, services, [])
    keywords_message_id = current_session(services).stage_message_id
    assert bot.last_text() == f"Step 1/6: Keywords\n\n{messages.KEYWORDS_QUESTION_TEXT}"

    send_text(bot, services, "iphone 15")

    assert current_session(services).stage == Stage.PRICE
    assert current_session(services).request.query == "iphone 15"
    assert bot.calls_to("edit_message_reply_markup") == [
        {"chat_id": CHAT_ID, "message_id": keywords_message_id, "reply_markup": None}
    ]
    assert bot.last_text().startswith("Step 2/6: Price")


def test_tapping_a_choice_edits_the_step_message_in_place(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "iphone 15")
    step_message_id = current_session(services).stage_message_id
    bot.calls.clear()

    tap(bot, services, "pr:100-300")
    tap(bot, services, "co:like_new")

    assert bot.method_names() == ["answer_callback_query", "edit_message_text"] * 2
    first_edit, second_edit = bot.calls_to("edit_message_text")
    assert first_edit["message_id"] == second_edit["message_id"] == step_message_id
    assert first_edit["text"].startswith("Step 3/6: Condition\nSo far: iphone 15 · S$100 to S$300")
    assert "✓ Like new" in button_texts(second_edit["reply_markup"])
    assert current_session(services).request.conditions == [Condition.LIKE_NEW]


def test_unchanged_message_edit_is_not_an_error(services: BotServices) -> None:
    bot = FakeBot(failures={"edit_message_text": BadRequest("Message is not modified: specified new message content")})
    send_text(bot, services, "sofa")
    tap(bot, services, "pr:")

    tap(bot, services, "cx:")  # "Any" is already selected, so Telegram refuses the edit

    assert current_session(services).stage == Stage.CONDITION
    assert bot.last_text().startswith("Step 2/6")  # no error message was sent


def test_typed_price_range_is_read_and_bad_input_is_re_asked(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    price_message_id = current_session(services).stage_message_id

    tap(bot, services, "pa:")
    edit = bot.calls_to("edit_message_text")[-1]
    assert edit["text"].endswith(messages.PRICE_INPUT_PROMPT_TEXT)
    assert button_texts(edit["reply_markup"]) == ["Back", "Cancel"]

    send_text(bot, services, "cheap")
    assert bot.last_text() == messages.render_price_range_problem("cheap")
    assert current_session(services).stage == Stage.PRICE

    send_text(bot, services, "150-400")
    request = current_session(services).request
    assert (request.query, request.price_min, request.price_max) == ("sofa", 150, 400)
    assert {"chat_id": CHAT_ID, "message_id": price_message_id, "reply_markup": None} in bot.calls_to(
        "edit_message_reply_markup"
    )
    assert bot.last_text().startswith("Step 3/6: Condition\nSo far: sofa · S$150 to S$400")


def test_cancel_button_ends_the_session(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    step_message_id = current_session(services).stage_message_id

    tap(bot, services, "cn:")

    assert current_session(services) is None
    assert bot.calls_to("edit_message_text")[-1] == {
        "text": messages.SESSION_ENDED_TEXT, "chat_id": CHAT_ID, "message_id": step_message_id, "reply_markup": None
    }


def test_buttons_on_an_old_message_are_rejected(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    session_before = current_session(services)
    bot.calls.clear()

    tap(bot, services, "pr:100-300", message_id=OLD_MESSAGE_ID)

    assert bot.calls_to("answer_callback_query") == [
        {"callback_query_id": "query-1", "text": messages.INVALID_BUTTON_TEXT}
    ]
    assert bot.calls_to("edit_message_reply_markup") == [
        {"chat_id": CHAT_ID, "message_id": OLD_MESSAGE_ID, "reply_markup": None}
    ]
    assert current_session(services) == session_before


def test_button_without_a_session_is_answered(services: BotServices) -> None:
    bot = FakeBot()

    tap(bot, services, "co:like_new", message_id=OLD_MESSAGE_ID)

    assert bot.calls == [("answer_callback_query", {"callback_query_id": "query-1", "text": messages.NO_SESSION_TEXT})]


# --- the session lock --------------------------------------------------------------------------


def test_search_while_a_session_is_active_is_refused_and_changes_nothing(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "iphone 15")
    tap(bot, services, "pr:100-300")
    session_before = current_session(services)
    bot.calls.clear()

    run_command(handlers.handle_search_command, bot, services, ["sofa"])

    assert bot.method_names() == ["send_message"]
    assert bot.last_text() == (
        "A search for 'iphone 15' is in progress at step 3/6 (Condition). "
        "Send /end to finish it, or tap Back / Refine on the current message."
    )
    assert current_session(services) == session_before


def test_start_shows_help_or_the_reminder(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_start, bot, services, [])
    assert bot.last_text() == messages.HELP_TEXT

    send_text(bot, services, "sofa")
    run_command(handlers.handle_start, bot, services, [])
    assert bot.last_text().startswith("A search for 'sofa' is in progress at step 2/6 (Price).")


def test_plain_text_at_a_button_step_gets_the_reminder(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    tap(bot, services, "pr:")
    session_before = current_session(services)

    send_text(bot, services, "a different search")

    assert bot.last_text().startswith("A search for 'sofa' is in progress at step 3/6 (Condition).")
    assert current_session(services) == session_before


def test_plain_text_while_showing_results_gets_the_reminder(services: BotServices) -> None:
    bot = FakeBot()
    start_at_summary(bot, services)
    tap(bot, services, "go:")

    send_text(bot, services, "sofa")

    assert bot.last_text().startswith("A search for 'iphone 15' is in progress showing results.")


def test_end_clears_the_session_and_retires_its_buttons(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    step_message_id = current_session(services).stage_message_id

    run_command(handlers.handle_end_command, bot, services, [])

    assert current_session(services) is None
    assert bot.calls_to("edit_message_reply_markup") == [
        {"chat_id": CHAT_ID, "message_id": step_message_id, "reply_markup": None}
    ]
    assert bot.last_text() == messages.SESSION_ENDED_TEXT

    send_text(bot, services, "lamp")
    assert current_session(services).request.query == "lamp"


def test_end_and_status_without_a_session(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_end_command, bot, services, [])
    run_command(handlers.handle_status_command, bot, services, [])

    assert [call["text"] for call in bot.calls_to("send_message")] == [messages.NO_SESSION_TEXT] * 2


def test_status_reports_the_position_and_the_filters(services: BotServices) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    tap(bot, services, "pr:-100")

    run_command(handlers.handle_status_command, bot, services, [])

    status = bot.last_text()
    assert status.startswith("Search in progress at step 3/6 (Condition).\n\nSearch: sofa\nPrice: up to S$100\n")
    assert status.endswith("Send /end to finish it.")
    assert current_session(services).stage == Stage.CONDITION


def test_an_expired_session_is_replaced(services: BotServices, clock: FakeClock) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    clock.advance(minutes=31)
    bot.calls.clear()

    send_text(bot, services, "lamp")

    assert [call["text"].splitlines()[0] for call in bot.calls_to("send_message")] == [
        messages.SESSION_EXPIRED_TEXT, "Step 2/6: Price"
    ]
    assert current_session(services).request.query == "lamp"


def test_a_session_idle_for_under_30_minutes_is_still_locked(services: BotServices, clock: FakeClock) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    clock.advance(minutes=29)

    run_command(handlers.handle_search_command, bot, services, ["lamp"])

    assert bot.last_text().startswith("A search for 'sofa'")


def test_button_on_an_expired_session_says_so(services: BotServices, clock: FakeClock) -> None:
    bot = FakeBot()
    send_text(bot, services, "sofa")
    clock.advance(minutes=31)
    bot.calls.clear()

    tap(bot, services, "pr:", message_id=1001)

    assert bot.calls == [
        ("answer_callback_query", {"callback_query_id": "query-1", "text": messages.SESSION_EXPIRED_BUTTON_TEXT})
    ]


# --- search and results ---------------------------------------------------------------------------


def test_a_ready_made_request_starts_at_the_summary(services: BotServices) -> None:
    bot = FakeBot()

    start_at_summary(bot, services, price_max=900)

    assert current_session(services).stage == Stage.SUMMARY
    [summary] = bot.calls_to("send_message")
    assert summary["text"].startswith("Summary\n\nSearch: iphone 15\nPrice: up to S$900\n")
    assert button_texts(summary["reply_markup"])[0] == "Search"


def test_search_sends_five_cards_and_the_results_controls(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()
    start_at_summary(bot, services)
    summary_message_id = current_session(services).stage_message_id
    bot.calls.clear()

    tap(bot, services, "go:")

    cards = bot.calls_to("send_photo")
    assert len(cards) == 5
    assert cards[0]["photo"].startswith("https://media.karousell.com/")
    assert "https://www.carousell.sg/p/" in cards[0]["caption"]
    assert messages.SEEN_TAG not in cards[0]["caption"]
    assert button_texts(cards[0]["reply_markup"]) == ["Details"]
    [controls] = bot.calls_to("send_message")
    assert controls["text"].startswith("Page 1:")
    assert button_texts(controls["reply_markup"]) == ["Next 5", "Refine", "End"]
    assert bot.calls_to("edit_message_reply_markup") == [
        {"chat_id": CHAT_ID, "message_id": summary_message_id, "reply_markup": None}
    ]
    session = current_session(services)
    assert session.stage == Stage.RESULTS
    assert session.stage_message_id != summary_message_id
    [body] = search_bodies(carousell)
    assert body["count"] == 5 and "session" not in body
    assert storage.get_seen_listing(services.database, "1469923094") is not None


def test_next_uses_the_cursor_and_tags_seen_listings(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()
    start_at_summary(bot, services)
    tap(bot, services, "go:")
    first_page_cursor = current_session(services).next_page_cursor
    controls_message_id = current_session(services).stage_message_id
    bot.calls.clear()

    tap(bot, services, "nx:")

    assert search_bodies(carousell)[-1]["session"] == first_page_cursor
    assert bot.calls_to("edit_message_reply_markup") == [
        {"chat_id": CHAT_ID, "message_id": controls_message_id, "reply_markup": None}
    ]
    cards = bot.calls_to("send_photo")
    assert len(cards) == 5
    # FakeCarousell serves the same fixture listings again, so they were all shown before.
    assert all(card["caption"].startswith(messages.SEEN_TAG) for card in cards)
    assert bot.last_text().startswith("Page 2:")
    assert current_session(services).page_cursors == (None, first_page_cursor)


def test_refine_returns_to_the_summary_with_the_same_request(services: BotServices) -> None:
    bot = FakeBot()
    start_at_summary(bot, services, price_max=900)
    tap(bot, services, "go:")
    controls_message_id = current_session(services).stage_message_id
    bot.calls.clear()

    tap(bot, services, "rf:")

    session = current_session(services)
    assert (session.stage, session.request.price_max) == (Stage.SUMMARY, 900)
    [edit] = bot.calls_to("edit_message_text")
    assert edit["message_id"] == controls_message_id
    assert edit["text"].startswith("Summary\n\nSearch: iphone 15\nPrice: up to S$900")

    tap(bot, services, "ed:price")
    tap(bot, services, "pr:100-300")
    assert current_session(services).stage == Stage.SUMMARY  # editing returns to the summary


def test_end_on_the_results_finishes_the_session(services: BotServices) -> None:
    bot = FakeBot()
    start_at_summary(bot, services)
    tap(bot, services, "go:")

    tap(bot, services, "en:")

    assert current_session(services) is None
    assert bot.calls_to("edit_message_text")[-1]["text"] == messages.SESSION_ENDED_TEXT


def test_a_multi_item_card_gets_the_screening_line(tmp_path: Path, clock: FakeClock) -> None:
    response_json = copy.deepcopy(load_json_fixture("search_price_condition.json"))
    first_card = next(item["listingCard"] for item in response_json["data"]["results"] if "listingCard" in item)
    first_card["title"] = "iPhone 15 / 15 Pro / 15 Pro Max all storage"
    carousell = FakeCarousell(api_responses=[httpx.Response(200, json=response_json)])
    services = BotServices(
        carousell_client=carousell.client(),
        database=storage.open_database(tmp_path / "bot.db"),
        session_store=SessionStore(clock),
        owner_user_id=OWNER_ID,
        clock=clock,
    )
    bot = FakeBot()
    start_at_summary(bot, services)

    tap(bot, services, "go:")

    cards = bot.calls_to("send_photo")
    assert cards[0]["caption"].endswith("\nMulti-item · listed S$579 · may not be your variant's price")
    assert all("Multi-item" not in card["caption"] for card in cards[1:])
    assert [request for request in carousell.api_requests if "listing-detail" in request.url.path] == []


def test_photo_rejected_by_telegram_falls_back_to_text(services: BotServices) -> None:
    bot = FakeBot(failures={"send_photo": BadRequest("Wrong file identifier/http url specified")})
    start_at_summary(bot, services)
    bot.calls.clear()

    tap(bot, services, "go:")

    texts = [call["text"] for call in bot.calls_to("send_message")]
    assert len(texts) == 6  # five text cards and the results controls
    assert "https://www.carousell.sg/p/" in texts[0]


# --- errors -----------------------------------------------------------------------------


def test_blocked_search_sends_a_friendly_message_and_keeps_the_summary(services: BotServices) -> None:
    services.carousell_client = FakeCarousell(api_responses=[httpx.Response(403), httpx.Response(403)]).client()
    bot = FakeBot()
    start_at_summary(bot, services)

    tap(bot, services, "go:")  # must not raise

    assert "Carousell is blocking" in bot.last_text()
    assert bot.calls_to("send_photo") == []
    assert bot.calls_to("edit_message_reply_markup") == []  # the Search button is still there
    assert current_session(services).stage == Stage.SUMMARY


def test_schema_change_points_at_the_canary(services: BotServices) -> None:
    services.carousell_client = FakeCarousell(api_responses=[httpx.Response(200, json={"data": {}})]).client()
    bot = FakeBot()
    start_at_summary(bot, services)

    tap(bot, services, "go:")

    assert "carousearch canary" in bot.last_text()


def test_unexpected_error_is_logged_and_reported_briefly(services: BotServices, caplog) -> None:
    services.carousell_client = ExplodingClient()  # type: ignore[assignment]
    bot = FakeBot()
    start_at_summary(bot, services)

    tap(bot, services, "go:")

    assert bot.last_text() == messages.describe_error_for_user(RuntimeError())
    assert "unexpected bug" in caplog.text


# --- details and saved searches -------------------------------------------------------


def test_details_are_fetched_once_then_served_from_the_cache(services: BotServices, carousell: FakeCarousell) -> None:
    bot = FakeBot()

    tap(bot, services, "dt:1469923094", message_id=OLD_MESSAGE_ID)
    tap(bot, services, "dt:1469923094", message_id=OLD_MESSAGE_ID)

    details = bot.calls_to("send_message")
    assert len(details) == 2
    assert "Seller:" in details[0]["text"] and "Description:" in details[0]["text"]
    assert details[0]["text"] == details[1]["text"]
    detail_requests = [request for request in carousell.api_requests if "listing-detail" in request.url.path]
    assert len(detail_requests) == 1


def test_save_list_and_load_a_search(services: BotServices) -> None:
    bot = FakeBot()
    start_at_summary(bot, services, query="aeron chair", price_max=800)

    run_command(handlers.handle_save_command, bot, services, ["work", "chair"])
    assert bot.last_text().startswith('Saved as "work chair"')

    run_command(handlers.handle_save_command, bot, services, ["work", "chair"])
    assert "already exists" in bot.last_text()

    run_command(handlers.handle_saved_command, bot, services, [])
    saved_list = bot.calls_to("send_message")[-1]
    assert "work chair: aeron chair" in saved_list["text"]
    [load_button] = [button for row in saved_list["reply_markup"].inline_keyboard for button in row]
    assert load_button.text == "Load: work chair"

    tap(bot, services, load_button.callback_data, message_id=OLD_MESSAGE_ID)
    assert bot.last_text().startswith("A search for 'aeron chair' is in progress at the summary.")

    run_command(handlers.handle_end_command, bot, services, [])
    tap(bot, services, load_button.callback_data, message_id=OLD_MESSAGE_ID)
    session = current_session(services)
    assert (session.stage, session.request.query, session.request.price_max) == (Stage.SUMMARY, "aeron chair", 800)
    assert bot.last_text().startswith("Summary\n\nSearch: aeron chair")


def test_save_needs_a_name_and_a_search_with_keywords(services: BotServices) -> None:
    bot = FakeBot()

    run_command(handlers.handle_save_command, bot, services, [])
    run_command(handlers.handle_save_command, bot, services, ["name"])
    run_command(handlers.handle_search_command, bot, services, [])  # waiting for keywords
    run_command(handlers.handle_save_command, bot, services, ["name"])

    texts = [call["text"] for call in bot.calls_to("send_message")]
    assert [texts[0], texts[1], texts[3]] == [
        messages.SAVE_USAGE_TEXT, messages.NOTHING_TO_SAVE_TEXT, messages.NOTHING_TO_SAVE_TEXT
    ]


# --- wiring -------------------------------------------------------------------------------


def test_build_application_registers_handlers_and_services(services: BotServices) -> None:
    application = build_application(TelegramSettings(bot_token="123456:TEST-TOKEN", owner_user_id=OWNER_ID), services)

    registered = application.handlers[0]
    commands = {command for handler in registered if isinstance(handler, CommandHandler) for command in handler.commands}
    assert application.bot_data[SERVICES_KEY] is services
    assert commands == {"start", "help", "search", "end", "status", "save", "saved"}
    assert any(isinstance(handler, CallbackQueryHandler) for handler in registered)
    assert any(isinstance(handler, MessageHandler) for handler in registered)


def test_describe_senders_lists_each_user_once() -> None:
    updates = [text_update("hi", OWNER_ID), text_update("again", OWNER_ID), text_update("hello", STRANGER_ID)]

    assert describe_senders(updates) == [f"{OWNER_ID}  Test", f"{STRANGER_ID}  Test"]
