"""A chat's search session: which step of the search wizard it is at, the button actions that move it
along, and result paging. Pure logic, no Telegram imports.

A `SearchSession` holds the `SearchRequest` being built and the `Stage` the chat is at. A button
press is an `Action`, sent to Telegram as a compact callback_data string (`encode_action`, read back
with `decode_action`) and applied with `apply_action`; typed text goes through `apply_text_input`.
Both return a new session; nothing is changed in place.

The stages run KEYWORDS, PRICE, CONDITION, DELIVERY, SORT, CATEGORY (the numbered steps), then
SUMMARY and RESULTS. Adding a step means a Stage member in the right place, its keyboard in
keyboard.py, its case in `apply_action` (or `apply_text_input`), and a test.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from carousearch.bot import messages
from carousearch.bot.price_input import parse_price_range
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

# Telegram rejects callback_data longer than 64 bytes.
MAX_CALLBACK_DATA_BYTES = 64
RESULTS_PER_PAGE = 5
SESSION_IDLE_TIMEOUT = timedelta(minutes=30)


class Stage(StrEnum):
    """Where a session is, in the order the stages come. The value doubles as the title ("Price")."""

    KEYWORDS = "keywords"
    PRICE = "price"
    CONDITION = "condition"
    DELIVERY = "delivery"
    SORT = "sort"
    CATEGORY = "category"
    SUMMARY = "summary"
    RESULTS = "results"


# The numbered steps of the wizard ("Step 2/6"): every stage before the summary.
WIZARD_STEPS: tuple[Stage, ...] = tuple(stage for stage in Stage if stage not in (Stage.SUMMARY, Stage.RESULTS))


class ActionKind(StrEnum):
    """What a button does. Values are short because they travel inside callback_data."""

    # Moving through the wizard.
    NEXT = "nt"
    BACK = "bk"
    CANCEL = "cn"
    EDIT = "ed"  # value: the Stage to edit, from the summary
    # Answers on the wizard steps.
    SET_PRICE = "pr"  # value: a price range such as "100-300", "-100", "800-"; "" means any price
    TYPE_PRICE = "pa"  # wait for a typed price range
    TOGGLE_CONDITION = "co"  # value: a Condition value
    CLEAR_CONDITIONS = "cx"
    TOGGLE_DEAL_METHOD = "dm"  # value: a DealMethod value
    CLEAR_DEAL_METHODS = "dx"
    SET_SORT = "so"  # value: a SortOrder value
    SET_CATEGORY = "ca"  # value: a Carousell collection id; "" means any category
    TYPE_CATEGORY = "cq"  # wait for a typed collection id
    # The summary and the results.
    RUN_SEARCH = "go"
    NEXT_PAGE = "nx"
    REFINE = "rf"
    END = "en"
    # Buttons that work whether or not a session is active.
    SHOW_DETAILS = "dt"  # value: a listing id
    LOAD_SAVED_SEARCH = "ls"  # value: a saved search id


class AwaitingInput(StrEnum):
    """Free text a step has asked for, instead of a button: the next plain message is read as this."""

    PRICE_RANGE = "price_range"
    CATEGORY_ID = "category_id"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    value: str = ""


@dataclass(frozen=True)
class SearchSession:
    # None only while a session started without keywords is waiting for the first ones.
    request: SearchRequest | None
    stage: Stage
    last_activity_at: datetime
    # True when the step was opened with an Edit button on the summary: finishing it goes back there.
    return_to_summary: bool = False
    awaiting_input: AwaitingInput | None = None
    # The cursor used to fetch each results page shown so far, oldest first. None is the first page.
    page_cursors: tuple[str | None, ...] = ()
    # Carousell's cursor for the page after the last one shown; None when there are no more pages.
    next_page_cursor: str | None = None
    # The bot message showing the current stage (or the results controls). Only its buttons are live.
    stage_message_id: int | None = None

    @property
    def query(self) -> str | None:
        return self.request.query if self.request is not None else None


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


# --- starting and expiring sessions ---------------------------------------------------


def start_session(query: str | None, now: datetime) -> SearchSession:
    """A new session: at the price step when keywords came with it, otherwise asking for them."""
    if query is None or not query.strip():
        return SearchSession(request=None, stage=Stage.KEYWORDS, last_activity_at=now)
    return SearchSession(request=SearchRequest(query=query), stage=Stage.PRICE, last_activity_at=now)


def start_session_at_summary(request: SearchRequest, now: datetime) -> SearchSession:
    """A session for a ready-made request (a saved search, later the LLM prompt parser) that skips the wizard."""
    return SearchSession(
        request=request.model_copy(update={"page_cursor": None}), stage=Stage.SUMMARY, last_activity_at=now
    )


def is_expired(session: SearchSession, now: datetime, idle_timeout: timedelta) -> bool:
    return now - session.last_activity_at > idle_timeout


def require_request(session: SearchSession) -> SearchRequest:
    """The session's request. Every stage after KEYWORDS has one, since keywords come first."""
    if session.request is None:
        raise ValueError("the session has no keywords yet")
    return session.request


# --- the order of stages ------------------------------------------------------------------


def step_number(stage: Stage) -> int | None:
    """1 for the first wizard step; None for the summary and the results."""
    if stage not in WIZARD_STEPS:
        return None
    return WIZARD_STEPS.index(stage) + 1


def next_stage(stage: Stage) -> Stage:
    stages = list(Stage)
    position = stages.index(stage)
    if position == len(stages) - 1:
        raise ValueError(f"there is no stage after {stage}")
    return stages[position + 1]


def previous_stage(stage: Stage) -> Stage:
    stages = list(Stage)
    position = stages.index(stage)
    if position == 0:
        raise ValueError(f"there is no stage before {stage}")
    return stages[position - 1]


def can_go_back(session: SearchSession) -> bool:
    """Back works everywhere except on the first step (unless that step was opened from the summary)."""
    return session.awaiting_input is not None or session.return_to_summary or session.stage != Stage.KEYWORDS


def expects_text_input(session: SearchSession) -> bool:
    """Whether a plain message is an answer to the current step (keywords or a typed value)."""
    return session.stage == Stage.KEYWORDS or session.awaiting_input is not None


# --- button presses -------------------------------------------------------------------------


def apply_action(session: SearchSession, action: Action, now: datetime) -> SearchSession | None:
    """The session after a button press, or None when the press ends it (Cancel, End).

    Raises ValueError for a value that does not parse, or for a button that belongs to another
    stage (an old message's button, or a double tap). Run Search and Next page are not handled
    here: they need a Carousell call first, and then `record_page_shown`.
    """
    match action.kind:
        case ActionKind.CANCEL | ActionKind.END:
            return None
        case ActionKind.BACK:
            updated = go_back(session)
        case ActionKind.NEXT:
            _require_stage(session, Stage.KEYWORDS, Stage.CONDITION, Stage.DELIVERY)
            updated = finish_step(session)
        case ActionKind.EDIT:
            _require_stage(session, Stage.SUMMARY)
            updated = edit_step(session, Stage(action.value))
        case ActionKind.REFINE:
            _require_stage(session, Stage.RESULTS)
            updated = _moved_to(session, Stage.SUMMARY)
        case ActionKind.SET_PRICE:
            _require_stage(session, Stage.PRICE)
            updated = finish_step(_with_price_range(session, action.value))
        case ActionKind.TYPE_PRICE:
            _require_stage(session, Stage.PRICE)
            updated = replace(session, awaiting_input=AwaitingInput.PRICE_RANGE)
        case ActionKind.TOGGLE_CONDITION:
            _require_stage(session, Stage.CONDITION)
            updated = _with_condition_toggled(session, Condition(action.value))
        case ActionKind.CLEAR_CONDITIONS:
            _require_stage(session, Stage.CONDITION)
            updated = _with_filters(session, conditions=[])
        case ActionKind.TOGGLE_DEAL_METHOD:
            _require_stage(session, Stage.DELIVERY)
            updated = _with_deal_method_toggled(session, DealMethod(action.value))
        case ActionKind.CLEAR_DEAL_METHODS:
            _require_stage(session, Stage.DELIVERY)
            updated = _with_filters(session, deal_methods=[])
        case ActionKind.SET_SORT:
            _require_stage(session, Stage.SORT)
            updated = finish_step(_with_filters(session, sort=SortOrder(action.value)))
        case ActionKind.SET_CATEGORY:
            _require_stage(session, Stage.CATEGORY)
            updated = finish_step(_with_filters(session, category=action.value or None))
        case ActionKind.TYPE_CATEGORY:
            _require_stage(session, Stage.CATEGORY)
            updated = replace(session, awaiting_input=AwaitingInput.CATEGORY_ID)
        case _:
            raise ValueError(f"{action.kind.name} is not a session action")
    return replace(updated, last_activity_at=now)


def finish_step(session: SearchSession) -> SearchSession:
    """Move on once the current step is answered: back to the summary when the step was opened from
    there, otherwise to the next stage."""
    require_request(session)
    if session.return_to_summary:
        return _moved_to(session, Stage.SUMMARY)
    return _moved_to(session, next_stage(session.stage))


def go_back(session: SearchSession) -> SearchSession:
    """Out of typed input back to the step's buttons; otherwise back to the summary when the step was
    opened from there, or else to the previous stage."""
    if session.awaiting_input is not None:
        return replace(session, awaiting_input=None)
    if session.return_to_summary:
        return _moved_to(session, Stage.SUMMARY)
    return _moved_to(session, previous_stage(session.stage))


def edit_step(session: SearchSession, step: Stage) -> SearchSession:
    """Open one wizard step from the summary; answering it returns to the summary."""
    if step not in WIZARD_STEPS:
        raise ValueError(f"{step} is not a wizard step")
    return replace(session, stage=step, return_to_summary=True, awaiting_input=None)


# --- typed text -------------------------------------------------------------------------------


def apply_text_input(session: SearchSession, text: str, now: datetime) -> tuple[SearchSession, str | None]:
    """Read typed text as what the current step asked for: keywords, a price range or a collection id.

    Returns the updated session and None, or the session (unchanged but for its activity time) and
    the problem to show the user. Raises ValueError if the session is not waiting for text.
    """
    active_session = replace(session, last_activity_at=now)
    if active_session.stage == Stage.KEYWORDS:
        return _read_keywords(active_session, text)
    match active_session.awaiting_input:
        case AwaitingInput.PRICE_RANGE:
            return _read_price_range(active_session, text)
        case AwaitingInput.CATEGORY_ID:
            return _read_category_id(active_session, text)
    raise ValueError("the session is not waiting for text input")


def _read_keywords(session: SearchSession, text: str) -> tuple[SearchSession, str | None]:
    keywords = text.strip()
    if not keywords:
        return session, messages.KEYWORDS_MISSING_TEXT
    if session.request is None:
        return finish_step(replace(session, request=SearchRequest(query=keywords))), None
    return finish_step(_with_filters(session, query=keywords)), None


def _read_price_range(session: SearchSession, text: str) -> tuple[SearchSession, str | None]:
    price_range = parse_price_range(text)
    if price_range is None:
        return session, messages.render_price_range_problem(text)
    price_min, price_max = price_range
    return finish_step(_with_filters(session, price_min=price_min, price_max=price_max)), None


def _read_category_id(session: SearchSession, text: str) -> tuple[SearchSession, str | None]:
    category_id = text.strip()
    if not category_id.isdigit():
        return session, messages.CATEGORY_ID_PROBLEM_TEXT
    return finish_step(_with_filters(session, category=category_id)), None


# --- result pages -------------------------------------------------------------------------------


def first_page_request(session: SearchSession) -> SearchRequest:
    return _page_request(require_request(session), None)


def next_page_request(session: SearchSession) -> SearchRequest | None:
    """The request for the page after the last one shown, or None when there is none."""
    if session.next_page_cursor is None:
        return None
    return _page_request(require_request(session), session.next_page_cursor)


def record_page_shown(
    session: SearchSession, page_cursor: str | None, next_page_cursor: str | None, now: datetime
) -> SearchSession:
    """The session showing a results page. A `page_cursor` of None is a first page and restarts the cursor stack."""
    page_cursors = (None,) if page_cursor is None else (*session.page_cursors, page_cursor)
    return replace(
        _moved_to(session, Stage.RESULTS),
        page_cursors=page_cursors,
        next_page_cursor=next_page_cursor,
        last_activity_at=now,
    )


# --- helpers ----------------------------------------------------------------------------------


def _require_stage(session: SearchSession, *stages: Stage) -> None:
    if session.stage not in stages:
        raise ValueError(f"that button belongs to {', '.join(stages)}, but the session is at {session.stage}")


def _moved_to(session: SearchSession, stage: Stage) -> SearchSession:
    return replace(session, stage=stage, return_to_summary=False, awaiting_input=None)


def _with_filters(session: SearchSession, **changes: object) -> SearchSession:
    """The session with its request changed and validated again (model_copy alone skips validation)."""
    request = require_request(session)
    updated_request = SearchRequest.model_validate({**request.model_dump(), **changes, "page_cursor": None})
    return replace(session, request=updated_request)


def _with_price_range(session: SearchSession, price_range_text: str) -> SearchSession:
    price_range = parse_price_range(price_range_text)
    if price_range is None:
        raise ValueError(f"{price_range_text!r} is not a price range")
    price_min, price_max = price_range
    return _with_filters(session, price_min=price_min, price_max=price_max)


def _with_condition_toggled(session: SearchSession, condition: Condition) -> SearchSession:
    conditions = _toggled(require_request(session).conditions, condition, list(Condition))
    return _with_filters(session, conditions=conditions)


def _with_deal_method_toggled(session: SearchSession, deal_method: DealMethod) -> SearchSession:
    deal_methods = _toggled(require_request(session).deal_methods, deal_method, list(DealMethod))
    return _with_filters(session, deal_methods=deal_methods)


def _page_request(request: SearchRequest, page_cursor: str | None) -> SearchRequest:
    return request.model_copy(update={"page_size": RESULTS_PER_PAGE, "page_cursor": page_cursor})


def _toggled[Item](selected: list[Item], item: Item, canonical_order: list[Item]) -> list[Item]:
    """`selected` with `item` added or removed, in canonical order so the display stays stable."""
    updated = set(selected)
    if item in updated:
        updated.remove(item)
    else:
        updated.add(item)
    return [candidate for candidate in canonical_order if candidate in updated]
