"""Per-chat search state and the keyboard actions that change it. Pure logic, no Telegram imports.

A `ChatDraft` wraps the `SearchRequest` being edited. Every button press is an `Action`, sent to
Telegram as a compact `callback_data` string (`encode_action`) and read back with `decode_action`.
Filter actions go through `apply_action`; result paging goes through `record_page_shown`.
"""

import math
from dataclasses import dataclass, replace
from enum import StrEnum

from carousearch.filters.payload import format_price
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

# Telegram rejects callback_data longer than 64 bytes.
MAX_CALLBACK_DATA_BYTES = 64
RESULTS_PER_PAGE = 5

# The sorts the Sort button cycles through. NEARBY is left out: it needs a location filter.
SORT_CYCLE: tuple[SortOrder, ...] = (
    SortOrder.BEST_MATCH,
    SortOrder.RECENT,
    SortOrder.PRICE_LOW_TO_HIGH,
    SortOrder.PRICE_HIGH_TO_LOW,
)


class ActionKind(StrEnum):
    """What a button does. Values are short because they travel inside callback_data."""

    SET_PRICE = "pr"  # value: a price range such as "100-300", "-100", "800-"; "" means any price
    ASK_PRICE = "pa"  # ask the user to type a price range
    TOGGLE_CONDITION = "co"  # value: a Condition value
    TOGGLE_DEAL_METHOD = "dm"  # value: a DealMethod value
    NEXT_SORT = "so"
    SET_CATEGORY = "ca"  # value: a Carousell collection id; "" means any category
    ASK_CATEGORY = "cq"  # ask the user to type a collection id
    RESET = "rs"
    RUN_SEARCH = "go"
    NEXT_PAGE = "nx"
    BACK_TO_FILTERS = "bf"
    SHOW_DETAILS = "dt"  # value: a listing id
    LOAD_SAVED_SEARCH = "ls"  # value: a saved search id


# Actions that edit the draft's filters and are handled by `apply_action`.
FILTER_ACTION_KINDS = frozenset(
    {
        ActionKind.SET_PRICE,
        ActionKind.ASK_PRICE,
        ActionKind.TOGGLE_CONDITION,
        ActionKind.TOGGLE_DEAL_METHOD,
        ActionKind.NEXT_SORT,
        ActionKind.SET_CATEGORY,
        ActionKind.ASK_CATEGORY,
        ActionKind.RESET,
    }
)


class AwaitingInput(StrEnum):
    """Free text the bot has asked for; the next plain message is read as this."""

    PRICE_RANGE = "price_range"
    CATEGORY_ID = "category_id"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    value: str = ""


@dataclass(frozen=True)
class ChatDraft:
    request: SearchRequest
    # The cursor used to fetch each results page shown so far, oldest first. None is the first page.
    page_cursors: tuple[str | None, ...] = ()
    # Carousell's cursor for the page after the last one shown; None when there are no more pages.
    next_page_cursor: str | None = None
    # Every listing id shown in this chat, carried over from one draft to the next.
    shown_listing_ids: frozenset[str] = frozenset()
    awaiting_input: AwaitingInput | None = None


# --- callback_data encoding ------------------------------------------------------


def encode_action(action: Action) -> str:
    callback_data = f"{action.kind.value}:{action.value}"
    if len(callback_data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        raise ValueError(f"callback_data {callback_data!r} is longer than {MAX_CALLBACK_DATA_BYTES} bytes")
    return callback_data


def decode_action(callback_data: str) -> Action:
    kind_code, separator, value = callback_data.partition(":")
    if not separator:
        raise ValueError(f"callback_data {callback_data!r} has no ':' separator")
    try:
        kind = ActionKind(kind_code)
    except ValueError:
        raise ValueError(f"callback_data {callback_data!r} has an unknown action {kind_code!r}") from None
    return Action(kind, value)


# --- starting a draft and editing its filters ---------------------------------------


def start_draft(request: SearchRequest, previous_draft: ChatDraft | None = None) -> ChatDraft:
    """A fresh draft for `request` that remembers which listings this chat has already been shown."""
    shown_listing_ids = previous_draft.shown_listing_ids if previous_draft else frozenset()
    return ChatDraft(request=request.model_copy(update={"page_cursor": None}), shown_listing_ids=shown_listing_ids)


def apply_action(draft: ChatDraft, action: Action) -> ChatDraft:
    """Apply one filter action. Raises ValueError for a bad value or a non-filter action."""
    request = draft.request
    match action.kind:
        case ActionKind.SET_PRICE:
            price_min, price_max = parse_price_range(action.value)
            return _with_request(draft, _changed(request, price_min=price_min, price_max=price_max))
        case ActionKind.TOGGLE_CONDITION:
            conditions = _toggled(request.conditions, Condition(action.value), list(Condition))
            return _with_request(draft, _changed(request, conditions=conditions))
        case ActionKind.TOGGLE_DEAL_METHOD:
            deal_methods = _toggled(request.deal_methods, DealMethod(action.value), list(DealMethod))
            return _with_request(draft, _changed(request, deal_methods=deal_methods))
        case ActionKind.NEXT_SORT:
            return _with_request(draft, _changed(request, sort=next_sort(request.sort)))
        case ActionKind.SET_CATEGORY:
            return _with_request(draft, _changed(request, category=action.value or None))
        case ActionKind.RESET:
            return _with_request(draft, reset_request(request))
        case ActionKind.ASK_PRICE:
            return replace(draft, awaiting_input=AwaitingInput.PRICE_RANGE)
        case ActionKind.ASK_CATEGORY:
            return replace(draft, awaiting_input=AwaitingInput.CATEGORY_ID)
    raise ValueError(f"{action.kind.name} is not a filter action")


def apply_text_input(draft: ChatDraft, text: str) -> ChatDraft:
    """Read `text` as the input the draft is waiting for. Raises ValueError if it does not parse."""
    match draft.awaiting_input:
        case AwaitingInput.PRICE_RANGE:
            return apply_action(draft, Action(ActionKind.SET_PRICE, text))
        case AwaitingInput.CATEGORY_ID:
            return apply_action(draft, Action(ActionKind.SET_CATEGORY, text.strip()))
    raise ValueError("the draft is not waiting for text input")


def reset_request(request: SearchRequest) -> SearchRequest:
    """All filters back to their defaults. The query is kept (and the category, if there is no query)."""
    if request.query is None:
        return SearchRequest(category=request.category)
    return SearchRequest(query=request.query)


def next_sort(current: SortOrder) -> SortOrder:
    if current not in SORT_CYCLE:
        return SORT_CYCLE[0]
    return SORT_CYCLE[(SORT_CYCLE.index(current) + 1) % len(SORT_CYCLE)]


# --- price ranges -------------------------------------------------------------------


def format_price_range(price_min: float | None, price_max: float | None) -> str:
    """The inverse of parse_price_range: (100, 300) -> "100-300", (None, 100) -> "-100", (None, None) -> ""."""
    if price_min is None and price_max is None:
        return ""
    low = format_price(price_min) if price_min is not None else ""
    high = format_price(price_max) if price_max is not None else ""
    return f"{low}-{high}"


def parse_price_range(text: str) -> tuple[float | None, float | None]:
    """Read "100-500", "-500", "100-", "<500", "800+", "S$1,000-2,000", or "" / "any" for no limit."""
    cleaned = text.strip().lower().replace("s$", "").replace("$", "").replace(",", "").replace(" ", "")
    if cleaned in ("", "any"):
        return None, None
    if cleaned.startswith("<"):
        return None, _parse_price(cleaned[1:], text)
    if cleaned.endswith("+"):
        return _parse_price(cleaned[:-1], text), None
    low_text, separator, high_text = cleaned.partition("-")
    if not separator or (not low_text and not high_text):
        raise ValueError(f"could not read {text!r} as a price range; try 100-500, <300 or 800+")
    price_min = _parse_price(low_text, text) if low_text else None
    price_max = _parse_price(high_text, text) if high_text else None
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ValueError(f"the lower price in {text!r} is above the upper price")
    return price_min, price_max


# --- result pages -------------------------------------------------------------------


def first_page_request(draft: ChatDraft) -> SearchRequest:
    return _page_request(draft, None)


def next_page_request(draft: ChatDraft) -> SearchRequest | None:
    """The request for the page after the last one shown, or None when there is none."""
    if draft.next_page_cursor is None:
        return None
    return _page_request(draft, draft.next_page_cursor)


def record_page_shown(
    draft: ChatDraft, page_cursor: str | None, listing_ids: list[str], next_page_cursor: str | None
) -> ChatDraft:
    """Remember a results page. A `page_cursor` of None is a first page and restarts the cursor stack.

    Any pending request for typed input is dropped: the user moved on to the results.
    """
    page_cursors = (None,) if page_cursor is None else (*draft.page_cursors, page_cursor)
    return replace(
        draft,
        page_cursors=page_cursors,
        next_page_cursor=next_page_cursor,
        shown_listing_ids=draft.shown_listing_ids | set(listing_ids),
        awaiting_input=None,
    )


# --- helpers ------------------------------------------------------------------------


def _with_request(draft: ChatDraft, request: SearchRequest) -> ChatDraft:
    """A draft with new filters. Paging restarts, because old cursors belong to the old filters."""
    return replace(draft, request=request, page_cursors=(), next_page_cursor=None, awaiting_input=None)


def _changed(request: SearchRequest, **changes: object) -> SearchRequest:
    """A copy of `request` with `changes`, validated again (model_copy alone skips validation)."""
    return SearchRequest.model_validate({**request.model_dump(), **changes, "page_cursor": None})


def _page_request(draft: ChatDraft, page_cursor: str | None) -> SearchRequest:
    return draft.request.model_copy(update={"page_size": RESULTS_PER_PAGE, "page_cursor": page_cursor})


def _toggled[Item](selected: list[Item], item: Item, canonical_order: list[Item]) -> list[Item]:
    """`selected` with `item` added or removed, in canonical order so the display stays stable."""
    updated = set(selected)
    if item in updated:
        updated.remove(item)
    else:
        updated.add(item)
    return [candidate for candidate in canonical_order if candidate in updated]


def _parse_price(number_text: str, original_text: str) -> float:
    try:
        price = float(number_text)
    except ValueError:
        raise ValueError(f"could not read {original_text!r} as a price range; try 100-500, <300 or 800+") from None
    if not math.isfinite(price) or price < 0:
        raise ValueError(f"{original_text!r} is not a valid price")
    return price
