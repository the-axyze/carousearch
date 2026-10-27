"""What Ask mode's templates show: the form, a run's progress, its answer and its failure. Pure functions, no I/O.

As in view_models.py, templates only print these fields and pick CSS classes from them. A kept card is built from
the Listing a search returned (Carousell's data) plus the model's reason; a kept id that no search returned keeps
the model's title, price and link, with a warning.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from carousearch.agent.result import AgentAnswer, DroppedListing, KeptListing, ToolCall
from carousearch.agent.settings import AGENT_MODEL_VARIABLE, AgentBudget, AgentSettings
from carousearch.carousell.parse import LISTING_URL_TEMPLATE
from carousearch.filters.schema import Listing
from carousearch.screening.models import format_sgd
from carousearch.web.prompt_runs import PromptRun, TraceLine, trace_lines
from carousearch.web.view_models import ErrorMessage, meta_line, plural

if TYPE_CHECKING:  # agent.progress imports Pydantic AI, which the web app loads only when a run starts
    from carousearch.agent.progress import BudgetUsed

MAX_PROMPT_LENGTH = 2000
EMPTY_PROMPT_MESSAGE = ErrorMessage("Type what you are looking for, then press Run.")
RUN_IN_PROGRESS_MESSAGE = ErrorMessage("An Ask run is already in progress. Wait for it to finish, then run again.")
UNKNOWN_RUN_MESSAGE = ErrorMessage("That Ask run is no longer here (the web app keeps the last 10 until it restarts).")
NOT_FROM_THE_PAGE_MESSAGE = ErrorMessage("Start Ask runs from the carousearch page itself.")
UNSEEN_LISTING_WARNING = "Not in any search result: the title, price and link are the model's, not Carousell's."


# --- the form -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AskFormView:
    model_line: str  # "Model: anthropic:claude-haiku-5-5, from AGENT_MODEL"
    budget_line: str  # "Up to 40 tool calls, 15 photos and 200 listings per run"
    missing_key: ErrorMessage | None  # set when the model's provider key is missing; Run is then disabled
    run_disabled: bool
    max_prompt_length: int


def ask_form_view(settings: AgentSettings) -> AskFormView:
    missing_key = missing_key_message(settings)
    budget = settings.budget
    return AskFormView(
        model_line=f"Model: {settings.model}, from {AGENT_MODEL_VARIABLE}",
        budget_line=(
            f"Up to {plural(budget.max_tool_calls, 'tool call')}, {plural(budget.max_photos, 'photo')} and "
            f"{plural(budget.max_listings, 'listing')} per run"
        ),
        missing_key=missing_key,
        run_disabled=missing_key is not None,
        max_prompt_length=MAX_PROMPT_LENGTH,
    )


def missing_key_message(settings: AgentSettings) -> ErrorMessage | None:
    """Names the variable to set; never shows a key."""
    variable = settings.missing_key_variable()
    if variable is None:
        return None
    return ErrorMessage(
        f"{variable} is not set; the model {settings.model} needs it. Add it to .env and restart", "carousearch web"
    )


def prompt_problem(prompt: str) -> ErrorMessage | None:
    """Why the prompt cannot run (empty, or too long), or None."""
    text = prompt.strip()
    if not text:
        return EMPTY_PROMPT_MESSAGE
    if len(text) > MAX_PROMPT_LENGTH:
        return ErrorMessage(f"Keep the request to {MAX_PROMPT_LENGTH:,} characters; this one has {len(text):,}.")
    return None


# --- the trace and the budget -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceRowView:
    tool_name: str
    arguments: str
    status: str  # "ok", "failed", "refused", "not run" or "running"
    status_class: str  # the CSS modifier: "ok", "failed", "refused", "not-run", "running"
    duration: str  # "1.2 s"; empty for a call that did not run or is still running


def trace_row(tool_name: str, arguments: str, status: str, duration_seconds: float) -> TraceRowView:
    ran = status in {"ok", "failed"}
    return TraceRowView(
        tool_name=tool_name,
        arguments=arguments,
        status=status,
        status_class=status.replace(" ", "-"),
        duration=format_duration(duration_seconds) if ran else "",
    )


def progress_trace_rows(lines: Sequence[TraceLine]) -> list[TraceRowView]:
    return [trace_row(line.tool_name, line.arguments, line.status, line.duration_seconds) for line in lines]


def answer_trace_rows(tool_trace: Sequence[ToolCall]) -> list[TraceRowView]:
    return [trace_row(call.tool_name, call.arguments, call.status, call.duration_seconds) for call in tool_trace]


def budget_used_line(used: "BudgetUsed | None", budget: AgentBudget) -> str:
    """ "calls 3 of 40 · photos 1 of 15 · listings 48 of 200", from the latest event (none yet: all zero)."""
    if used is None:
        return f"calls 0 of {budget.max_tool_calls} · photos 0 of {budget.max_photos} · listings 0 of {budget.max_listings}"
    return (
        f"calls {used.tool_calls_used} of {used.max_tool_calls} · photos {used.photos_used} of {used.max_photos} · "
        f"listings {used.listings_used} of {used.max_listings}"
    )


def latest_budget(run: PromptRun) -> "BudgetUsed | None":
    return run.events[-1].budget if run.events else None


# --- a run in flight ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressView:
    run_id: str
    poll_url: str  # GET /ask/{run_id}
    prompt: str
    status_line: str  # "Running · 12 s · 2 tool calls"
    budget_line: str
    trace: list[TraceRowView]


def progress_view(run: PromptRun, budget: AgentBudget, elapsed_seconds: float) -> ProgressView:
    lines = trace_lines(run.events)
    calls = plural(len(lines), "tool call") if lines else "starting"
    return ProgressView(
        run_id=run.run_id,
        poll_url=f"/ask/{run.run_id}",
        prompt=run.prompt,
        status_line=f"Running · {format_duration(elapsed_seconds)} · {calls}",
        budget_line=budget_used_line(latest_budget(run), budget),
        trace=progress_trace_rows(lines),
    )


# --- the answer ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeptCardView:
    listing_id: str
    title: str
    url: str | None  # None when the model gave no usable http(s) link
    thumbnail_url: str | None
    price: str | None
    meta: str
    reason: str
    warning: str | None  # set when no search returned this listing
    details_available: bool  # the Details button needs a numeric Carousell id


@dataclass(frozen=True)
class DroppedRowView:
    listing_id: str
    title: str
    url: str | None
    price: str | None
    reason: str


@dataclass(frozen=True)
class AnswerView:
    prompt: str
    summary: str  # "2 kept · 5 dropped · 6 tool calls · 14 s"
    budget_line: str
    notes: list[str]
    kept: list[KeptCardView]
    dropped: list[DroppedRowView]
    trace: list[TraceRowView]


def answer_view(run: PromptRun, answer: AgentAnswer, budget: AgentBudget, elapsed_seconds: float) -> AnswerView:
    summary_parts = [
        f"{len(answer.kept):,} kept",
        f"{len(answer.dropped):,} dropped",
        plural(len(answer.tool_trace), "tool call"),
        format_duration(elapsed_seconds),
    ]
    return AnswerView(
        prompt=run.prompt,
        summary=" · ".join(summary_parts),
        budget_line=budget_used_line(latest_budget(run), budget),
        notes=notes_lines(answer.notes),
        kept=[kept_card(kept, run.seen_listings.get(kept.listing_id)) for kept in answer.kept],
        dropped=[dropped_row(dropped, run.seen_listings.get(dropped.listing_id)) for dropped in answer.dropped],
        trace=answer_trace_rows(answer.tool_trace),
    )


def kept_card(kept: KeptListing, seen: Listing | None) -> KeptCardView:
    """Carousell's own listing data when a search returned it; otherwise the model's, with a warning."""
    details_available = kept.listing_id.isdigit()
    if seen is not None:
        return KeptCardView(
            listing_id=seen.id,
            title=seen.title,
            url=seen.listing_url,
            thumbnail_url=seen.thumbnail_url,
            price=seen.price_display,
            meta=meta_line(seen),
            reason=kept.reason,
            warning=None,
            details_available=details_available,
        )
    return KeptCardView(
        listing_id=kept.listing_id,
        title=kept.title,
        url=web_link(kept.url),
        thumbnail_url=None,
        price=None if kept.price is None else format_sgd(kept.price),
        meta=kept.seller or "",
        reason=kept.reason,
        warning=UNSEEN_LISTING_WARNING,
        details_available=details_available,
    )


def dropped_row(dropped: DroppedListing, seen: Listing | None) -> DroppedRowView:
    """The model's title and reason; the link and price from the search result when there is one."""
    if seen is not None:
        url, price = seen.listing_url, seen.price_display
    else:
        url = LISTING_URL_TEMPLATE.format(listing_id=dropped.listing_id) if dropped.listing_id.isdigit() else None
        price = None
    return DroppedRowView(
        listing_id=dropped.listing_id, title=dropped.title, url=url, price=price, reason=dropped.reason
    )


def notes_lines(notes: str) -> list[str]:
    return [line.strip() for line in notes.splitlines() if line.strip()]


def web_link(url: str) -> str | None:
    """The model's link only when it is a plain http(s) address, so a page never gets a javascript: link."""
    return url if url.startswith(("https://", "http://")) else None


# --- a failed run -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedView:
    prompt: str
    error: ErrorMessage
    budget_line: str
    trace: list[TraceRowView]  # the calls made before it failed


def failed_view(run: PromptRun, error: ErrorMessage, budget: AgentBudget) -> FailedView:
    return FailedView(
        prompt=run.prompt,
        error=error,
        budget_line=budget_used_line(latest_budget(run), budget),
        trace=progress_trace_rows(trace_lines(run.events)),
    )


# --- a filter search waiting for an Ask run ---------------------------------------------------------------------


WAITING_RESULTS_ELEMENT_ID = "ask-waiting-results"  # sent back as HX-Trigger when the results partial asks again


@dataclass(frozen=True)
class WaitingView:
    """Shown instead of a search or a listing's details while an Ask run holds the Carousell client; it asks
    again every 2 s and is replaced by the answer once the run has ended."""

    url: str  # the same request again
    text: str
    element_id: str | None  # the results region's waiting partial has an id; a detail panel's has none


SEARCH_WAITING_TEXT = "An Ask run is in progress; your search will run when it finishes."
DETAIL_WAITING_TEXT = "An Ask run is in progress; these details will load when it finishes."


def waiting_view(url: str, for_detail: bool = False) -> WaitingView:
    if for_detail:
        return WaitingView(url=url, text=DETAIL_WAITING_TEXT, element_id=None)
    return WaitingView(url=url, text=SEARCH_WAITING_TEXT, element_id=WAITING_RESULTS_ELEMENT_ID)


# --- formatting ---------------------------------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """ "0.4 s", "1.2 s" under ten seconds; "14 s" under a minute; "2 min 5 s" beyond."""
    if seconds < 10:
        return f"{max(seconds, 0.0):.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes} min {remainder} s"
