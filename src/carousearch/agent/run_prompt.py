"""Answer a shopping prompt with an agent whose tools are carousearch's own MCP tools.

The MCP server from `mcp_server.build_server(services)` runs in this process: Pydantic AI's MCPToolset reaches it
over in-memory streams (FastMCP's in-process transport), so a run starts no second interpreter. The toolset's
`process_tool_call` hook (ToolCallGate) admits every call against the run's budget and times it. A model that
keeps calling tools after refusals is stopped by Pydantic AI's request limit. An optional injected observer is told
as each tool call starts and finishes (agent/progress.py), with the budget used and the listings a search returned.

I/O: the model provider, and Carousell through the injected McpServices.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic_ai import Agent, RunContext, ToolOutput, UsageLimits, capture_run_messages
from pydantic_ai.exceptions import ToolFailed, UsageLimitExceeded
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class

from carousearch.agent.budget import BudgetExhausted, ToolBudget
from carousearch.agent.progress import ProgressObserver, ToolCallEvent, finished_event, started_event
from carousearch.agent.prompt import build_system_prompt
from carousearch.agent.result import AgentAnswer, ModelAnswer, ToolCallStatus, build_agent_answer
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.agent.trace import CallRecord, build_tool_trace
from carousearch.mcp_server import McpServices, build_server

logger = logging.getLogger(__name__)

OUTPUT_TOOL_NAME = "final_answer"
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
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    """Run the agent once. `model` replaces the one AGENT_MODEL names (tests pass a scripted model). `observer`,
    if given, receives a ToolCallEvent as each tool call starts and finishes."""
    gate = ToolCallGate(ToolBudget(settings.budget), observer=observer)
    toolset = build_toolset(build_server(services), gate)
    agent = build_agent(model or build_model(settings), toolset, settings.budget)
    with capture_run_messages() as messages:
        try:
            result = await agent.run(prompt, usage_limits=usage_limits(settings.budget))
            model_answer = result.output
        except UsageLimitExceeded:
            model_answer = stopped_answer(usage_limits(settings.budget).request_limit)
    trace = build_tool_trace(messages, gate.records, OUTPUT_TOOL_NAME)
    return build_agent_answer(model_answer, trace, gate.budget.notes())


def run_prompt_sync(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    return asyncio.run(run_prompt(prompt, services, settings, model, observer=observer))


class ToolCallGate:
    """The MCP toolset's process_tool_call hook: admits each call against the budget, then times it.

    A refused call is answered with a failed tool result (ToolFailed) the model reads, telling it to finish.
    The observer, if any, gets a "started" event once a call is admitted and a "finished" event for every call,
    refused ones included. An observer that raises is logged and does not stop the call."""

    def __init__(
        self,
        budget: ToolBudget,
        clock: Callable[[], float] = time.monotonic,
        observer: ProgressObserver | None = None,
    ) -> None:
        self.budget = budget
        self.clock = clock
        self.observer = observer
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
            result = await call_tool(tool_name, admitted_arguments)
            status = "ok"
            return result
        finally:
            duration_seconds = self.clock() - started
            self.records[call_id] = CallRecord(
                duration_seconds=duration_seconds, status=status, arguments=admitted_arguments
            )
            self.notify(
                lambda: finished_event(tool_name, admitted_arguments, status, duration_seconds, self.budget, result)
            )

    def notify(self, build_event: Callable[[], ToolCallEvent]) -> None:
        """Build the event only when someone observes, and never let the observer break the run."""
        if self.observer is None:
            return
        try:
            self.observer(build_event())
        except Exception:
            logger.exception("The progress observer failed; the run goes on")


def build_toolset(server: MCPServer, gate: ToolCallGate) -> MCPToolset:
    """The server's tools for the agent. Its usage notes join the instructions; tool errors (a refused photo URL,
    Carousell blocking) reach the model as failed results without using up its retries."""
    return MCPToolset(server, include_instructions=True, tool_error_behavior="failed", process_tool_call=gate)


def build_agent(model: Model, toolset: MCPToolset, budget: AgentBudget) -> Agent[None, ModelAnswer]:
    return Agent(
        model,
        output_type=ToolOutput(ModelAnswer, name=OUTPUT_TOOL_NAME),
        instructions=build_system_prompt(budget),
        toolsets=[toolset],
        retries=MODEL_RETRIES,
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


def build_model(settings: AgentSettings) -> Model:
    """The model AGENT_MODEL names, given the provider key from settings (and so from .env) when we hold one."""
    api_key = settings.api_key()
    return infer_model(settings.model, provider_factory=lambda provider_name: build_provider(provider_name, api_key))


def build_provider(provider_name: str, api_key: str | None) -> Provider[Any]:
    if api_key is None:
        return infer_provider(provider_name)  # the provider reads its own environment variables
    return infer_provider_class(provider_name)(api_key=api_key)
