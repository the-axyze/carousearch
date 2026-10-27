"""Answer a request in two phases: plan it (agent/planning.py), then execute the agreed plan with the full toolset.

`run_prompt` does both in one call (`carousearch ask`). The web app's Ask mode calls `plan_prompt` first, shows the
plan, and calls `execute_plan` only when the owner presses Execute; `ask --plan-file` executes a saved plan.

The execution phase runs one Pydantic AI agent over the MCP server's tools (agent/toolsets.py) plus the local tools
(agent/local_tools.py: run_python, extract_fields, judge_listings, judge_photos, photo_metrics), all through one
ToolCallGate on AgentSettings.budget. Its user message carries the request, the plan, the plan items it must review
and the owner's matching notes; its answer's plan review is completed so every plan item has a status. A model that
keeps calling tools after refusals is stopped by Pydantic AI's request limit.

Two models: the planner runs both phases' loops, the worker answers the judge tools and extract_fields. The planner
gets the provider's hosted web search where it has one (agent/web_search.py) and portable prompt caching
(Caching). Token usage of both phases and both models is added up for the answer's cost estimate (agent/cost.py).

I/O: the model providers, Carousell through the injected McpServices, and the run directory.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent, ToolOutput, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability, Caching
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset

from carousearch.agent.budget import ToolBudget
from carousearch.agent.cost import UsageLedger
from carousearch.agent.knowledge import KnowledgeNote, format_notes_for_model, select_notes
from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.local_tools import LocalToolServices, RunTools, build_local_toolset
from carousearch.agent.planning import PlanningOutcome, plan_prompt
from carousearch.agent.progress import ProgressObserver
from carousearch.agent.prompt import build_execution_prompt, execution_user_prompt
from carousearch.agent.result import AgentAnswer, ModelAnswer, build_agent_answer
from carousearch.agent.run_photos import RunPhotoStore
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.agent.toolsets import (
    MODEL_RETRIES,
    GatedToolset,
    ToolCallGate,
    add_planner_usage,
    build_mcp_toolset,
    usage_limits,
)
from carousearch.agent.trace import build_tool_trace
from carousearch.agent.web_search import (
    count_web_searches,
    supports_hosted_web_search,
    web_search_capability,
    web_search_notes,
)
from carousearch.agent.worker import WorkerJudge
from carousearch.language_models import build_model
from carousearch.mcp_server import (
    CATEGORY_TOOL,
    DETAILS_TOOL,
    PHOTO_TOOL,
    SCREENING_TOOL,
    SEARCH_TOOL,
    McpServices,
    build_server,
)

logger = logging.getLogger(__name__)

OUTPUT_TOOL_NAME = "final_answer"
# usage_notes is left out (its text is in the instructions) and sample_descriptions is the planning phase's.
EXECUTION_MCP_TOOLS = frozenset({SEARCH_TOOL, DETAILS_TOOL, PHOTO_TOOL, SCREENING_TOOL, CATEGORY_TOOL})


async def run_prompt(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    knowledge: Sequence[KnowledgeNote] = (),
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    """Plan, then execute the plan without stopping for approval. `model` replaces the planner model for both phases
    (tests pass a scripted one); `local` holds the run directory both phases share, the judgement cache and
    optionally a replacement worker model; `knowledge` is every owner's note (each phase picks the matching ones)."""
    planning = await plan_prompt(
        prompt, services, settings, model, run_directory=local.run_directory, knowledge=knowledge, observer=observer
    )
    return await execute_plan(
        planning, prompt, services, settings, model, local=local, knowledge=knowledge, observer=observer
    )


async def execute_plan(
    planning: PlanningOutcome,
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    knowledge: Sequence[KnowledgeNote] = (),
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    """Execute an agreed plan once. The answer carries the plan, both phases' traces and usage, and a plan review
    with a status for every plan item."""
    plan = planning.plan
    planner = model or build_model(settings.planner_model, settings.keys)
    planner_label = settings.planner_model if model is None else planner.model_name
    worker = local.worker_model or build_model(settings.worker_model, settings.keys)
    worker_label = settings.worker_model if local.worker_model is None else worker.model_name
    ledger = UsageLedger()
    ledger.add_report(planning.usage)
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
    toolsets = [
        build_mcp_toolset(build_server(services), gate, EXECUTION_MCP_TOOLS),
        GatedToolset(build_local_toolset(tools), gate),
    ]
    web_search = web_search_capability(planner, settings.budget.max_web_searches)
    agent = build_agent(planner, toolsets, settings.budget, [Caching(), *([web_search] if web_search else [])])
    owner_notes = format_notes_for_model(select_notes(knowledge, prompt, plan.item_class))
    try:
        with capture_run_messages() as messages:
            try:
                result = await agent.run(
                    execution_user_prompt(prompt, plan, owner_notes), usage_limits=usage_limits(settings.budget)
                )
                model_answer = result.output
            except UsageLimitExceeded:
                model_answer = stopped_answer(usage_limits(settings.budget).request_limit)
    finally:
        listings.close()
    searches_used = count_web_searches(messages)
    add_planner_usage(ledger, planner_label, messages, searches_used)
    notes = planning.notes + gate.budget.notes() + web_search_notes(
        planner_label, supports_hosted_web_search(planner), searches_used, settings.budget.max_web_searches
    )
    trace = build_tool_trace(messages, gate.records, OUTPUT_TOOL_NAME)
    return build_agent_answer(model_answer, trace, notes, ledger.report(), plan, planning.tool_trace)


def run_prompt_sync(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    knowledge: Sequence[KnowledgeNote] = (),
    observer: ProgressObserver | None = None,
) -> AgentAnswer:
    return asyncio.run(
        run_prompt(prompt, services, settings, model, local=local, knowledge=knowledge, observer=observer)
    )


def execute_plan_sync(
    planning: PlanningOutcome,
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    local: LocalToolServices,
    knowledge: Sequence[KnowledgeNote] = (),
) -> AgentAnswer:
    return asyncio.run(execute_plan(planning, prompt, services, settings, model, local=local, knowledge=knowledge))


def build_agent(
    model: Model,
    toolsets: Sequence[AbstractToolset[Any]],
    budget: AgentBudget,
    capabilities: Sequence[AbstractCapability[Any]] = (),
) -> Agent[None, ModelAnswer]:
    return Agent(
        model,
        output_type=ToolOutput(ModelAnswer, name=OUTPUT_TOOL_NAME),
        instructions=build_execution_prompt(budget),
        toolsets=list(toolsets),
        retries=MODEL_RETRIES,
        capabilities=list(capabilities),
    )


def stopped_answer(request_limit: int | None) -> ModelAnswer:
    return ModelAnswer(
        kept=[],
        dropped=[],
        notes=(
            f"Stopped after {request_limit} model requests without an answer: the model kept calling tools after "
            "the budget refused them. The tool trace shows what was looked at."
        ),
    )
