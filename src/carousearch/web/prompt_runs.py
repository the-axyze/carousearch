"""Ask mode's runs: each prompt is first planned, shown as a plan, and executed only when the owner presses Execute.
Planning and execution are background asyncio tasks in the web process, and the page polls them.

A run's status goes planning -> planned -> running -> done, or ends failed or cancelled; Edit sends a planned run
back to planning once (MAX_REVISIONS) with the owner's change.

- PromptRun: one run's state. The agent's progress observer (`PromptRun.record`) appends each ToolCallEvent and
  keeps the listings finished searches returned, by id, so the answer's cards show Carousell's data.
- trace_lines: the tool calls so far, one line per call, from the events.
- PromptRunRegistry: the newest MAX_KEPT_RUNS runs, in memory until the app stops; at most one plans or runs at a
  time.
- start_planning_run / start_revision / start_execution: start a task that plans (agent.planning.plan_prompt) or
  executes the plan (agent.run_prompt.execute_plan) under WebServices.turn, so no filter search uses the Carousell
  client or the database while an agent tool does.
- save_suggested_note: writes the answer's suggested knowledge note to knowledge/, only when the owner asks.

Pure: PromptRun's bookkeeping, trace_lines and the registry. I/O: the start_* functions create tasks that call the
model provider and Carousell (through mcp_server.McpServices); save_suggested_note writes one file.

Everything here runs on the app's event loop thread: the routes, the task and the observer (the agent's tool hook
calls it on the loop), so the registry and the runs need no lock. Pydantic AI and the MCP SDK are imported only
when a run starts, so importing the web app (every CLI command does) stays cheap.
"""

import asyncio
import logging
import secrets
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from carousearch.agent.knowledge import load_notes, note_file_text, note_slug, save_note, split_suggestion
from carousearch.agent.result import AgentAnswer, ToolCallStatus
from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import Listing
from carousearch.web.view_models import ErrorMessage, error_message_for

if TYPE_CHECKING:
    from carousearch.agent.planning import PlanAmendment, PlanningOutcome
    from carousearch.agent.progress import ToolCallEvent
    from carousearch.mcp_server import McpServices
    from carousearch.web.services import WebServices

logger = logging.getLogger(__name__)

MAX_KEPT_RUNS = 10
MAX_REVISIONS = 1
SHUTDOWN_GRACE_SECONDS = 5.0
AGENT_FAILED_MESSAGE = ErrorMessage("The agent run failed. The terminal running carousearch web has the details.")
STOPPED_MESSAGE = ErrorMessage("The run was stopped because carousearch web shut down.")

RunStatus = Literal["planning", "planned", "running", "done", "failed", "cancelled"]
TraceLineStatus = ToolCallStatus | Literal["running"]


@dataclass(frozen=True)
class TraceLine:
    """One tool call of a run in flight: "running" until its finished event arrives."""

    tool_name: str
    arguments: str
    status: TraceLineStatus
    duration_seconds: float


@dataclass
class PromptRun:
    run_id: str
    prompt: str
    started_at: datetime
    status: RunStatus = "running"  # the registry starts runs at "planning"
    events: "list[ToolCallEvent]" = field(default_factory=list)  # the execution's events, in order
    planning_events: "list[ToolCallEvent]" = field(default_factory=list)  # the latest planning round's events
    seen_listings: dict[str, Listing] = field(default_factory=dict)  # by id, from finished searches
    planning: "PlanningOutcome | None" = None  # the plan shown, and what planning cost
    revisions: int = 0
    run_directory: Path | None = None  # shared by planning and execution
    answer: AgentAnswer | None = None
    error: ErrorMessage | None = None
    finished_at: datetime | None = None
    planned_at: datetime | None = None
    saved_note: str | None = None  # the knowledge/ file the answer's suggested note was saved to

    def record(self, event: "ToolCallEvent") -> None:
        """The run's progress observer: quick and never blocking, as agent.progress asks."""
        (self.planning_events if self.status == "planning" else self.events).append(event)
        for listing in event.listings:
            self.seen_listings[listing.id] = listing

    def plan_ready(self, planning: "PlanningOutcome", now: datetime) -> None:
        self.status, self.planning, self.planned_at = "planned", planning, now

    def begin_revision(self) -> None:
        self.status, self.revisions, self.planning_events = "planning", self.revisions + 1, []

    def begin_execution(self) -> None:
        self.status = "running"

    def cancel(self, now: datetime) -> None:
        self.status, self.finished_at = "cancelled", now

    def finish(self, answer: AgentAnswer, now: datetime) -> None:
        self.status, self.answer, self.finished_at = "done", answer, now

    def fail(self, error: ErrorMessage, now: datetime) -> None:
        self.status, self.error, self.finished_at = "failed", error, now

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def busy(self) -> bool:
        """Planning or running: a task holds the Carousell client for this run."""
        return self.status in ("planning", "running")

    @property
    def can_revise(self) -> bool:
        return self.status == "planned" and self.revisions < MAX_REVISIONS


def trace_lines(events: "list[ToolCallEvent]") -> list[TraceLine]:
    """One line per call in the order calls started (a refused call, which never starts, where it was refused).
    A finished event completes the oldest running line of the same tool and arguments (parallel calls)."""
    lines: list[TraceLine] = []
    for event in events:
        if event.phase == "started":
            lines.append(TraceLine(event.tool_name, event.arguments, "running", 0.0))
            continue
        finished = TraceLine(event.tool_name, event.arguments, event.status or "failed", event.duration_seconds)
        position = next(
            (
                index
                for index, line in enumerate(lines)
                if line.status == "running" and (line.tool_name, line.arguments) == (event.tool_name, event.arguments)
            ),
            None,
        )
        if position is None:
            lines.append(finished)
        else:
            lines[position] = finished
    return lines


class PromptRunRegistry:
    """The newest runs by id, oldest first. Only the event loop thread touches it (see the module docstring)."""

    def __init__(self, limit: int = MAX_KEPT_RUNS) -> None:
        self._limit = limit
        self._runs: OrderedDict[str, PromptRun] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()  # strong references, so a running task is not collected

    def start(self, prompt: str, started_at: datetime, run_id: str | None = None) -> PromptRun:
        """Record a new run, planning; the oldest beyond the limit are forgotten. The caller checks active() first."""
        if self.active() is not None:
            raise RuntimeError("an Ask run is already in progress")
        run = PromptRun(run_id=run_id or new_run_id(), prompt=prompt, started_at=started_at, status="planning")
        self._runs[run.run_id] = run
        while len(self._runs) > self._limit:
            self._runs.popitem(last=False)
        return run

    def get(self, run_id: str) -> PromptRun | None:
        return self._runs.get(run_id)

    def active(self) -> PromptRun | None:
        """The run that is planning or running, if any."""
        return next((run for run in reversed(self._runs.values()) if run.busy), None)

    def newest(self) -> PromptRun | None:
        return next(reversed(self._runs.values()), None)

    def keep_task(self, task: "asyncio.Task[None]") -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Cancel the run in flight, if any, and give it a moment to end (the app is shutting down). A tool call
        in a worker thread cannot be interrupted, so this waits at most `grace_seconds` for it."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace_seconds)


def new_run_id() -> str:
    return secrets.token_urlsafe(8)


# --- planning and execution (I/O) --------------------------------------------------------------------------------


def start_planning_run(prompt: str, services: "WebServices") -> PromptRun:
    """Record a new run and start planning it on the running event loop; returns at once."""
    run = services.prompt_runs.start(prompt, services.screening.clock())
    start_task(services, plan_prompt_run(run, services), f"ask-plan-{run.run_id}")
    return run


def start_revision(run: PromptRun, change: str, services: "WebServices") -> None:
    """Send a planned run back to the planner with the owner's change (one round); returns at once."""
    from carousearch.agent.planning import PlanAmendment

    assert run.planning is not None, "only a planned run can be revised"
    amendment = PlanAmendment(previous_plan=run.planning.plan, request=change)
    run.begin_revision()
    start_task(services, plan_prompt_run(run, services, amendment), f"ask-revise-{run.run_id}")


def start_execution(run: PromptRun, services: "WebServices") -> None:
    """Start executing a planned run's plan; returns at once."""
    run.begin_execution()
    start_task(services, execute_prompt_run(run, services), f"ask-run-{run.run_id}")


def start_task(services: "WebServices", work: Coroutine[Any, Any, None], name: str) -> None:
    services.prompt_runs.keep_task(asyncio.create_task(work, name=name))


def ask_run_directory(runs_root: Path | None, run: PromptRun) -> Path:
    """<runs_root>/runs/<started>-<run id>/, or a fresh temporary folder when the app was given no data folder."""
    from carousearch.agent.run_directory import new_run_directory

    if runs_root is None:
        return Path(tempfile.mkdtemp(prefix=f"carousearch-ask-{run.run_id}-"))
    return new_run_directory(runs_root, run.started_at, suffix=run.run_id)


def mcp_services_for(services: "WebServices") -> "McpServices":
    """The agent's view of the web app's own Carousell client and 12-hour detail cache."""
    from carousearch.mcp_server import McpServices

    screening = services.screening
    return McpServices(carousell=services.carousell, detail_cache=screening.store, clock=screening.clock)


async def plan_prompt_run(run: PromptRun, services: "WebServices", amendment: "PlanAmendment | None" = None) -> None:
    """Plan (or revise the plan of) a run over the web app's client, holding the turn; the run ends planned, or
    failed with a calm message."""
    from carousearch.agent.planning import plan_prompt

    async def plan() -> "PlanningOutcome":
        if run.run_directory is None:
            run.run_directory = ask_run_directory(services.runs_root, run)
        return await plan_prompt(
            run.prompt, mcp_services_for(services), services.agent, model=services.agent_model,
            run_directory=run.run_directory, knowledge=load_notes(services.knowledge_dir), amendment=amendment,
            observer=run.record,
        )

    planning = await run_guarded(run, services, "planning", plan)
    if planning is not None:
        logger.info("Ask run %s planned with %d tool calls", run.run_id, len(planning.tool_trace))
        run.plan_ready(planning, services.screening.clock())


async def execute_prompt_run(run: PromptRun, services: "WebServices") -> None:
    """Execute a run's plan over the web app's own client and detail cache, holding the turn throughout. Every
    outcome ends the run: done with the answer, or failed with a calm message (the details go to the log)."""
    from carousearch.agent.local_tools import LocalToolServices
    from carousearch.agent.run_prompt import execute_plan

    async def execute() -> AgentAnswer:
        assert run.planning is not None, "only a planned run can be executed"
        local = LocalToolServices(
            run_directory=run.run_directory or ask_run_directory(services.runs_root, run),
            judgement_cache=services.judgement_cache,
            worker_model=services.agent_worker_model,
        )
        return await execute_plan(
            run.planning, run.prompt, mcp_services_for(services), services.agent, model=services.agent_model,
            local=local, knowledge=load_notes(services.knowledge_dir), observer=run.record,
        )

    answer = await run_guarded(run, services, "execution", execute)
    if answer is not None:
        logger.info("Ask run %s finished with %d tool calls", run.run_id, len(answer.tool_trace))
        run.finish(answer, services.screening.clock())


async def run_guarded[Result](
    run: PromptRun, services: "WebServices", phase: str, work: Callable[[], Awaitable[Result]]
) -> Result | None:
    """Run one phase holding the turn. A failure ends the run as failed with a calm message and returns None;
    the details go to the log. Pydantic AI and the MCP SDK are imported here, when a run starts."""
    import pydantic_ai
    from pydantic_ai.exceptions import AgentRunError, UserError

    pydantic_ai.BANNER_ENABLED = False  # the terminal running carousearch web is its log, as for `carousearch ask`
    clock = services.screening.clock
    try:
        async with services.turn:
            return await work()
    except (AgentRunError, UserError) as error:  # the model API failed or misbehaved; an unusable model setting
        logger.warning("Ask run %s %s failed: %s: %s", run.run_id, phase, type(error).__name__, error)
        run.fail(AGENT_FAILED_MESSAGE, clock())
    except CarousellError as error:
        logger.warning("Ask run %s: Carousell request failed: %s: %s", run.run_id, type(error).__name__, error)
        run.fail(error_message_for(error), clock())
    except asyncio.CancelledError:
        logger.warning("Ask run %s was stopped by the web app shutting down", run.run_id)
        run.fail(STOPPED_MESSAGE, clock())
        raise
    except Exception:  # never a silent death: the run says it failed and the log has the traceback
        logger.exception("Ask run %s %s failed", run.run_id, phase)
        run.fail(AGENT_FAILED_MESSAGE, clock())
    return None


# --- the owner's knowledge notes (I/O) ----------------------------------------------------------------------------


def save_suggested_note(run: PromptRun, knowledge_dir: Path) -> str:
    """Save the answer's suggested note under knowledge/ (never overwriting) and return its file name; a run whose
    note was saved already returns that file's name."""
    if run.saved_note is not None:
        return run.saved_note
    assert run.answer is not None and run.answer.suggested_knowledge, "only an answer with a suggested note"
    item_class = run.answer.plan.item_class if run.answer.plan else run.prompt
    keywords, body = split_suggestion(run.answer.suggested_knowledge, [item_class])
    path = save_note(knowledge_dir, note_slug(item_class), note_file_text(keywords, body))
    run.saved_note = path.name
    return path.name
