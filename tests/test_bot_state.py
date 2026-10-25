from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from carousearch.bot.keyboard import (
    build_keyboard_for_stage,
    build_listing_card_keyboard,
    build_saved_searches_keyboard,
)
from carousearch.bot.price_input import format_price_range, parse_price_range
from carousearch.bot.session_store import SessionStore
from carousearch.bot.state import (
    MAX_CALLBACK_DATA_BYTES,
    RESULTS_PER_PAGE,
    SESSION_IDLE_TIMEOUT,
    WIZARD_STEPS,
    Action,
    ActionKind,
    AwaitingInput,
    SearchSession,
    Stage,
    apply_action,
    apply_text_input,
    can_go_back,
    decode_action,
    encode_action,
    expects_text_input,
    first_page_request,
    is_expired,
    next_page_request,
    next_stage,
    previous_stage,
    record_page_shown,
    start_session,
    start_session_at_summary,
    step_number,
)
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder
from carousearch.storage import SavedSearch

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def session_at(stage: Stage, **request_fields: object) -> SearchSession:
    return SearchSession(request=SearchRequest(query="office chair", **request_fields), stage=stage, last_activity_at=NOW)


def press(session: SearchSession, kind: ActionKind, value: str = "") -> SearchSession:
    updated = apply_action(session, Action(kind, value), LATER)
    assert updated is not None
    return updated


def type_text(session: SearchSession, text: str) -> SearchSession:
    updated, problem = apply_text_input(session, text, LATER)
    assert problem is None
    return updated


def every_action() -> list[Action]:
    """One realistic action of each kind, with the longest values the bot produces."""
    return [
        Action(ActionKind.NEXT),
        Action(ActionKind.BACK),
        Action(ActionKind.CANCEL),
        Action(ActionKind.EDIT, Stage.CONDITION),
        Action(ActionKind.SET_PRICE, "100000-999999.5"),
        Action(ActionKind.TYPE_PRICE),
        Action(ActionKind.TOGGLE_CONDITION, Condition.HEAVILY_USED),
        Action(ActionKind.CLEAR_CONDITIONS),
        Action(ActionKind.TOGGLE_DEAL_METHOD, DealMethod.MAILING),
        Action(ActionKind.CLEAR_DEAL_METHODS),
        Action(ActionKind.SET_SORT, SortOrder.PRICE_HIGH_TO_LOW),
        Action(ActionKind.SET_CATEGORY, "123456789"),
        Action(ActionKind.TYPE_CATEGORY),
        Action(ActionKind.RUN_SEARCH),
        Action(ActionKind.NEXT_PAGE),
        Action(ActionKind.REFINE),
        Action(ActionKind.END),
        Action(ActionKind.SHOW_DETAILS, "14699230941469923094"),
        Action(ActionKind.LOAD_SAVED_SEARCH, "999999"),
    ]


# --- callback_data encoding ------------------------------------------------------------


def test_every_action_kind_round_trips() -> None:
    actions = every_action()
    assert {action.kind for action in actions} == set(ActionKind)
    for action in actions:
        assert decode_action(encode_action(action)) == action


def test_every_encoded_action_fits_in_64_bytes() -> None:
    for action in every_action():
        assert len(encode_action(action).encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES


def test_every_keyboard_button_fits_in_64_bytes() -> None:
    full_request = SearchRequest(
        query="x" * 500, price_min=123456.75, price_max=999999.25, category="999999999",
        conditions=list(Condition), deal_methods=list(DealMethod), sort=SortOrder.PRICE_HIGH_TO_LOW,
    )
    sessions = [SearchSession(request=None, stage=Stage.KEYWORDS, last_activity_at=NOW)]
    for request in [SearchRequest(query="a"), full_request]:
        for stage in Stage:
            sessions.append(SearchSession(request=request, stage=stage, last_activity_at=NOW, next_page_cursor="c" * 500))
    layouts = [build_keyboard_for_stage(session) for session in sessions]
    layouts.append(build_listing_card_keyboard("1469923094"))
    saved = SavedSearch(id=123456, name="x" * 200, request=full_request, created_at=NOW)
    layouts.append(build_saved_searches_keyboard([saved]))
    for layout in layouts:
        for row in layout:
            for button in row:
                assert 0 < len(button.callback_data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES


def test_encoding_refuses_callback_data_over_64_bytes() -> None:
    with pytest.raises(ValueError, match="longer than 64 bytes"):
        encode_action(Action(ActionKind.SHOW_DETAILS, "9" * 70))


@pytest.mark.parametrize("callback_data", ["", "go", "zz:1", "unknown:"])
def test_decoding_rejects_bad_callback_data(callback_data: str) -> None:
    with pytest.raises(ValueError):
        decode_action(callback_data)


# --- starting a session and the order of stages --------------------------------------------


def test_keywords_given_up_front_skip_the_keywords_step() -> None:
    session = start_session("office chair", NOW)

    assert session.stage == Stage.PRICE
    assert session.request == SearchRequest(query="office chair")
    assert session.last_activity_at == NOW


@pytest.mark.parametrize("query", [None, "", "   "])
def test_no_keywords_starts_at_the_keywords_step(query: str | None) -> None:
    session = start_session(query, NOW)

    assert (session.stage, session.request) == (Stage.KEYWORDS, None)
    assert expects_text_input(session)


def test_a_ready_made_request_starts_at_the_summary() -> None:
    session = start_session_at_summary(SearchRequest(query="sofa", price_max=300, page_cursor="stale"), NOW)

    assert session.stage == Stage.SUMMARY
    assert session.request == SearchRequest(query="sofa", price_max=300)


def test_six_numbered_steps_then_summary_and_results() -> None:
    assert WIZARD_STEPS == (Stage.KEYWORDS, Stage.PRICE, Stage.CONDITION, Stage.DELIVERY, Stage.SORT, Stage.CATEGORY)
    assert [step_number(stage) for stage in Stage] == [1, 2, 3, 4, 5, 6, None, None]
    assert next_stage(Stage.CATEGORY) == Stage.SUMMARY
    assert previous_stage(Stage.PRICE) == Stage.KEYWORDS
    with pytest.raises(ValueError):
        next_stage(Stage.RESULTS)
    with pytest.raises(ValueError):
        previous_stage(Stage.KEYWORDS)


# --- the happy path -------------------------------------------------------------------------


def test_full_happy_path_through_every_stage() -> None:
    session = start_session(None, NOW)

    session = type_text(session, "  office chair ")
    assert session.stage == Stage.PRICE
    session = press(session, ActionKind.SET_PRICE, "100-300")
    assert session.stage == Stage.CONDITION
    session = press(session, ActionKind.TOGGLE_CONDITION, "well_used")
    session = press(session, ActionKind.TOGGLE_CONDITION, "like_new")
    assert session.stage == Stage.CONDITION  # toggles stay on the step
    session = press(session, ActionKind.NEXT)
    assert session.stage == Stage.DELIVERY
    session = press(session, ActionKind.TOGGLE_DEAL_METHOD, "meetup")
    session = press(session, ActionKind.NEXT)
    assert session.stage == Stage.SORT
    session = press(session, ActionKind.SET_SORT, "recent")
    assert session.stage == Stage.CATEGORY
    session = press(session, ActionKind.SET_CATEGORY, "13")

    assert session.stage == Stage.SUMMARY
    assert session.request == SearchRequest(
        query="office chair",
        price_min=100,
        price_max=300,
        conditions=[Condition.LIKE_NEW, Condition.WELL_USED],
        deal_methods=[DealMethod.MEETUP],
        sort=SortOrder.RECENT,
        category="13",
    )
    assert session.last_activity_at == LATER

    session = record_page_shown(session, None, "cursor-2", LATER)
    assert session.stage == Stage.RESULTS
    session = press(session, ActionKind.REFINE)
    assert session.stage == Stage.SUMMARY
    assert session.request.category == "13"


def test_any_clears_toggles_and_any_price_or_category_clears_the_filter() -> None:
    conditions = press(session_at(Stage.CONDITION, conditions=[Condition.BRAND_NEW]), ActionKind.CLEAR_CONDITIONS)
    deal_methods = press(session_at(Stage.DELIVERY, deal_methods=[DealMethod.MAILING]), ActionKind.CLEAR_DEAL_METHODS)
    price = press(session_at(Stage.PRICE, price_max=100), ActionKind.SET_PRICE, "")
    category = press(session_at(Stage.CATEGORY, category="13"), ActionKind.SET_CATEGORY, "")

    assert conditions.request.conditions == [] and conditions.stage == Stage.CONDITION
    assert deal_methods.request.deal_methods == [] and deal_methods.stage == Stage.DELIVERY
    assert (price.request.price_min, price.request.price_max) == (None, None) and price.stage == Stage.CONDITION
    assert category.request.category is None and category.stage == Stage.SUMMARY


def test_next_on_the_keywords_step_keeps_the_keywords_once_there_are_some() -> None:
    assert press(session_at(Stage.KEYWORDS), ActionKind.NEXT).stage == Stage.PRICE
    with pytest.raises(ValueError):
        apply_action(start_session(None, NOW), Action(ActionKind.NEXT), LATER)


# --- Back -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (Stage.PRICE, Stage.KEYWORDS),
        (Stage.CONDITION, Stage.PRICE),
        (Stage.DELIVERY, Stage.CONDITION),
        (Stage.SORT, Stage.DELIVERY),
        (Stage.CATEGORY, Stage.SORT),
        (Stage.SUMMARY, Stage.CATEGORY),
    ],
)
def test_back_goes_to_the_previous_stage(stage: Stage, expected: Stage) -> None:
    session = session_at(stage, price_max=50)

    went_back = press(session, ActionKind.BACK)

    assert went_back.stage == expected
    assert went_back.request == session.request


def test_there_is_no_back_from_the_first_step() -> None:
    session = session_at(Stage.KEYWORDS)

    assert not can_go_back(session)
    with pytest.raises(ValueError):
        apply_action(session, Action(ActionKind.BACK), LATER)


def test_back_while_typing_a_value_returns_to_the_step_buttons() -> None:
    typing_price = press(session_at(Stage.PRICE), ActionKind.TYPE_PRICE)
    typing_category = press(session_at(Stage.CATEGORY), ActionKind.TYPE_CATEGORY)

    assert typing_price.awaiting_input == AwaitingInput.PRICE_RANGE
    assert typing_category.awaiting_input == AwaitingInput.CATEGORY_ID
    assert press(typing_price, ActionKind.BACK) == replace(typing_price, awaiting_input=None, last_activity_at=LATER)
    assert press(typing_category, ActionKind.BACK).stage == Stage.CATEGORY


# --- Edit from the summary ---------------------------------------------------------------------


ANSWER_FOR_STEP: dict[Stage, list[Action]] = {
    Stage.PRICE: [Action(ActionKind.SET_PRICE, "-100")],
    Stage.CONDITION: [Action(ActionKind.TOGGLE_CONDITION, "brand_new"), Action(ActionKind.NEXT)],
    Stage.DELIVERY: [Action(ActionKind.TOGGLE_DEAL_METHOD, "mailing"), Action(ActionKind.NEXT)],
    Stage.SORT: [Action(ActionKind.SET_SORT, "price_low_to_high")],
    Stage.CATEGORY: [Action(ActionKind.SET_CATEGORY, "1793")],
}


@pytest.mark.parametrize("step", list(ANSWER_FOR_STEP))
def test_edit_from_the_summary_returns_to_the_summary(step: Stage) -> None:
    editing = press(session_at(Stage.SUMMARY), ActionKind.EDIT, step.value)
    assert (editing.stage, editing.return_to_summary) == (step, True)

    for action in ANSWER_FOR_STEP[step]:
        editing = press(editing, action.kind, action.value)

    assert (editing.stage, editing.return_to_summary) == (Stage.SUMMARY, False)
    assert editing.request != session_at(Stage.SUMMARY).request


def test_edit_keywords_from_the_summary_returns_to_the_summary() -> None:
    editing = press(session_at(Stage.SUMMARY, price_max=99), ActionKind.EDIT, Stage.KEYWORDS.value)
    assert can_go_back(editing)

    edited = type_text(editing, "gaming chair")

    assert edited.stage == Stage.SUMMARY
    assert (edited.request.query, edited.request.price_max) == ("gaming chair", 99)


def test_back_while_editing_returns_to_the_summary() -> None:
    editing = press(session_at(Stage.SUMMARY), ActionKind.EDIT, Stage.DELIVERY.value)

    assert press(editing, ActionKind.BACK).stage == Stage.SUMMARY


@pytest.mark.parametrize("value", ["summary", "results", "colour"])
def test_edit_only_opens_wizard_steps(value: str) -> None:
    with pytest.raises(ValueError):
        apply_action(session_at(Stage.SUMMARY), Action(ActionKind.EDIT, value), LATER)


# --- Cancel, End, and buttons from the wrong stage -------------------------------------------------


@pytest.mark.parametrize("stage", list(Stage))
def test_cancel_and_end_finish_the_session(stage: Stage) -> None:
    assert apply_action(session_at(stage), Action(ActionKind.CANCEL), LATER) is None
    assert apply_action(session_at(stage), Action(ActionKind.END), LATER) is None


@pytest.mark.parametrize(
    ("stage", "action"),
    [
        (Stage.CONDITION, Action(ActionKind.SET_PRICE, "100-300")),
        (Stage.PRICE, Action(ActionKind.TOGGLE_CONDITION, "like_new")),
        (Stage.SORT, Action(ActionKind.NEXT)),
        (Stage.CATEGORY, Action(ActionKind.SET_SORT, "recent")),
        (Stage.PRICE, Action(ActionKind.EDIT, "category")),
        (Stage.SUMMARY, Action(ActionKind.REFINE)),
        (Stage.RESULTS, Action(ActionKind.SET_CATEGORY, "13")),
    ],
)
def test_a_button_from_another_stage_is_rejected(stage: Stage, action: Action) -> None:
    with pytest.raises(ValueError, match="belongs to"):
        apply_action(session_at(stage), action, LATER)


@pytest.mark.parametrize(
    ("stage", "action"),
    [
        (Stage.CONDITION, Action(ActionKind.TOGGLE_CONDITION, "mint")),
        (Stage.DELIVERY, Action(ActionKind.TOGGLE_DEAL_METHOD, "courier")),
        (Stage.PRICE, Action(ActionKind.SET_PRICE, "cheap")),
        (Stage.SORT, Action(ActionKind.SET_SORT, "random")),
        (Stage.CATEGORY, Action(ActionKind.SET_CATEGORY, "chairs")),
    ],
)
def test_bad_button_values_raise(stage: Stage, action: Action) -> None:
    with pytest.raises(ValueError):
        apply_action(session_at(stage), action, LATER)


@pytest.mark.parametrize("kind", [ActionKind.RUN_SEARCH, ActionKind.NEXT_PAGE, ActionKind.SHOW_DETAILS])
def test_actions_that_need_io_are_not_applied_here(kind: ActionKind) -> None:
    with pytest.raises(ValueError, match="not a session action"):
        apply_action(session_at(Stage.SUMMARY), Action(kind), LATER)


# --- typed text ------------------------------------------------------------------------------------


def test_typed_price_range_and_collection_id() -> None:
    session = press(session_at(Stage.PRICE), ActionKind.TYPE_PRICE)
    assert expects_text_input(session)

    session = type_text(session, " S$1,000 – 2,500 ")
    assert (session.request.price_min, session.request.price_max) == (1000, 2500)
    assert (session.stage, session.awaiting_input) == (Stage.CONDITION, None)

    session = press(session_at(Stage.CATEGORY), ActionKind.TYPE_CATEGORY)
    session = type_text(session, " 5755 ")
    assert (session.request.category, session.stage) == ("5755", Stage.SUMMARY)


def test_bad_typed_price_is_reported_and_the_step_keeps_waiting() -> None:
    session = press(session_at(Stage.PRICE), ActionKind.TYPE_PRICE)

    updated, problem = apply_text_input(session, "cheap please", LATER + timedelta(minutes=1))

    assert problem is not None and '"cheap please"' in problem and "150-400" in problem
    assert (updated.stage, updated.awaiting_input, updated.request) == (Stage.PRICE, AwaitingInput.PRICE_RANGE, session.request)
    assert updated.last_activity_at == LATER + timedelta(minutes=1)


def test_bad_typed_collection_id_and_blank_keywords_are_reported() -> None:
    typing_category = press(session_at(Stage.CATEGORY), ActionKind.TYPE_CATEGORY)
    _, category_problem = apply_text_input(typing_category, "chairs", LATER)
    _, keywords_problem = apply_text_input(start_session(None, NOW), "   ", LATER)

    assert category_problem is not None and "number" in category_problem
    assert keywords_problem is not None and "keywords" in keywords_problem


@pytest.mark.parametrize("stage", [Stage.PRICE, Stage.CONDITION, Stage.DELIVERY, Stage.SORT, Stage.SUMMARY, Stage.RESULTS])
def test_only_keywords_and_typed_values_take_text(stage: Stage) -> None:
    session = session_at(stage)

    assert not expects_text_input(session)
    with pytest.raises(ValueError, match="not waiting"):
        apply_text_input(session, "100-200", LATER)


# --- price ranges --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("150-400", (150, 400)),
        ("<250", (None, 250)),
        ("< 250", (None, 250)),
        (">1000", (1000, None)),
        ("800+", (800, None)),
        ("-50", (None, 50)),
        ("50-", (50, None)),
        ("$99.5-$120", (99.5, 120)),
        ("S$1,000 – 2,000", (1000, 2000)),
        ("100—300", (100, 300)),
        ("0-0", (0, 0)),
        ("any", (None, None)),
        ("ANY", (None, None)),
        ("", (None, None)),
        # Invalid input:
        ("500", None),
        ("-", None),
        ("<", None),
        (">", None),
        ("+", None),
        ("500-100", None),
        ("1-2-3", None),
        ("abc", None),
        ("cheap please", None),
        ("inf-", None),
        ("nan-5", None),
        ("<-5", None),
        (">abc", None),
    ],
)
def test_parse_price_range(text: str, expected: tuple[float | None, float | None] | None) -> None:
    assert parse_price_range(text) == expected


def test_format_price_range_round_trips() -> None:
    for price_range in [(100, 300), (None, 100), (800, None), (None, None), (99.5, 120)]:
        assert parse_price_range(format_price_range(*price_range)) == price_range


# --- expiry -----------------------------------------------------------------------------------------


def test_a_session_expires_after_30_idle_minutes() -> None:
    session = start_session("sofa", NOW)

    assert not is_expired(session, NOW + SESSION_IDLE_TIMEOUT, SESSION_IDLE_TIMEOUT)
    assert is_expired(session, NOW + SESSION_IDLE_TIMEOUT + timedelta(seconds=1), SESSION_IDLE_TIMEOUT)
    assert SESSION_IDLE_TIMEOUT == timedelta(minutes=30)


def test_activity_pushes_expiry_back() -> None:
    session = press(start_session("sofa", NOW), ActionKind.SET_PRICE, "")

    assert not is_expired(session, LATER + timedelta(minutes=29), SESSION_IDLE_TIMEOUT)


def test_session_store_hides_and_then_removes_an_expired_session() -> None:
    clock = FakeClock(NOW)
    store = SessionStore(clock)
    session = start_session("sofa", NOW)
    store.set(5, session)

    clock.now = NOW + timedelta(minutes=30)
    assert store.get(5) == session
    assert not store.remove_if_expired(5)

    clock.now = NOW + timedelta(minutes=31)
    assert store.get(5) is None
    assert store.remove_if_expired(5)
    assert not store.remove_if_expired(5)


def test_session_store_keeps_one_session_per_chat() -> None:
    store = SessionStore(FakeClock(NOW))
    store.set(5, start_session("sofa", NOW))
    store.set(6, start_session("lamp", NOW))
    store.set(5, start_session("table", NOW))

    store.clear(6)

    assert store.get(5).request.query == "table"
    assert store.get(6) is None


# --- result pages ------------------------------------------------------------------------------------


def test_first_page_request_uses_five_results_and_no_cursor() -> None:
    request = first_page_request(session_at(Stage.SUMMARY, page_size=40, price_max=100))

    assert request.page_size == RESULTS_PER_PAGE
    assert request.page_cursor is None
    assert request.price_max == 100


def test_pages_push_cursors_and_move_to_results() -> None:
    session = record_page_shown(session_at(Stage.SUMMARY), None, "cursor-2", LATER)
    assert (session.stage, session.last_activity_at) == (Stage.RESULTS, LATER)
    assert next_page_request(session).page_cursor == "cursor-2"

    session = record_page_shown(session, "cursor-2", None, LATER)

    assert session.page_cursors == (None, "cursor-2")
    assert next_page_request(session) is None


def test_new_first_page_restarts_the_cursor_stack() -> None:
    session = record_page_shown(session_at(Stage.SUMMARY), None, "cursor-2", LATER)
    session = record_page_shown(session, "cursor-2", "cursor-3", LATER)

    session = record_page_shown(session, None, "cursor-2", LATER)

    assert session.page_cursors == (None,)
