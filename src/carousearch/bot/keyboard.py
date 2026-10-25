"""The screen for each session stage: its inline keyboard and the text above it. Pure logic: sessions
in, Buttons and strings out.

`Button` is a plain dataclass; handlers.to_inline_keyboard turns a layout into python-telegram-bot markup.
"""

from dataclasses import dataclass

from carousearch.bot import messages
from carousearch.bot.messages import COMMON_CATEGORIES, CONDITION_LABELS, DEAL_METHOD_LABELS, SORT_LABELS
from carousearch.bot.price_input import PriceRange, format_price_range
from carousearch.bot.state import (
    RESULTS_PER_PAGE,
    WIZARD_STEPS,
    Action,
    ActionKind,
    AwaitingInput,
    SearchSession,
    Stage,
    can_go_back,
    encode_action,
    require_request,
    step_number,
)
from carousearch.filters.schema import SearchRequest, SortOrder
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
    price_range: PriceRange


PRICE_PRESETS: tuple[PricePreset, ...] = (
    PricePreset("< 100", (None, 100)),
    PricePreset("100–300", (100, 300)),
    PricePreset("300–800", (300, 800)),
    PricePreset("800+", (800, None)),
)

# The sort step's choices. Nearby is left out: it needs a location filter.
SORT_CHOICES: tuple[SortOrder, ...] = (
    SortOrder.BEST_MATCH,
    SortOrder.RECENT,
    SortOrder.PRICE_LOW_TO_HIGH,
    SortOrder.PRICE_HIGH_TO_LOW,
)


# --- stage keyboards --------------------------------------------------------------------


def build_keyboard_for_stage(session: SearchSession) -> KeyboardLayout:
    match session.stage:
        case Stage.KEYWORDS:
            return keywords_keyboard(session)
        case Stage.PRICE:
            return price_keyboard(session)
        case Stage.CONDITION:
            return condition_keyboard(session)
        case Stage.DELIVERY:
            return delivery_keyboard(session)
        case Stage.SORT:
            return sort_keyboard(session)
        case Stage.CATEGORY:
            return category_keyboard(session)
        case Stage.SUMMARY:
            return summary_keyboard(session)
        case Stage.RESULTS:
            return results_keyboard(session)


def keywords_keyboard(session: SearchSession) -> KeyboardLayout:
    """Keywords are typed; Next keeps the current ones when there are some."""
    keep_row = [[_button("Next", ActionKind.NEXT)]] if session.request is not None else []
    return [*keep_row, _navigation_row(session)]


def price_keyboard(session: SearchSession) -> KeyboardLayout:
    if session.awaiting_input == AwaitingInput.PRICE_RANGE:
        return [_navigation_row(session)]
    request = require_request(session)
    current_range = (request.price_min, request.price_max)
    presets = [
        _button(preset.label, ActionKind.SET_PRICE, format_price_range(*preset.price_range),
                selected=current_range == preset.price_range)
        for preset in PRICE_PRESETS
    ]
    preset_ranges = [preset.price_range for preset in PRICE_PRESETS]
    is_typed_range = current_range != (None, None) and current_range not in preset_ranges
    other_choices = [
        _button("Any", ActionKind.SET_PRICE, "", selected=current_range == (None, None)),
        _button("Type a range", ActionKind.TYPE_PRICE, selected=is_typed_range),
    ]
    return [*_rows_of(presets, 2), other_choices, _navigation_row(session)]


def condition_keyboard(session: SearchSession) -> KeyboardLayout:
    request = require_request(session)
    toggles = [
        _button(label, ActionKind.TOGGLE_CONDITION, condition.value, selected=condition in request.conditions)
        for condition, label in CONDITION_LABELS.items()
    ]
    any_or_next = [_button("Any", ActionKind.CLEAR_CONDITIONS, selected=not request.conditions), _button("Next", ActionKind.NEXT)]
    return [*_rows_of(toggles, 3), any_or_next, _navigation_row(session)]


def delivery_keyboard(session: SearchSession) -> KeyboardLayout:
    request = require_request(session)
    toggles = [
        _button(label, ActionKind.TOGGLE_DEAL_METHOD, method.value, selected=method in request.deal_methods)
        for method, label in DEAL_METHOD_LABELS.items()
    ]
    any_or_next = [
        _button("Any", ActionKind.CLEAR_DEAL_METHODS, selected=not request.deal_methods),
        _button("Next", ActionKind.NEXT),
    ]
    return [toggles, any_or_next, _navigation_row(session)]


def sort_keyboard(session: SearchSession) -> KeyboardLayout:
    request = require_request(session)
    choices = [
        _button(SORT_LABELS[sort], ActionKind.SET_SORT, sort.value, selected=request.sort == sort)
        for sort in SORT_CHOICES
    ]
    return [*_rows_of(choices, 2), _navigation_row(session)]


def category_keyboard(session: SearchSession) -> KeyboardLayout:
    if session.awaiting_input == AwaitingInput.CATEGORY_ID:
        return [_navigation_row(session)]
    request = require_request(session)
    choices = [_button("Any", ActionKind.SET_CATEGORY, "", selected=request.category is None)]
    for category_id, label in COMMON_CATEGORIES.items():
        choices.append(_button(label, ActionKind.SET_CATEGORY, category_id, selected=request.category == category_id))
    is_other_id = request.category is not None and request.category not in COMMON_CATEGORIES
    other_label = f"Id {request.category}" if is_other_id else "Other id…"
    choices.append(_button(other_label, ActionKind.TYPE_CATEGORY, selected=is_other_id))
    return [*_rows_of(choices, 2), _navigation_row(session)]


def summary_keyboard(session: SearchSession) -> KeyboardLayout:
    edit_buttons = [_button(f"Edit {stage_title(step).lower()}", ActionKind.EDIT, step.value) for step in WIZARD_STEPS]
    return [[_button("Search", ActionKind.RUN_SEARCH)], *_rows_of(edit_buttons, 2), _navigation_row(session)]


def results_keyboard(session: SearchSession) -> KeyboardLayout:
    """The controls under a results page."""
    next_page = [_button(f"Next {RESULTS_PER_PAGE}", ActionKind.NEXT_PAGE)] if session.next_page_cursor else []
    return [[*next_page, _button("Refine", ActionKind.REFINE), _button("End", ActionKind.END)]]


# --- stage text ---------------------------------------------------------------------------


def stage_title(stage: Stage) -> str:
    return stage.value.capitalize()


def render_stage_text(session: SearchSession) -> str:
    """The whole message for the session's stage: the header, then the question it asks (or the summary)."""
    return f"{render_stage_header(session)}\n\n{_stage_question(session)}"


def render_stage_header(session: SearchSession) -> str:
    """ "Step 3/6: Condition" and a one-line recap of the steps before it; just the title after the wizard."""
    number = step_number(session.stage)
    if number is None:
        return stage_title(session.stage)
    title = f"Step {number}/{len(WIZARD_STEPS)}: {stage_title(session.stage)}"
    earlier_steps = WIZARD_STEPS[: number - 1]
    if not earlier_steps:
        return title
    request = require_request(session)
    recap = " · ".join(describe_step_choice(request, step) for step in earlier_steps)
    return f"{title}\nSo far: {recap}"


def describe_step_choice(request: SearchRequest, step: Stage) -> str:
    """What was chosen at one wizard step, as a recap fragment."""
    match step:
        case Stage.KEYWORDS:
            return request.query or "no keywords"
        case Stage.PRICE:
            return _named_if_any(messages.describe_price_range(request.price_min, request.price_max), "price")
        case Stage.CONDITION:
            return _named_if_any(messages.describe_choices(request.conditions, CONDITION_LABELS), "condition")
        case Stage.DELIVERY:
            return _named_if_any(messages.describe_choices(request.deal_methods, DEAL_METHOD_LABELS), "delivery")
        case Stage.SORT:
            return SORT_LABELS[request.sort]
        case Stage.CATEGORY:
            return _named_if_any(messages.describe_category(request.category), "category")
    raise ValueError(f"{step} is not a wizard step")


def describe_stage_position(session: SearchSession) -> str:
    """Where the session is, for the lock reminder and /status: "at step 2/6 (Price)", "at the summary"."""
    number = step_number(session.stage)
    if number is not None:
        return f"at step {number}/{len(WIZARD_STEPS)} ({stage_title(session.stage)})"
    if session.stage == Stage.SUMMARY:
        return "at the summary"
    return "showing results"


def _stage_question(session: SearchSession) -> str:
    match session.stage:
        case Stage.KEYWORDS:
            if session.request is None:
                return messages.KEYWORDS_QUESTION_TEXT
            return messages.render_keywords_change_question(session.request.query)
        case Stage.PRICE:
            if session.awaiting_input == AwaitingInput.PRICE_RANGE:
                return messages.PRICE_INPUT_PROMPT_TEXT
            return messages.PRICE_QUESTION_TEXT
        case Stage.CONDITION:
            return messages.CONDITION_QUESTION_TEXT
        case Stage.DELIVERY:
            return messages.DELIVERY_QUESTION_TEXT
        case Stage.SORT:
            return messages.SORT_QUESTION_TEXT
        case Stage.CATEGORY:
            if session.awaiting_input == AwaitingInput.CATEGORY_ID:
                return messages.CATEGORY_INPUT_PROMPT_TEXT
            return messages.CATEGORY_QUESTION_TEXT
        case Stage.SUMMARY:
            return f"{messages.render_request_summary(require_request(session))}\n\n{messages.SUMMARY_HINT_TEXT}"
        case Stage.RESULTS:
            return messages.RESULTS_HINT_TEXT


def _named_if_any(description: str, noun: str) -> str:
    """ "any" becomes "any price" (or condition, ...) so a recap fragment reads on its own."""
    return f"any {noun}" if description == "any" else description


# --- cards and saved searches -----------------------------------------------------------


def build_listing_card_keyboard(listing_id: str) -> KeyboardLayout:
    return [[_button("Details", ActionKind.SHOW_DETAILS, listing_id)]]


def build_saved_searches_keyboard(saved_searches: list[SavedSearch]) -> KeyboardLayout:
    return [[_button(f"Load: {saved.name}", ActionKind.LOAD_SAVED_SEARCH, str(saved.id))] for saved in saved_searches]


# --- helpers ----------------------------------------------------------------------------


def _navigation_row(session: SearchSession) -> list[Button]:
    """Back (when there is somewhere to go back to) and Cancel, under every wizard step and the summary."""
    back = [_button("Back", ActionKind.BACK)] if can_go_back(session) else []
    return [*back, _button("Cancel", ActionKind.CANCEL)]


def _rows_of(buttons: list[Button], width: int) -> KeyboardLayout:
    return [buttons[start : start + width] for start in range(0, len(buttons), width)]


def _button(text: str, kind: ActionKind, value: str = "", selected: bool = False) -> Button:
    label = SELECTED_MARK + text if selected else text
    return Button(label, encode_action(Action(kind, value)))
