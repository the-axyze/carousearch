"""The hook every tool call of a run passes through (ToolCallGate) and the toolsets both phases build on it.

The planning phase (agent/planning.py) and the execution phase (agent/run_prompt.py) each run one Pydantic AI agent
over the MCP server from `mcp_server.build_server(services)`, in this process: Pydantic AI's MCPToolset reaches it
over in-memory streams. The execution phase also has the local tools (agent/local_tools.py), wrapped in
GatedToolset. Every call is admitted against the phase's budget, timed, filed into the run's listings.db and shown
to an optional observer; the model gets a shorter result (agent/tool_results.py).

Also here, for both phases: the request limit of a phase and add_planner_usage, which reads the planner's tokens
from a phase's messages.

I/O: none of its own; it calls the tools it is handed.
"""

import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from pydantic_ai import RunContext, UsageLimits
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from carousearch.agent.budget import BudgetExhausted, ToolBudget
from carousearch.agent.cost import UsageLedger
from carousearch.agent.progress import ProgressObserver, ToolCallEvent, finished_event, started_event
from carousearch.agent.result import ToolCallStatus
from carousearch.agent.settings import AgentBudget
from carousearch.agent.tool_results import arguments_for_server, result_for_model
from carousearch.agent.trace import CallRecord

logger = logging.getLogger(__name__)

# The backstop for a model that ignores refusals: a phase ends after this many model requests beyond its tool-call
# budget. Parallel calls make the normal request count lower than the tool-call count.
EXTRA_MODEL_REQUESTS = 5
# Retries per tool and for the final output when the model sends arguments that fail validation.
MODEL_RETRIES = 2


class ToolResultRecorder(Protocol):
    """Keeps tool results for the run (agent.listings_db.RunListingsStore)."""

    def record_tool_result(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None: ...


class ToolCallGate:
    """The hook every tool call passes through: admits it against the budget, then times it.

    A refused call is answered with a failed tool result (ToolFailed) the model reads, telling it to finish.
    A finished call's full result goes to the recorder (listings.db) and the observer; the model gets the shorter
    version agent/tool_results.py makes. The observer, if any, gets a "started" event once a call is admitted and a
    "finished" event for every call, refused ones included. An observer or recorder that raises is logged and does
    not stop the call."""

    def __init__(
        self,
        budget: ToolBudget,
        clock: Callable[[], float] = time.monotonic,
        observer: ProgressObserver | None = None,
        recorder: ToolResultRecorder | None = None,
    ) -> None:
        self.budget = budget
        self.clock = clock
        self.observer = observer
        self.recorder = recorder
        self.records: dict[str, CallRecord] = {}  # by tool-call id

    async def __call__(
        self, context: RunContext[Any], call_tool: CallToolFunc, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        call_id = context.tool_call_id or tool_name
        try:
            admitted_arguments = self.budget.admit(tool_name, arguments)
        except BudgetExhausted as refusal:
            self.records[call_id] = CallRecord(duration_seconds=0.0, status="refused", arguments=arguments)
            self.notify(lambda: finished_event(tool_name, arguments, "refused", 0.0, self.budget))
            raise ToolFailed(str(refusal)) from refusal
        self.notify(lambda: started_event(tool_name, admitted_arguments, self.budget))
        started = self.clock()
        status: ToolCallStatus = "failed"
        result: ToolResult | None = None
        try:
            result = await call_tool(tool_name, arguments_for_server(tool_name, admitted_arguments))
            status = "ok"
            self.record(tool_name, admitted_arguments, result)
            return result_for_model(tool_name, admitted_arguments, result)
        finally:
            duration_seconds = self.clock() - started
            self.records[call_id] = CallRecord(
                duration_seconds=duration_seconds, status=status, arguments=admitted_arguments
            )
            self.notify(
                lambda: finished_event(tool_name, admitted_arguments, status, duration_seconds, self.budget, result)
            )

    def record(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder.record_tool_result(tool_name, arguments, result)
        except Exception:
            logger.exception("Could not keep the %s result in listings.db; the run goes on", tool_name)

    def notify(self, build_event: Callable[[], ToolCallEvent]) -> None:
        """Build the event only when someone observes, and never let the observer break the run."""
        if self.observer is None:
            return
        try:
            self.observer(build_event())
        except Exception:
            logger.exception("The progress observer failed; the run goes on")


@dataclass
class GatedToolset(WrapperToolset[Any]):
    """Sends a function toolset's calls through the same ToolCallGate the MCP toolset uses (process_tool_call)."""

    gate: ToolCallGate

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        async def call_wrapped(tool_name: str, arguments: dict[str, Any], **_: Any) -> Any:
            return await self.wrapped.call_tool(tool_name, arguments, ctx, tool)

        return await self.gate(ctx, call_wrapped, name, tool_args)


def build_mcp_toolset(server: MCPServer, gate: ToolCallGate, tool_names: Collection[str]) -> AbstractToolset[Any]:
    """The server's tools named in `tool_names`, gated. Its usage notes join the instructions. Tool errors (a refused
    photo URL, Carousell blocking) reach the model as failed results without using up its retries."""
    toolset = MCPToolset(server, include_instructions=True, tool_error_behavior="failed", process_tool_call=gate)
    return toolset.filtered(lambda context, tool_definition: tool_definition.name in tool_names)


def usage_limits(budget: AgentBudget) -> UsageLimits:
    return UsageLimits(request_limit=budget.max_tool_calls + EXTRA_MODEL_REQUESTS)


def add_planner_usage(
    ledger: UsageLedger, model_label: str, messages: Sequence[ModelMessage], web_searches: int
) -> None:
    """The planner model's tokens, from each model response's own usage (there even when a phase was stopped)."""
    for message in messages:
        if isinstance(message, ModelResponse):
            ledger.add_usage("planner", model_label, message.usage, requests=1)
    if web_searches:
        ledger.add("planner", model_label, web_searches=web_searches)
