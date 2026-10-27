"""The planning phase end to end, offline: a scripted planner discovers with category_fields, sample_descriptions and
a (scripted) web search, then returns a plan; the plan is checked, revised on request, and capped by its budget.
The real MCP server runs in this process over a fake Carousell."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from carousearch.agent.knowledge import parse_note
from carousearch.agent.plan import BenchmarkSpec
from carousearch.agent.planning import (
    PLAN_OUTPUT_TOOL_NAME,
    PlanAmendment,
    PlanningFailed,
    PlanningOutcome,
    plan_prompt,
    plan_prompt_sync,
)
from carousearch.agent.result import LISTING_SAMPLING_TOOLS, planning_sampled_listings
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.mcp_server import SAMPLE_TOOL, SEARCH_TOOL, McpServices

from fake_mcp_services import FakeCarousellSource, fake_services
from plan_examples import EXAMPLE_PLAN, example_plan
from scripted_model import ScriptedModel, calls, plan_answer

models.ALLOW_MODEL_REQUESTS = False

REQUEST = "Find a table well below the usual price for its size."
SAMPLE_CALL = ("sample_descriptions", {"request": {"query": "table"}, "n": 5})
CATEGORY_CALL = ("category_fields", {"category": "1595"})


def web_search_with(call: tuple[str, dict]) -> ModelResponse:
    """A turn in which the provider ran a web search, then the model called a tool."""
    return ModelResponse(parts=[
        NativeToolCallPart("web_search", {"query": "what sets resale prices of tables"}, "search-1"),
        NativeToolReturnPart("web_search", {"status": "completed"}, "search-1"),
        ToolCallPart(*call),
    ])


def plan_with(
    script: ScriptedModel,
    carousell: FakeCarousellSource | None = None,
    settings: AgentSettings | None = None,
    run_directory: Path | None = None,
    **options,
) -> PlanningOutcome:
    services: McpServices = fake_services(carousell or FakeCarousellSource())
    return plan_prompt_sync(
        REQUEST, services, settings or AgentSettings(), FunctionModel(script), run_directory=run_directory, **options
    )


def user_prompt_of(script: ScriptedModel) -> str:
    first_request = script.requests[0][0][0]
    assert isinstance(first_request, ModelRequest)
    return next(part.content for part in first_request.parts if isinstance(part, UserPromptPart))


def test_the_planner_discovers_then_returns_a_plan_with_its_trace_and_cost(tmp_path: Path) -> None:
    carousell = FakeCarousellSource()
    script = ScriptedModel(calls(CATEGORY_CALL), web_search_with(SAMPLE_CALL), plan_answer())

    outcome = plan_with(script, carousell, run_directory=tmp_path)

    assert outcome.plan == EXAMPLE_PLAN
    assert [(call.tool_name, call.status) for call in outcome.tool_trace] == [
        ("category_fields", "ok"), ("web_search", "ok"), ("sample_descriptions", "ok"),
    ]
    assert carousell.category_filter_requests == ["1595"] and len(carousell.search_requests) == 1
    (usage,) = outcome.usage
    assert (usage.role, usage.requests, usage.web_searches) == ("planner", 3, 1)
    assert outcome.notes == [] and outcome.knowledge_files == []


def test_the_planner_sees_only_the_discovery_tools_and_the_planning_method() -> None:
    script = ScriptedModel(calls(SAMPLE_CALL), plan_answer())

    plan_with(script)

    (_, info) = script.requests[0]
    assert {tool.name for tool in info.function_tools} == {"category_fields", "search_listings", "sample_descriptions"}
    assert [tool.name for tool in info.output_tools] == [PLAN_OUTPUT_TOOL_NAME]
    assert info.output_tools[0].strict is True  # the provider enforces the plan's schema
    assert info.instructions.startswith("You plan a search of Carousell Singapore listings for one request.")
    assert "- under_benchmark: Listings priced well below" in info.instructions
    assert "Planning budget: 6 tool calls and 2 web searches" in info.instructions
    assert user_prompt_of(script) == f"Request:\n{REQUEST}"


def test_search_pages_read_while_planning_land_in_the_runs_listings_db(tmp_path: Path) -> None:
    search = ("search_listings", {"request": {"query": "table"}, "limit": 5, "fields": ["id", "title"]})
    plan_with(ScriptedModel(calls(search), plan_answer()), run_directory=tmp_path)

    count = sqlite3.connect(tmp_path / "listings.db").execute("SELECT count(*) FROM listings").fetchone()[0]
    assert count == 5


def test_a_plan_that_does_not_fit_together_is_sent_back_to_be_fixed() -> None:
    broken = example_plan(benchmark=BenchmarkSpec(population="all", grouping=["colour"], statistic="median"))
    script = ScriptedModel(calls(SAMPLE_CALL), plan_answer(broken), plan_answer())

    outcome = plan_with(script)

    retry_prompt = script.requests[2][0][-1].parts[0]
    assert "Fix the plan: Benchmark grouping names attributes that the plan does not define: colour." in str(
        retry_prompt.content
    )
    assert outcome.plan == EXAMPLE_PLAN


def test_the_planning_budget_refuses_the_seventh_tool_call() -> None:
    carousell = FakeCarousellSource()
    seven_samples = calls(*[SAMPLE_CALL] * 7)
    script = ScriptedModel(seven_samples, plan_answer())

    outcome = plan_with(script, carousell)

    assert [call.status for call in outcome.tool_trace].count("refused") == 1
    assert len(carousell.search_requests) == 6
    assert outcome.notes[0].startswith("Planning: Tool-call budget reached: 6 calls used, 1 more refused.")


def test_a_sample_asking_for_details_is_cut_to_the_planning_budget() -> None:
    bare = FakeCarousellSource()
    bare.search_result = bare.search_result.model_copy(update={"listings": [
        listing.model_copy(update={"description_snippet": None}) for listing in bare.search_result.listings
    ]})
    sample = ("sample_descriptions", {"request": {"query": "table"}, "n": 5, "max_detail_fetches": 5})
    settings = AgentSettings(planning_budget=AgentBudget(max_tool_calls=3, max_photos=0, max_listings=96))

    plan_with(ScriptedModel(calls(sample), plan_answer()), bare, settings)

    assert len(bare.detail_requests) == 2  # 3 calls: the sample itself and two details


def test_the_owners_notes_that_match_the_request_are_shown_labelled() -> None:
    tables = parse_note("tables.md", "---\napplies_to: table\n---\nSolid wood holds its value.")
    lamps = parse_note("lamps.md", "---\napplies_to: lamp\n---\nBulbs are extra.")
    script = ScriptedModel(plan_answer())

    outcome = plan_with(script, knowledge=[tables, lamps])

    prompt = user_prompt_of(script)
    assert "The owner's notes" in prompt and "Solid wood holds its value." in prompt
    assert "Bulbs are extra." not in prompt
    assert outcome.knowledge_files == ["tables.md"]


def test_a_revision_shows_the_previous_plan_and_the_owners_change() -> None:
    revised = example_plan(exclusions=[])
    script = ScriptedModel(plan_answer(revised))

    amendment = PlanAmendment(previous_plan=EXAMPLE_PLAN, request="Keep multi-item posts.")
    services = fake_services(FakeCarousellSource())
    outcome = asyncio.run(plan_prompt(REQUEST, services, AgentSettings(), FunctionModel(script), amendment=amendment))

    prompt = user_prompt_of(script)
    assert prompt.startswith(f"Request:\n{REQUEST}\n\nYour previous plan (JSON):\n")
    assert '"rule": "posts offering several items at one price"' in prompt
    assert prompt.endswith("The owner asks for this change; revise the plan accordingly:\nKeep multi-item posts.")
    assert outcome.plan == revised


def test_a_planner_that_never_plans_fails_with_a_clear_error() -> None:
    script = ScriptedModel(calls(SAMPLE_CALL))  # samples forever

    with pytest.raises(PlanningFailed, match="no plan within 11 model requests"):
        plan_with(script)


def test_a_saved_plan_becomes_an_outcome_that_spent_nothing() -> None:
    outcome = PlanningOutcome.from_plan(EXAMPLE_PLAN)
    assert (outcome.plan, outcome.tool_trace, outcome.usage, outcome.notes) == (EXAMPLE_PLAN, [], [], [])


def test_the_tool_results_the_planner_reads_are_the_samples() -> None:
    script = ScriptedModel(calls(SAMPLE_CALL), plan_answer())

    plan_with(script)

    returned = [part for part in script.requests[1][0][-1].parts if isinstance(part, ToolReturnPart)]
    assert returned[0].tool_name == "sample_descriptions"
    assert len(returned[0].content["samples"]) == 5


def test_a_first_plan_made_without_sampling_is_sent_back_once_with_a_reminder() -> None:
    script = ScriptedModel(plan_answer(), plan_answer())  # never samples

    outcome = plan_with(script)

    reminder = str(script.requests[1][0][-1].parts[0].content)
    assert reminder.startswith("Fix the plan: Sample the listings before planning: call sample_descriptions")
    assert len(script.requests) == 2 and outcome.plan == EXAMPLE_PLAN  # reminded once, then accepted
    assert not planning_sampled_listings(outcome.tool_trace)


def test_a_planner_that_samples_first_gets_no_reminder() -> None:
    script = ScriptedModel(calls(SAMPLE_CALL), plan_answer())

    outcome = plan_with(script)

    assert len(script.requests) == 2 and planning_sampled_listings(outcome.tool_trace)


def test_a_revision_needs_no_new_sample() -> None:
    script = ScriptedModel(plan_answer())
    amendment = PlanAmendment(previous_plan=EXAMPLE_PLAN, request="Rank by price.")

    asyncio.run(plan_prompt(REQUEST, fake_services(FakeCarousellSource()), AgentSettings(), FunctionModel(script),
                            amendment=amendment))

    assert len(script.requests) == 1


def test_the_sampling_tool_names_match_the_servers() -> None:
    assert set(LISTING_SAMPLING_TOOLS) == {SAMPLE_TOOL, SEARCH_TOOL}
