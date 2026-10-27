"""The planning phase: turn a request into a SearchPlan before any listing is judged.

The planner model gets the discovery tools only (category_fields, search_listings and sample_descriptions, gated by
AgentSettings.planning_budget: 6 tool calls by default, no photos), the provider's hosted web search where it has
one, and the owner's knowledge notes whose keywords appear in the request. It answers with a SearchPlan; a plan
whose parts do not fit together (agent.plan.plan_problems) is sent back once or twice to be fixed, and a first plan
made without reading a single search page is sent back once with a reminder to sample the listings (the plan's
display says so if it still did not: agent.result.planning_sampled_listings). A revision (PlanAmendment) shows the planner its previous plan and the owner's
requested change, and needs no new sample.

The search pages it reads go into the run's listings.db, where the execution phase finds them again.

I/O: the model provider, and Carousell through the injected McpServices.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, ModelRetry, ToolOutput, capture_run_messages
from pydantic_ai.capabilities import Caching
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.models import Model

from carousearch.agent.budget import ToolBudget
from carousearch.agent.cost import ModelUsage, UsageLedger
from carousearch.agent.knowledge import KnowledgeNote, format_notes_for_model, select_notes
from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.plan import SearchPlan, plan_problems
from carousearch.agent.progress import ProgressObserver
from carousearch.agent.prompt import build_planning_prompt, planning_user_prompt
from carousearch.agent.result import ToolCall
from carousearch.agent.settings import AgentSettings
from carousearch.agent.templates import template_names
from carousearch.agent.toolsets import (
    MODEL_RETRIES,
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
from carousearch.language_models import build_model
from carousearch.mcp_server import CATEGORY_TOOL, SAMPLE_TOOL, SEARCH_TOOL, McpServices, build_server

PLAN_OUTPUT_TOOL_NAME = "search_plan"
PLANNING_TOOLS = frozenset({CATEGORY_TOOL, SEARCH_TOOL, SAMPLE_TOOL})
SAMPLE_REMINDER = (
    "Sample the listings before planning: call sample_descriptions on the search you expect to run, then base the "
    "attributes, regexes and exclusions on what sellers actually state there."
)


class PlanningFailed(AgentRunError):
    """The planner gave no usable plan within its request limit."""


@dataclass(frozen=True)
class PlanAmendment:
    """One revision round: the plan the owner saw and the change asked for, in plain words."""

    previous_plan: SearchPlan
    request: str


@dataclass(frozen=True)
class PlanningOutcome:
    """A plan and what making it cost; the execution phase adds its trace and usage to the answer."""

    plan: SearchPlan
    tool_trace: list[ToolCall] = field(default_factory=list)
    usage: list[ModelUsage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # budget and web search notes of the planning phase
    knowledge_files: list[str] = field(default_factory=list)  # the owner's notes the planner was shown

    @classmethod
    def from_plan(cls, plan: SearchPlan) -> "PlanningOutcome":
        """A plan from a file (`ask --plan-file`): nothing was spent on planning."""
        return cls(plan=plan)


async def plan_prompt(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    run_directory: Path | None = None,
    knowledge: Sequence[KnowledgeNote] = (),
    amendment: PlanAmendment | None = None,
    observer: ProgressObserver | None = None,
) -> PlanningOutcome:
    """Plan once. `model` replaces the planner AGENT_PLANNER_MODEL names (tests pass a scripted one); search pages go
    into <run_directory>/listings.db when given; `knowledge` is every note, of which those matching the request (and
    a revised plan's item class) are shown; `observer` receives each tool call's events."""
    planner = model or build_model(settings.planner_model, settings.keys)
    planner_label = settings.planner_model if model is None else planner.model_name
    budget = settings.planning_budget
    item_class = amendment.previous_plan.item_class if amendment else None
    notes_shown = select_notes(knowledge, prompt, item_class)
    recorder = RunListingsStore(run_directory) if run_directory is not None else None
    gate = ToolCallGate(ToolBudget(budget), observer=observer, recorder=recorder)
    web_search = web_search_capability(planner, budget.max_web_searches)
    remind_to_sample = amendment is None and budget.max_listings > 0
    agent = build_planning_agent(planner, gate, services, settings, web_search, remind_to_sample)
    user_prompt = planning_user_prompt(
        prompt,
        format_notes_for_model(notes_shown),
        amendment.previous_plan if amendment else None,
        amendment.request if amendment else None,
    )
    try:
        with capture_run_messages() as messages:
            try:
                result = await agent.run(user_prompt, usage_limits=usage_limits(budget))
            except UsageLimitExceeded as error:
                raise PlanningFailed(
                    f"The planner made no plan within {usage_limits(budget).request_limit} model requests."
                ) from error
    finally:
        if recorder is not None:
            recorder.close()
    ledger = UsageLedger()
    searches_used = count_web_searches(messages)
    add_planner_usage(ledger, planner_label, messages, searches_used)
    return PlanningOutcome(
        plan=result.output,
        tool_trace=build_tool_trace(messages, gate.records, PLAN_OUTPUT_TOOL_NAME),
        usage=ledger.report(),
        notes=planning_notes(gate, planner, planner_label, searches_used, budget.max_web_searches),
        knowledge_files=[note.file_name for note in notes_shown],
    )


def planning_notes(
    gate: ToolCallGate, planner: Model, planner_label: str, searches_used: int, max_web_searches: int
) -> list[str]:
    """The planning budget's notes and a reached web search cap, marked as planning's. A planner without hosted web
    search gets no note here: the execution phase's notes say so once."""
    supported = supports_hosted_web_search(planner)
    web_notes = web_search_notes(planner_label, supported, searches_used, max_web_searches) if supported else []
    return [f"Planning: {note}" for note in [*gate.budget.notes(), *web_notes]]


def plan_prompt_sync(
    prompt: str,
    services: McpServices,
    settings: AgentSettings,
    model: Model | None = None,
    *,
    run_directory: Path | None = None,
    knowledge: Sequence[KnowledgeNote] = (),
) -> PlanningOutcome:
    return asyncio.run(
        plan_prompt(prompt, services, settings, model, run_directory=run_directory, knowledge=knowledge)
    )


def build_planning_agent(
    planner: Model,
    gate: ToolCallGate,
    services: McpServices,
    settings: AgentSettings,
    web_search: Any,
    remind_to_sample: bool,
) -> Agent[None, SearchPlan]:
    agent: Agent[None, SearchPlan] = Agent(
        planner,
        output_type=ToolOutput(SearchPlan, name=PLAN_OUTPUT_TOOL_NAME),
        instructions=build_planning_prompt(settings.planning_budget),
        toolsets=[build_mcp_toolset(build_server(services), gate, PLANNING_TOOLS)],
        retries=MODEL_RETRIES,
        capabilities=[Caching(), *([web_search] if web_search else [])],
    )

    reminded = False

    @agent.output_validator
    def plan_is_usable(plan: SearchPlan) -> SearchPlan:
        nonlocal reminded
        problems = plan_problems(plan, template_names())
        if remind_to_sample and not reminded and not read_a_search_page(gate):
            reminded = True
            problems.insert(0, SAMPLE_REMINDER)
        if problems:
            raise ModelRetry("Fix the plan: " + " ".join(problems))
        return plan

    return agent


def read_a_search_page(gate: ToolCallGate) -> bool:
    """Whether the planner was admitted a search_listings or sample_descriptions page."""
    return gate.budget.listings > 0
