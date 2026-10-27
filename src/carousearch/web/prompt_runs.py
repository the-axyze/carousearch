"""Ask mode's runs: each prompt runs once, as a background asyncio task in the web process, and the page polls it.

- PromptRun: one run's state. The agent's progress observer (`PromptRun.record`) appends each ToolCallEvent and
  keeps the listings finished searches returned, by id, so the answer's cards show Carousell's data.
- trace_lines: the tool calls so far, one line per call, from the events.
- PromptRunRegistry: the newest MAX_KEPT_RUNS runs, in memory until the app stops; at most one runs at a time.
- start_prompt_run / execute_prompt_run: start the task and run the agent (agent.run_prompt) under
  WebServices.turn, so no filter search uses the Carousell client or the database while an agent tool does.

Pure: PromptRun's bookkeeping, trace_lines and the registry. I/O: start_prompt_run creates the task, and
execute_prompt_run calls the model provider and Carousell (through mcp_server.McpServices).

Everything here runs on the app's event loop thread: the routes, the task and the observer (the agent's tool hook
calls it on the loop), so the registry and the runs need no lock. Pydantic AI and the MCP SDK are imported only
when a run starts, so importing the web app (every CLI command does) stays cheap.
"""

import asyncio
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from carousearch.agent.result import AgentAnswer, ToolCallStatus
from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import Listing
from carousearch.web.view_models import ErrorMessage, error_message_for

if TYPE_CHECKING:
    from carousearch.agent.progress import ToolCallEvent
    from carousearch.web.services import WebServices

logger = logging.getLogger(__name__)

MAX_KEPT_RUNS = 10
SHUTDOWN_GRACE_SECONDS = 5.0
AGENT_FAILED_MESSAGE = ErrorMessage("The agent run failed. The terminal running carousearch web has the details.")
STOPPED_MESSAGE = ErrorMessage("The run was stopped because carousearch web shut down.")

RunStatus = Literal["running", "done", "failed"]
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
    status: RunStatus = "running"
    events: "list[ToolCallEvent]" = field(default_factory=list)  # what the observer was handed, in order
    seen_listings: dict[str, Listing] = field(default_factory=dict)  # by id, from finished searches
    answer: AgentAnswer | None = None
    error: ErrorMessage | None = None
    finished_at: datetime | None = None

    def record(self, event: "ToolCallEvent") -> None:
        """The run's progress observer: quick and never blocking, as agent.progress asks."""
        self.events.append(event)
        for listing in event.listings:
            self.seen_listings[listing.id] = listing

    def finish(self, answer: AgentAnswer, now: datetime) -> None:
        self.status, self.answer, self.finished_at = "done", answer, now

    def fail(self, error: ErrorMessage, now: datetime) -> None:
        self.status, self.error, self.finished_at = "failed", error, now

    @property
    def running(self) -> bool:
        return self.status == "running"


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
        """Record a new running run; the oldest beyond the limit are forgotten. The caller checks active() first."""
        if self.active() is not None:
            raise RuntimeError("an Ask run is already in progress")
        run = PromptRun(run_id=run_id or new_run_id(), prompt=prompt, started_at=started_at)
        self._runs[run.run_id] = run
        while len(self._runs) > self._limit:
            self._runs.popitem(last=False)
        return run

    def get(self, run_id: str) -> PromptRun | None:
        return self._runs.get(run_id)

    def active(self) -> PromptRun | None:
        return next((run for run in reversed(self._runs.values()) if run.running), None)

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


# --- the run itself (I/O) ----------------------------------------------------------------------------------------


def start_prompt_run(prompt: str, services: "WebServices") -> PromptRun:
    """Record the run and start it as a task on the running event loop; returns at once."""
    run = services.prompt_runs.start(prompt, services.screening.clock())
    task = asyncio.create_task(execute_prompt_run(run, services), name=f"ask-{run.run_id}")
    services.prompt_runs.keep_task(task)
    return run


async def execute_prompt_run(run: PromptRun, services: "WebServices") -> None:
    """Run the agent once over the web app's own client and detail cache, holding the turn throughout. Every
    outcome ends the run: done with the answer, or failed with a calm message (the details go to the log)."""
    # Imported here: Pydantic AI and the MCP SDK cost about a second to import (see the module docstring).
    import pydantic_ai
    from pydantic_ai.exceptions import AgentRunError, UserError

    from carousearch.agent.run_prompt import run_prompt
    from carousearch.mcp_server import McpServices

    pydantic_ai.BANNER_ENABLED = False  # the terminal running carousearch web is its log, as for `carousearch ask`
    clock = services.screening.clock
    try:
        async with services.turn:
            mcp_services = McpServices(
                carousell=services.carousell, detail_cache=services.screening.store, clock=clock
            )
            answer = await run_prompt(
                run.prompt, mcp_services, services.agent, model=services.agent_model, observer=run.record
            )
    except (AgentRunError, UserError) as error:  # the model API failed or misbehaved; an unusable AGENT_MODEL
        logger.warning("Ask run %s failed: %s: %s", run.run_id, type(error).__name__, error)
        run.fail(AGENT_FAILED_MESSAGE, clock())
    except CarousellError as error:
        logger.warning("Ask run %s: Carousell request failed: %s: %s", run.run_id, type(error).__name__, error)
        run.fail(error_message_for(error), clock())
    except asyncio.CancelledError:
        logger.warning("Ask run %s was stopped by the web app shutting down", run.run_id)
        run.fail(STOPPED_MESSAGE, clock())
        raise
    except Exception:  # never a silent death: the run says it failed and the log has the traceback
        logger.exception("Ask run %s failed", run.run_id)
        run.fail(AGENT_FAILED_MESSAGE, clock())
    else:
        logger.info("Ask run %s finished with %d tool calls", run.run_id, len(answer.tool_trace))
        run.finish(answer, clock())
