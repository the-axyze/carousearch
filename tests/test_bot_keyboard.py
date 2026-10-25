from datetime import UTC, datetime

from carousearch.bot import messages
from carousearch.bot.keyboard import (
    SELECTED_MARK,
    Button,
    KeyboardLayout,
    build_keyboard_for_stage,
    describe_stage_position,
    render_stage_header,
    render_stage_text,
)
from carousearch.bot.state import Action, ActionKind, AwaitingInput, SearchSession, Stage, decode_action
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def session_at(
    stage: Stage,
    awaiting_input: AwaitingInput | None = None,
    return_to_summary: bool = False,
    next_page_cursor: str | None = None,
    **request_fields: object,
) -> SearchSession:
    """A session at `stage` whose request searches for "sofa" unless `request_fields` say otherwise."""
    return SearchSession(
        request=SearchRequest(**{"query": "sofa", **request_fields}),
        stage=stage,
        last_activity_at=NOW,
        awaiting_input=awaiting_input,
        return_to_summary=return_to_summary,
        next_page_cursor=next_page_cursor,
    )


def all_buttons(layout: KeyboardLayout) -> list[Button]:
    return [button for row in layout for button in row]


def labels(session: SearchSession) -> list[str]:
    return [button.text for button in all_buttons(build_keyboard_for_stage(session))]


def selected_labels(session: SearchSession) -> list[str]:
    return [label.removeprefix(SELECTED_MARK) for label in labels(session) if label.startswith(SELECTED_MARK)]


def action_of(session: SearchSession, label: str) -> Action:
    layout = build_keyboard_for_stage(session)
    matches = [button for button in all_buttons(layout) if button.text.removeprefix(SELECTED_MARK) == label]
    assert len(matches) == 1, f"expected one {label!r} button, found {len(matches)}"
    return decode_action(matches[0].callback_data)


# --- keyboards --------------------------------------------------------------------------------


def test_keywords_step_offers_next_only_once_there_are_keywords() -> None:
    waiting_for_first_keywords = SearchSession(request=None, stage=Stage.KEYWORDS, last_activity_at=NOW)

    assert labels(waiting_for_first_keywords) == ["Cancel"]
    assert labels(session_at(Stage.KEYWORDS)) == ["Next", "Cancel"]
    assert labels(session_at(Stage.KEYWORDS, return_to_summary=True)) == ["Next", "Back", "Cancel"]


def test_price_step_marks_any_by_default() -> None:
    session = session_at(Stage.PRICE)

    assert labels(session) == ["< 100", "100–300", "300–800", "800+", "✓ Any", "Type a range", "Back", "Cancel"]
    assert action_of(session, "< 100") == Action(ActionKind.SET_PRICE, "-100")
    assert action_of(session, "800+") == Action(ActionKind.SET_PRICE, "800-")
    assert action_of(session, "Any") == Action(ActionKind.SET_PRICE, "")
    assert action_of(session, "Type a range") == Action(ActionKind.TYPE_PRICE)


def test_price_step_marks_the_chosen_preset_or_a_typed_range() -> None:
    assert selected_labels(session_at(Stage.PRICE, price_min=100, price_max=300)) == ["100–300"]
    assert selected_labels(session_at(Stage.PRICE, price_min=150, price_max=450)) == ["Type a range"]


def test_waiting_for_typed_text_leaves_only_back_and_cancel() -> None:
    assert labels(session_at(Stage.PRICE, awaiting_input=AwaitingInput.PRICE_RANGE)) == ["Back", "Cancel"]
    assert labels(session_at(Stage.CATEGORY, awaiting_input=AwaitingInput.CATEGORY_ID)) == ["Back", "Cancel"]


def test_condition_step_marks_selected_toggles() -> None:
    nothing_selected = session_at(Stage.CONDITION)
    two_selected = session_at(Stage.CONDITION, conditions=[Condition.LIKE_NEW, Condition.WELL_USED])

    assert selected_labels(nothing_selected) == ["Any"]
    assert selected_labels(two_selected) == ["Like new", "Well used"]
    assert action_of(two_selected, "Lightly used") == Action(ActionKind.TOGGLE_CONDITION, "lightly_used")
    assert action_of(two_selected, "Any") == Action(ActionKind.CLEAR_CONDITIONS)
    assert action_of(two_selected, "Next") == Action(ActionKind.NEXT)


def test_delivery_step_marks_selected_toggles() -> None:
    session = session_at(Stage.DELIVERY, deal_methods=[DealMethod.MAILING])

    assert labels(session) == ["Meet-up", "✓ Mailing", "Any", "Next", "Back", "Cancel"]
    assert action_of(session, "Meet-up") == Action(ActionKind.TOGGLE_DEAL_METHOD, "meetup")
    assert selected_labels(session_at(Stage.DELIVERY)) == ["Any"]


def test_sort_step_marks_the_current_sort_and_leaves_out_nearby() -> None:
    session = session_at(Stage.SORT, sort=SortOrder.PRICE_LOW_TO_HIGH)

    assert labels(session) == ["Best match", "Recent", "✓ Price low→high", "Price high→low", "Back", "Cancel"]
    assert action_of(session, "Recent") == Action(ActionKind.SET_SORT, "recent")


def test_category_step_marks_common_and_other_categories() -> None:
    assert selected_labels(session_at(Stage.CATEGORY)) == ["Any"]
    assert selected_labels(session_at(Stage.CATEGORY, category="1793")) == ["Laptops"]
    assert selected_labels(session_at(Stage.CATEGORY, category="9999")) == ["Id 9999"]
    assert action_of(session_at(Stage.CATEGORY), "Mobile Phones") == Action(ActionKind.SET_CATEGORY, "5707")
    assert action_of(session_at(Stage.CATEGORY), "Other id…") == Action(ActionKind.TYPE_CATEGORY)


def test_summary_offers_search_and_an_edit_button_per_step() -> None:
    session = session_at(Stage.SUMMARY)

    assert labels(session) == [
        "Search", "Edit keywords", "Edit price", "Edit condition", "Edit delivery", "Edit sort", "Edit category",
        "Back", "Cancel",
    ]
    assert action_of(session, "Search") == Action(ActionKind.RUN_SEARCH)
    assert action_of(session, "Edit delivery") == Action(ActionKind.EDIT, "delivery")


def test_results_controls_offer_next_only_when_there_is_a_next_page() -> None:
    assert labels(session_at(Stage.RESULTS, next_page_cursor="cursor-2")) == ["Next 5", "Refine", "End"]
    assert labels(session_at(Stage.RESULTS)) == ["Refine", "End"]


# --- header and text -----------------------------------------------------------------------------


def test_header_shows_the_step_number_and_a_recap_of_earlier_steps() -> None:
    request_fields = {"query": "iphone 15", "price_max": 900, "conditions": [Condition.LIKE_NEW], "sort": SortOrder.RECENT}

    assert render_stage_header(session_at(Stage.KEYWORDS, **request_fields)) == "Step 1/6: Keywords"
    assert render_stage_header(session_at(Stage.PRICE, **request_fields)) == "Step 2/6: Price\nSo far: iphone 15"
    assert render_stage_header(session_at(Stage.DELIVERY, **request_fields)) == (
        "Step 4/6: Delivery\nSo far: iphone 15 · up to S$900 · Like new"
    )
    assert render_stage_header(session_at(Stage.CATEGORY, **request_fields)) == (
        "Step 6/6: Category\nSo far: iphone 15 · up to S$900 · Like new · any delivery · Recent"
    )
    assert render_stage_header(session_at(Stage.SUMMARY)) == "Summary"


def test_stage_text_asks_the_step_question() -> None:
    first_keywords = SearchSession(request=None, stage=Stage.KEYWORDS, last_activity_at=NOW)

    assert render_stage_text(first_keywords) == f"Step 1/6: Keywords\n\n{messages.KEYWORDS_QUESTION_TEXT}"
    assert "Current keywords: sofa." in render_stage_text(session_at(Stage.KEYWORDS))
    assert render_stage_text(session_at(Stage.PRICE)).endswith(messages.PRICE_QUESTION_TEXT)
    typing_price = session_at(Stage.PRICE, awaiting_input=AwaitingInput.PRICE_RANGE)
    assert render_stage_text(typing_price).endswith(messages.PRICE_INPUT_PROMPT_TEXT)
    typing_category = session_at(Stage.CATEGORY, awaiting_input=AwaitingInput.CATEGORY_ID)
    assert render_stage_text(typing_category).endswith(messages.CATEGORY_INPUT_PROMPT_TEXT)


def test_summary_text_lists_every_filter() -> None:
    session = session_at(Stage.SUMMARY, query="iphone 15", price_min=100, price_max=300, category="5707")

    assert render_stage_text(session).splitlines() == [
        "Summary",
        "",
        "Search: iphone 15",
        "Price: S$100 to S$300",
        "Condition: any",
        "Delivery: any",
        "Sort: Best match",
        "Category: Mobile Phones (5707)",
        "",
        messages.SUMMARY_HINT_TEXT,
    ]


def test_position_for_the_lock_reminder_and_status() -> None:
    assert describe_stage_position(session_at(Stage.PRICE)) == "at step 2/6 (Price)"
    assert describe_stage_position(session_at(Stage.SUMMARY)) == "at the summary"
    assert describe_stage_position(session_at(Stage.RESULTS)) == "showing results"


# --- request summary wording ------------------------------------------------------------------


def test_request_summary_of_a_full_request() -> None:
    request = SearchRequest(
        query="iphone 15",
        price_max=900,
        conditions=[Condition.LIKE_NEW, Condition.LIGHTLY_USED],
        deal_methods=[DealMethod.MEETUP, DealMethod.MAILING],
        sort=SortOrder.RECENT,
        category="5707",
        category_fields={"mobile_storage": "MOBILE_STORAGE_256_GB"},
        free_delivery_only=True,
    )

    assert messages.render_request_summary(request).splitlines() == [
        "Search: iphone 15",
        "Price: up to S$900",
        "Condition: Like new, Lightly used",
        "Delivery: Meet-up, Mailing",
        "Sort: Recent",
        "Category: Mobile Phones (5707)",
        "Category fields: mobile_storage=MOBILE_STORAGE_256_GB",
        "Free delivery only",
    ]


def test_request_summary_price_and_category_wording() -> None:
    assert "Price: S$800 and up" in messages.render_request_summary(SearchRequest(query="x", price_min=800))
    assert "Category: collection 9999" in messages.render_request_summary(SearchRequest(query="x", category="9999"))
