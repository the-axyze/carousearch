from datetime import UTC, datetime

import pytest

from carousearch.bot.keyboard import (
    build_filter_keyboard,
    build_listing_card_keyboard,
    build_results_keyboard,
    build_saved_searches_keyboard,
)
from carousearch.bot.state import (
    FILTER_ACTION_KINDS,
    MAX_CALLBACK_DATA_BYTES,
    RESULTS_PER_PAGE,
    Action,
    ActionKind,
    AwaitingInput,
    ChatDraft,
    apply_action,
    apply_text_input,
    decode_action,
    encode_action,
    first_page_request,
    format_price_range,
    next_page_request,
    next_sort,
    parse_price_range,
    record_page_shown,
    start_draft,
)
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder
from carousearch.storage import SavedSearch


def draft_for(**request_fields: object) -> ChatDraft:
    return start_draft(SearchRequest(query="office chair", **request_fields))


def every_action() -> list[Action]:
    """One realistic action of each kind, with the longest values the bot produces."""
    return [
        Action(ActionKind.SET_PRICE, "100000-999999.5"),
        Action(ActionKind.ASK_PRICE),
        Action(ActionKind.TOGGLE_CONDITION, Condition.HEAVILY_USED),
        Action(ActionKind.TOGGLE_DEAL_METHOD, DealMethod.MAILING),
        Action(ActionKind.NEXT_SORT),
        Action(ActionKind.SET_CATEGORY, "123456789"),
        Action(ActionKind.ASK_CATEGORY),
        Action(ActionKind.RESET),
        Action(ActionKind.RUN_SEARCH),
        Action(ActionKind.NEXT_PAGE),
        Action(ActionKind.BACK_TO_FILTERS),
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
    requests = [
        SearchRequest(query="a"),
        SearchRequest(query="a", price_min=123456.75, price_max=999999.25, category="999999999",
                      conditions=list(Condition), deal_methods=list(DealMethod), sort=SortOrder.PRICE_HIGH_TO_LOW),
    ]
    layouts = [build_filter_keyboard(request) for request in requests]
    layouts.append(build_listing_card_keyboard("1469923094"))
    layouts.append(build_results_keyboard(has_next_page=True))
    saved = SavedSearch(id=123456, name="x" * 200, request=requests[0], created_at=datetime.now(UTC))
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


# --- filter actions ------------------------------------------------------------------


def test_set_price_preset_and_clear() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.SET_PRICE, "100-300"))
    assert (draft.request.price_min, draft.request.price_max) == (100, 300)

    draft = apply_action(draft, Action(ActionKind.SET_PRICE, "-100"))
    assert (draft.request.price_min, draft.request.price_max) == (None, 100)

    draft = apply_action(draft, Action(ActionKind.SET_PRICE, "800-"))
    assert (draft.request.price_min, draft.request.price_max) == (800, None)

    draft = apply_action(draft, Action(ActionKind.SET_PRICE, ""))
    assert (draft.request.price_min, draft.request.price_max) == (None, None)


def test_toggle_condition_on_and_off_in_canonical_order() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.TOGGLE_CONDITION, "well_used"))
    draft = apply_action(draft, Action(ActionKind.TOGGLE_CONDITION, "like_new"))
    assert draft.request.conditions == [Condition.LIKE_NEW, Condition.WELL_USED]

    draft = apply_action(draft, Action(ActionKind.TOGGLE_CONDITION, "well_used"))
    assert draft.request.conditions == [Condition.LIKE_NEW]


def test_toggle_deal_method() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.TOGGLE_DEAL_METHOD, "mailing"))
    draft = apply_action(draft, Action(ActionKind.TOGGLE_DEAL_METHOD, "meetup"))
    assert draft.request.deal_methods == [DealMethod.MEETUP, DealMethod.MAILING]

    draft = apply_action(draft, Action(ActionKind.TOGGLE_DEAL_METHOD, "meetup"))
    assert draft.request.deal_methods == [DealMethod.MAILING]


def test_next_sort_cycles_and_wraps() -> None:
    draft = draft_for()
    seen_sorts = []
    for _ in range(5):
        draft = apply_action(draft, Action(ActionKind.NEXT_SORT))
        seen_sorts.append(draft.request.sort)
    assert seen_sorts == [
        SortOrder.RECENT, SortOrder.PRICE_LOW_TO_HIGH, SortOrder.PRICE_HIGH_TO_LOW, SortOrder.BEST_MATCH, SortOrder.RECENT
    ]
    assert next_sort(SortOrder.NEARBY) == SortOrder.BEST_MATCH


def test_set_and_clear_category() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.SET_CATEGORY, "5755"))
    assert draft.request.category == "5755"

    draft = apply_action(draft, Action(ActionKind.SET_CATEGORY, ""))
    assert draft.request.category is None


def test_ask_actions_wait_for_text_without_changing_filters() -> None:
    draft = draft_for(price_max=50)

    asking_price = apply_action(draft, Action(ActionKind.ASK_PRICE))
    asking_category = apply_action(draft, Action(ActionKind.ASK_CATEGORY))

    assert asking_price.awaiting_input == AwaitingInput.PRICE_RANGE
    assert asking_category.awaiting_input == AwaitingInput.CATEGORY_ID
    assert asking_price.request == draft.request == asking_category.request


def test_reset_returns_a_clean_draft_keeping_the_query() -> None:
    draft = draft_for(price_min=10, price_max=20, conditions=[Condition.BRAND_NEW], deal_methods=[DealMethod.MEETUP],
                      sort=SortOrder.RECENT, category="5755", buyer_protection_only=True)

    reset = apply_action(draft, Action(ActionKind.RESET))

    assert reset.request == SearchRequest(query="office chair")


def test_reset_keeps_the_category_when_there_is_no_query() -> None:
    draft = start_draft(SearchRequest(category="5707", price_max=100))

    assert apply_action(draft, Action(ActionKind.RESET)).request == SearchRequest(category="5707")


def test_filter_change_restarts_paging_and_cancels_typed_input() -> None:
    draft = record_page_shown(draft_for(), None, ["1"], "cursor-2")
    draft = apply_action(draft, Action(ActionKind.ASK_PRICE))

    changed = apply_action(draft, Action(ActionKind.NEXT_SORT))

    assert changed.page_cursors == () and changed.next_page_cursor is None
    assert changed.awaiting_input is None
    assert changed.shown_listing_ids == {"1"}


@pytest.mark.parametrize(
    "action",
    [
        Action(ActionKind.TOGGLE_CONDITION, "mint"),
        Action(ActionKind.TOGGLE_DEAL_METHOD, "courier"),
        Action(ActionKind.SET_PRICE, "cheap"),
        Action(ActionKind.SET_CATEGORY, "chairs"),
    ],
)
def test_bad_filter_values_raise(action: Action) -> None:
    with pytest.raises(ValueError):
        apply_action(draft_for(), action)


def test_non_filter_actions_are_not_applied_to_filters() -> None:
    for kind in set(ActionKind) - FILTER_ACTION_KINDS:
        with pytest.raises(ValueError, match="not a filter action"):
            apply_action(draft_for(), Action(kind))


# --- typed input ------------------------------------------------------------------------


def test_typed_price_and_category() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.ASK_PRICE))
    draft = apply_text_input(draft, " S$1,000 - 2,500 ")
    assert (draft.request.price_min, draft.request.price_max) == (1000, 2500)
    assert draft.awaiting_input is None

    draft = apply_action(draft, Action(ActionKind.ASK_CATEGORY))
    draft = apply_text_input(draft, " 1793 ")
    assert draft.request.category == "1793"


def test_bad_typed_input_raises_and_keeps_waiting() -> None:
    draft = apply_action(draft_for(), Action(ActionKind.ASK_PRICE))
    with pytest.raises(ValueError, match="price range"):
        apply_text_input(draft, "cheap please")
    assert draft.awaiting_input == AwaitingInput.PRICE_RANGE

    with pytest.raises(ValueError, match="not waiting"):
        apply_text_input(draft_for(), "100-200")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100-500", (100, 500)),
        ("<300", (None, 300)),
        ("800+", (800, None)),
        ("-50", (None, 50)),
        ("50-", (50, None)),
        ("$99.5-$120", (99.5, 120)),
        ("any", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_price_range(text: str, expected: tuple[float | None, float | None]) -> None:
    assert parse_price_range(text) == expected


@pytest.mark.parametrize("text", ["500", "-", "500-100", "1-2-3", "abc", "inf-", "<"])
def test_parse_price_range_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_price_range(text)


def test_format_price_range_round_trips() -> None:
    for price_range in [(100, 300), (None, 100), (800, None), (None, None), (99.5, 120)]:
        assert parse_price_range(format_price_range(*price_range)) == price_range


# --- paging -------------------------------------------------------------------------------


def test_first_page_request_uses_five_results_and_no_cursor() -> None:
    draft = draft_for(page_size=40, price_max=100)

    request = first_page_request(draft)

    assert request.page_size == RESULTS_PER_PAGE
    assert request.page_cursor is None
    assert request.price_max == 100


def test_pages_push_cursors_and_collect_shown_listings() -> None:
    draft = record_page_shown(draft_for(), None, ["1", "2"], "cursor-2")
    assert next_page_request(draft).page_cursor == "cursor-2"

    draft = record_page_shown(draft, "cursor-2", ["3"], None)

    assert draft.page_cursors == (None, "cursor-2")
    assert draft.shown_listing_ids == {"1", "2", "3"}
    assert next_page_request(draft) is None


def test_new_first_page_restarts_the_cursor_stack() -> None:
    draft = record_page_shown(draft_for(), None, ["1"], "cursor-2")
    draft = record_page_shown(draft, "cursor-2", ["2"], "cursor-3")

    draft = record_page_shown(draft, None, ["1"], "cursor-2")

    assert draft.page_cursors == (None,)


def test_new_draft_remembers_listings_already_shown_in_the_chat() -> None:
    previous = record_page_shown(draft_for(), None, ["1", "2"], "cursor-2")

    draft = start_draft(SearchRequest(query="sofa", page_cursor="stale"), previous)

    assert draft.shown_listing_ids == {"1", "2"}
    assert draft.request.page_cursor is None
    assert draft.page_cursors == () and draft.next_page_cursor is None
