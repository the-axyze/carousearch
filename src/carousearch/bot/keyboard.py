"""Inline keyboard layouts and the filter summary text. Pure logic: requests in, Buttons and strings out.

`Button` is a plain dataclass; handlers.py turns layouts into python-telegram-bot markup.
"""

from dataclasses import dataclass

from carousearch.bot.state import RESULTS_PER_PAGE, Action, ActionKind, encode_action, format_price_range
from carousearch.filters.payload import format_price
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder
from carousearch.storage import SavedSearch

SELECTED_MARK = "✓ "


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str


KeyboardLayout = list[list[Button]]


@dataclass(frozen=True)
class PricePreset:
    label: str
    price_min: float | None
    price_max: float | None


PRICE_PRESETS: tuple[PricePreset, ...] = (
    PricePreset("<100", None, 100),
    PricePreset("100-300", 100, 300),
    PricePreset("300-800", 300, 800),
    PricePreset("800+", 800, None),
)

CONDITION_LABELS: dict[Condition, str] = {
    Condition.BRAND_NEW: "Brand new",
    Condition.LIKE_NEW: "Like new",
    Condition.LIGHTLY_USED: "Lightly used",
    Condition.WELL_USED: "Well used",
    Condition.HEAVILY_USED: "Heavily used",
}

DEAL_METHOD_LABELS: dict[DealMethod, str] = {DealMethod.MEETUP: "Meet-up", DealMethod.MAILING: "Mailing"}

SORT_LABELS: dict[SortOrder, str] = {
    SortOrder.BEST_MATCH: "Best match",
    SortOrder.RECENT: "Recent",
    SortOrder.PRICE_LOW_TO_HIGH: "Price low to high",
    SortOrder.PRICE_HIGH_TO_LOW: "Price high to low",
    SortOrder.NEARBY: "Nearby",
}

# Carousell collection id -> short button label. A hand-picked subset of the category tree
# in CAROUSELL_ACCESS_NOTES.md section 4.8; any other id can be typed in through "Other id".
COMMON_CATEGORIES: dict[str, str] = {
    "5707": "Mobile Phones",
    "1793": "Laptops",
    "1241": "Tablets",
    "13": "Furniture",
    "264": "Video Gaming",
    "207": "Audio",
}


# --- filter panel ----------------------------------------------------------------


def build_filter_keyboard(request: SearchRequest) -> KeyboardLayout:
    return [
        *_price_rows(request),
        *_condition_rows(request),
        _deal_method_row(request),
        [_button(f"Sort: {SORT_LABELS[request.sort]}", ActionKind.NEXT_SORT)],
        *_category_rows(request),
        [_button("Reset", ActionKind.RESET), _button("Search", ActionKind.RUN_SEARCH)],
    ]


def render_filter_summary(request: SearchRequest) -> str:
    lines = [
        f"Search: {request.query or '(no keywords)'}",
        f"Price: {describe_price_range(request.price_min, request.price_max)}",
        f"Condition: {_describe_choices(request.conditions, CONDITION_LABELS)}",
        f"Delivery: {_describe_choices(request.deal_methods, DEAL_METHOD_LABELS)}",
        f"Sort: {SORT_LABELS[request.sort]}",
        f"Category: {describe_category(request.category)}",
    ]
    if request.category_fields:
        fields = ", ".join(f"{name}={value}" for name, value in request.category_fields.items())
        lines.append(f"Category fields: {fields}")
    if request.buyer_protection_only:
        lines.append("Buyer Protection only")
    if request.free_delivery_only:
        lines.append("Free delivery only")
    lines.extend(["", "Tap to change filters, then Search."])
    return "\n".join(lines)


def describe_price_range(price_min: float | None, price_max: float | None) -> str:
    if price_min is None and price_max is None:
        return "any"
    if price_min is None:
        return f"up to S${format_price(price_max)}"
    if price_max is None:
        return f"S${format_price(price_min)} and up"
    return f"S${format_price(price_min)} to S${format_price(price_max)}"


def describe_category(category: str | None) -> str:
    if category is None:
        return "any"
    if category in COMMON_CATEGORIES:
        return f"{COMMON_CATEGORIES[category]} ({category})"
    return f"collection {category}"


def _price_rows(request: SearchRequest) -> KeyboardLayout:
    current_range = (request.price_min, request.price_max)
    preset_ranges = {(preset.price_min, preset.price_max) for preset in PRICE_PRESETS}
    is_custom = current_range != (None, None) and current_range not in preset_ranges
    buttons = [_button("Any price", ActionKind.SET_PRICE, "", selected=current_range == (None, None))]
    for preset in PRICE_PRESETS:
        is_selected = current_range == (preset.price_min, preset.price_max)
        buttons.append(
            _button(preset.label, ActionKind.SET_PRICE, format_price_range(preset.price_min, preset.price_max), is_selected)
        )
    custom_label = format_price_range(*current_range) if is_custom else "Custom…"
    buttons.append(_button(custom_label, ActionKind.ASK_PRICE, selected=is_custom))
    return [buttons[:3], buttons[3:]]


def _condition_rows(request: SearchRequest) -> KeyboardLayout:
    buttons = [
        _button(label, ActionKind.TOGGLE_CONDITION, condition.value, selected=condition in request.conditions)
        for condition, label in CONDITION_LABELS.items()
    ]
    return [buttons[:3], buttons[3:]]


def _deal_method_row(request: SearchRequest) -> list[Button]:
    return [
        _button(label, ActionKind.TOGGLE_DEAL_METHOD, method.value, selected=method in request.deal_methods)
        for method, label in DEAL_METHOD_LABELS.items()
    ]


def _category_rows(request: SearchRequest) -> KeyboardLayout:
    buttons = [_button("Any category", ActionKind.SET_CATEGORY, "", selected=request.category is None)]
    for category_id, label in COMMON_CATEGORIES.items():
        buttons.append(_button(label, ActionKind.SET_CATEGORY, category_id, selected=request.category == category_id))
    is_other = request.category is not None and request.category not in COMMON_CATEGORIES
    other_label = f"Id {request.category}" if is_other else "Other id…"
    buttons.append(_button(other_label, ActionKind.ASK_CATEGORY, selected=is_other))
    return [buttons[:4], buttons[4:]]


def _describe_choices[Choice](selected: list[Choice], labels: dict[Choice, str]) -> str:
    if not selected:
        return "any"
    return ", ".join(labels[choice] for choice in selected)


# --- results and saved searches -----------------------------------------------------


def build_listing_card_keyboard(listing_id: str) -> KeyboardLayout:
    return [[_button("Details", ActionKind.SHOW_DETAILS, listing_id)]]


def build_results_keyboard(has_next_page: bool) -> KeyboardLayout:
    buttons = [_button(f"Next {RESULTS_PER_PAGE}", ActionKind.NEXT_PAGE)] if has_next_page else []
    buttons.append(_button("Back to filters", ActionKind.BACK_TO_FILTERS))
    return [buttons]


def build_saved_searches_keyboard(saved_searches: list[SavedSearch]) -> KeyboardLayout:
    return [[_button(f"Load: {saved.name}", ActionKind.LOAD_SAVED_SEARCH, str(saved.id))] for saved in saved_searches]


def _button(text: str, kind: ActionKind, value: str = "", selected: bool = False) -> Button:
    label = SELECTED_MARK + text if selected else text
    return Button(label, encode_action(Action(kind, value)))
