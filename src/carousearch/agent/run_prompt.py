"""Answer a shopping prompt with an agent whose tools are carousearch's own MCP tools plus four local tools.

The MCP server from `mcp_server.build_server(services)` runs in this process: Pydantic AI's MCPToolset reaches it
over in-memory streams (FastMCP's in-process transport), so a run starts no second interpreter. The local tools
(agent/local_tools.py: run_python, judge_listings, judge_photos, photo_metrics) need this run's directory and the
worker model, so they are Pydantic AI function tools. Both kinds pass through one ToolCallGate, which admits every
call against the run's budget, times it, files search and detail results into the run's listings.db, and sends the
model a shorter version of them (agent/tool_results.py). A model that keeps calling tools after refusals is stopped
by Pydantic AI's request limit. An optional injected observer is told as each tool call starts and finishes
(agent/progress.py).

Two models: the planner runs this loop, the worker answers the judge tools. The planner gets the provider's hosted
web search where it has one (agent/web_search.py) and Pydantic AI's portable prompt caching (Caching). Token usage
of both is added up for the answer's cost estimate (agent/cost.py).

I/O: the model providers, Carousell through the injected McpServices, and the run directory.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from pydantic_ai import Agent, RunContext, ToolOutput, UsageLimits, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability, Caching
from pydantic_ai.exceptions import ToolFailed, UsageLimitExceeded
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from carousearch.agent.budget import BudgetExhausted, ToolBudget
from carousearch.agent.cost import UsageLedger
from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.local_tools import LocalToolServices, RunTools, build_local_toolset
from carousearch.agent.progress import ProgressObserver, ToolCallEvent, finished_event, started_event
from carousearch.agent.prompt import build_system_prompt
from carousearch.agent.result import AgentAnswer, ModelAnswer, ToolCallStatus, build_agent_answer
from carousearch.agent.run_photos import RunPhotoStore
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.agent.tool_results import arguments_for_server, result_for_model
from carousearch.agent.trace import CallRecord, build_tool_trace
from carousearch.agent.web_search import (
    count_web_searches,
    supports_hosted_web_search,
    web_search_capability,
    web_search_notes,
)
from carousearch.agent.worker import WorkerJudge
from carousearch.language_models import build_model
from carousearch.mcp_server import SAMPLE_TOOL, McpServices, build_server

logger = logging.getLogger(__name__)

OUTPUT_TOOL_NAME = "final_answer"
USAGE_NOTES_TOOL = "usage_notes"  # its text is already in the instructions
# The planning phase's discovery tool; the execution phase reads listings with search_listings and the local tools.
EXECUTION_HIDDEN_TOOLS = frozenset({USAGE_NOTES_TOOL, SAMPLE_TOOL})
# The backstop for a model that ignores refusals: the run ends after this many model requests beyond the
# tool-call budget. Parallel calls make the normal request count lower than the tool-call count.
EXTRA_MODEL_REQUESTS = 5
# Retries per tool and for the final answer when the model sends arguments that fail validation.
MODEL_RETRIES = 2


async def run_prompt(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    """Run the agent once. `model` replaces the planner AGENT_PLANNER_MODEL names (tests pass a scripted model);
    `local` holds the run directory, the judgement cache and optionally a replacement worker model. `observer`, if
    given, receives a ToolCallEvent as each tool call starts and finishes."""
    planner = model or build_model(settings.planner_model, settings.keys)
    planner_label = settings.planner_model if model is None else planner.model_name
    worker = local.worker_model or build_model(settings.worker_model, settings.keys)
    worker_label = settings.worker_model if local.worker_model is None else worker.model_name
    ledger = UsageLedger()
    listings = RunListingsStore(local.run_directory)
    gate = ToolCallGate(ToolBudget(settings.budget), observer=observer, recorder=listings)
    tools = RunTools(
        services=services,
        local=local,
        budget=gate.budget,
        listings=listings,
        worker=WorkerJudge(worker, worker_label, ledger),
        photos=RunPhotoStore(local.run_directory),
    )
    toolsets = [build_toolset(build_server(services), gate), GatedToolset(build_local_toolset(tools), gate)]
    web_search = web_search_capability(planner, settings.budget.max_web_searches)
    agent = build_agent(planner, toolsets, settings.budget, [Caching(), *([web_search] if web_search else [])])
    try:
        with capture_run_messages() as messages:
            try:
                result = await agent.run(prompt, usage_limits=usage_limits(settings.budget))
                model_answer = result.output
            except UsageLimitExceeded:
                model_answer = stopped_answer(usage_limits(settings.budget).request_limit)
    finally:
        listings.close()
    searches_used = count_web_searches(messages)
    add_planner_usage(ledger, planner_label, messages, searches_used)
    notes = gate.budget.notes() + web_search_notes(
        planner_label, supports_hosted_web_search(planner), searches_used, settings.budget.max_web_searches
    )
    trace = build_tool_trace(messages, gate.records, OUTPUT_TOOL_NAME)
    return build_agent_answer(model_answer, trace, notes, ledger.report())


def run_prompt_sync(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    return asyncio.run(run_prompt(prompt, services, settings, model, local=local, observer=observer))


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


def build_toolset(server: MCPServer, gate: ToolCallGate) -> AbstractToolset[Any]:
    """The server's tools for the agent. Its usage notes join the instructions, so the usage_notes tool, which
    returns the same text, is left out. Tool errors (a refused photo URL, Carousell blocking) reach the model as
    failed results without using up its retries."""
    toolset = MCPToolset(server, include_instructions=True, tool_error_behavior="failed", process_tool_call=gate)
    return toolset.filtered(lambda context, tool_definition: tool_definition.name not in EXECUTION_HIDDEN_TOOLS)


def build_agent(
    model: Model,
    toolsets: Sequence[AbstractToolset[Any]],
    budget: AgentBudget,
    capabilities: Sequence[AbstractCapability[Any]] = (),
) -> Agent[None, ModelAnswer]:
    return Agent(
        model,
        output_type=ToolOutput(ModelAnswer, name=OUTPUT_TOOL_NAME),
        instructions=build_system_prompt(budget),
        toolsets=list(toolsets),
        retries=MODEL_RETRIES,
        capabilities=list(capabilities),
    )


def usage_limits(budget: AgentBudget) -> UsageLimits:
    return UsageLimits(request_limit=budget.max_tool_calls + EXTRA_MODEL_REQUESTS)


def stopped_answer(request_limit: int | None) -> ModelAnswer:
    return ModelAnswer(
        kept=[],
        dropped=[],
        notes=(
            f"Stopped after {request_limit} model requests without an answer: the model kept calling tools after "
            "the budget refused them. The tool trace shows what was looked at."
        ),
    )


def add_planner_usage(
    ledger: UsageLedger, model_label: str, messages: Sequence[ModelMessage], web_searches: int
) -> None:
    """The planner's tokens, from each model response's own usage (there even when the run was stopped)."""
    for message in messages:
        if isinstance(message, ModelResponse):
            ledger.add_usage("planner", model_label, message.usage, requests=1)
    if web_searches:
        ledger.add("planner", model_label, web_searches=web_searches)
