from carousearch.bot.keyboard import (
    SELECTED_MARK,
    Button,
    KeyboardLayout,
    build_filter_keyboard,
    build_results_keyboard,
    render_filter_summary,
)
from carousearch.bot.state import Action, ActionKind, decode_action
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder


def all_buttons(layout: KeyboardLayout) -> list[Button]:
    return [button for row in layout for button in row]


def selected_labels(request: SearchRequest) -> list[str]:
    return [
        button.text.removeprefix(SELECTED_MARK)
        for button in all_buttons(build_filter_keyboard(request))
        if button.text.startswith(SELECTED_MARK)
    ]


def button_with_label(layout: KeyboardLayout, label: str) -> Button:
    matches = [button for button in all_buttons(layout) if button.text.removeprefix(SELECTED_MARK) == label]
    assert len(matches) == 1, f"expected one {label!r} button, found {len(matches)}"
    return matches[0]


def test_default_request_marks_any_price_and_any_category() -> None:
    request = SearchRequest(query="sofa")

    assert selected_labels(request) == ["Any price", "Any category"]
    assert button_with_label(build_filter_keyboard(request), "Sort: Best match")


def test_selected_conditions_and_deal_methods_are_marked() -> None:
    request = SearchRequest(
        query="sofa", conditions=[Condition.LIKE_NEW, Condition.WELL_USED], deal_methods=[DealMethod.MEETUP]
    )

    assert selected_labels(request) == ["Any price", "Like new", "Well used", "Meet-up", "Any category"]


def test_price_preset_is_highlighted() -> None:
    request = SearchRequest(query="sofa", price_min=100, price_max=300)

    assert selected_labels(request) == ["100-300", "Any category"]


def test_custom_price_shows_on_the_custom_button() -> None:
    request = SearchRequest(query="sofa", price_min=150, price_max=450)
    layout = build_filter_keyboard(request)

    assert selected_labels(request) == ["150-450", "Any category"]
    assert decode_action(button_with_label(layout, "150-450").callback_data) == Action(ActionKind.ASK_PRICE)


def test_current_sort_is_shown() -> None:
    layout = build_filter_keyboard(SearchRequest(query="sofa", sort=SortOrder.PRICE_LOW_TO_HIGH))

    assert decode_action(button_with_label(layout, "Sort: Price low to high").callback_data).kind == ActionKind.NEXT_SORT


def test_common_and_other_categories_are_marked() -> None:
    assert "Laptops" in selected_labels(SearchRequest(query="x", category="1793"))
    assert "Id 9999" in selected_labels(SearchRequest(query="x", category="9999"))


def test_buttons_carry_the_matching_actions() -> None:
    layout = build_filter_keyboard(SearchRequest(query="sofa"))

    assert decode_action(button_with_label(layout, "Lightly used").callback_data) == Action(
        ActionKind.TOGGLE_CONDITION, "lightly_used"
    )
    assert decode_action(button_with_label(layout, "<100").callback_data) == Action(ActionKind.SET_PRICE, "-100")
    assert decode_action(button_with_label(layout, "800+").callback_data) == Action(ActionKind.SET_PRICE, "800-")
    assert decode_action(button_with_label(layout, "Mailing").callback_data) == Action(
        ActionKind.TOGGLE_DEAL_METHOD, "mailing"
    )
    assert decode_action(button_with_label(layout, "Mobile Phones").callback_data) == Action(
        ActionKind.SET_CATEGORY, "5707"
    )
    assert [button.text for button in layout[-1]] == ["Reset", "Search"]


def test_summary_of_a_default_request() -> None:
    assert render_filter_summary(SearchRequest(query="sofa")).splitlines() == [
        "Search: sofa",
        "Price: any",
        "Condition: any",
        "Delivery: any",
        "Sort: Best match",
        "Category: any",
        "",
        "Tap to change filters, then Search.",
    ]


def test_summary_of_a_full_request() -> None:
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

    assert render_filter_summary(request).splitlines()[:9] == [
        "Search: iphone 15",
        "Price: up to S$900",
        "Condition: Like new, Lightly used",
        "Delivery: Meet-up, Mailing",
        "Sort: Recent",
        "Category: Mobile Phones (5707)",
        "Category fields: mobile_storage=MOBILE_STORAGE_256_GB",
        "Free delivery only",
        "",
    ]


def test_summary_price_wording() -> None:
    assert "Price: S$800 and up" in render_filter_summary(SearchRequest(query="x", price_min=800))
    assert "Price: S$100 to S$300" in render_filter_summary(SearchRequest(query="x", price_min=100, price_max=300))
    assert "Category: collection 9999" in render_filter_summary(SearchRequest(query="x", category="9999"))


def test_results_keyboard_offers_next_only_when_there_is_a_next_page() -> None:
    assert [button.text for button in all_buttons(build_results_keyboard(True))] == ["Next 5", "Back to filters"]
    assert [button.text for button in all_buttons(build_results_keyboard(False))] == ["Back to filters"]
