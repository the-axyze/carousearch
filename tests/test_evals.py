"""The cross-category eval set: the shipped prompts, the generic plan checks (pure), the runner, and the offline
check of every prompt against a scripted planner's plan."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.models.function import FunctionModel

from carousearch.agent.evals import (
    DEFAULT_EVAL_FILE,
    BlockWatch,
    EvalOutcome,
    EvalPrompt,
    attribute_sources,
    check_plan,
    load_eval_prompts,
    outcome_summary,
    run_eval_prompts,
)
from carousearch.agent.plan import AttributeSpec
from carousearch.agent.planning import plan_prompt_sync
from carousearch.agent.result import AgentAnswer
from carousearch.agent.settings import AgentSettings
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.filters.schema import SearchRequest

from fake_mcp_services import FakeCarousellSource, fake_services
from plan_examples import EXAMPLE_PLAN, example_plan
from scripted_model import ScriptedModel, calls, plan_answer

# A plan that has every generic property the eval checks for: what a scripted planner returns offline.
FULL_PLAN = example_plan(attributes=[
    *EXAMPLE_PLAN.attributes,
    AttributeSpec(name="condition", purpose="at least lightly used", source="structured_filter",
                  detail="request.conditions = like_new, lightly_used"),
])


def test_the_shipped_eval_set_spans_at_least_six_unrelated_requests() -> None:
    prompts = load_eval_prompts(DEFAULT_EVAL_FILE)

    assert len(prompts) >= 6
    assert {"phones_under_benchmark", "office_chair_budget", "neglected_cameras", "console_variants",
            "owner_r15_benchmark", "owner_motorcycles_below_median", "vague_request"} <= {p.name for p in prompts}
    assert len({prompt.name for prompt in prompts}) == len(prompts)


def test_an_unknown_check_is_rejected_when_the_file_is_read() -> None:
    with pytest.raises(ValidationError, match="unknown plan checks: has_magic"):
        EvalPrompt(name="x", prompt="y", expect=["has_magic"])
    with pytest.raises(ValidationError):
        EvalPrompt(name="x", prompt="y", expect=["source:guesswork"])


def test_the_generic_checks_pass_on_a_complete_plan_and_say_why_they_fail() -> None:
    names = ["has_benchmark", "benchmark_grouped", "has_exclusion_with_reason", "has_assumptions",
             "states_value_drivers", "ranking_stated", "source:text_extraction", "template:under_benchmark"]
    assert all(result.passed for result in check_plan(EXAMPLE_PLAN, names))

    bare = example_plan(benchmark=None, exclusions=[], assumptions=[], value_drivers=["price"], template=None)
    results = {result.name: result for result in check_plan(bare, names)}
    assert [name for name, result in results.items() if not result.passed] == [
        "has_benchmark", "benchmark_grouped", "has_exclusion_with_reason", "has_assumptions", "states_value_drivers",
        "template:under_benchmark",
    ]
    assert results["source:text_extraction"].description == "an attribute comes from text extraction"


@pytest.mark.parametrize("prompt", load_eval_prompts(DEFAULT_EVAL_FILE), ids=lambda prompt: prompt.name)
def test_every_eval_prompt_is_checked_offline_against_a_scripted_planners_plan(prompt: EvalPrompt) -> None:
    script = ScriptedModel(calls(("sample_descriptions", {"request": {"query": "x"}, "n": 5})), plan_answer(FULL_PLAN))

    outcome = plan_prompt_sync(prompt.prompt, fake_services(FakeCarousellSource()), AgentSettings(),
                               FunctionModel(script))

    results = check_plan(outcome.plan, prompt.expect)
    assert [result.name for result in results if not result.passed] == []
    assert prompt.prompt in str(script.requests[0][0][0].parts)


def test_a_plan_without_a_grouped_benchmark_fails_the_benchmark_prompts() -> None:
    prompts = {prompt.name: prompt for prompt in load_eval_prompts(DEFAULT_EVAL_FILE)}
    ungrouped = example_plan(benchmark=EXAMPLE_PLAN.benchmark.model_copy(update={"grouping": []}))

    failed = [result.name for result in check_plan(ungrouped, prompts["phones_under_benchmark"].expect)
              if not result.passed]

    assert failed == ["benchmark_grouped"]


def answer_for(plan=FULL_PLAN) -> AgentAnswer:
    return AgentAnswer(kept=[], dropped=[], notes="n", tool_trace=[], plan=plan,
                       plan_review=[{"item": "ranking", "status": "done", "note": "ok"}])


def test_the_runner_records_failures_and_stops_once_told_to() -> None:
    prompts = [EvalPrompt(name=name, prompt=name, expect=["has_benchmark"]) for name in ("a", "b", "c")]
    seen: list[str] = []
    blocked = {"now": False}

    def run_one(prompt: EvalPrompt) -> AgentAnswer:
        if prompt.name == "a":
            raise RuntimeError("model down")
        blocked["now"] = True
        return answer_for()

    outcomes = run_eval_prompts(prompts, run_one, should_stop=lambda: blocked["now"],
                                on_outcome=lambda outcome: seen.append(outcome.prompt.name))

    assert [outcome.error for outcome in outcomes] == [
        "RuntimeError: model down", None, "skipped: Carousell blocked an earlier request"
    ]
    assert outcomes[1].checks[0].passed and seen == ["a", "b", "c"]


def test_an_outcome_summary_is_plain_data_with_the_plan_checks_review_and_cost() -> None:
    prompt = EvalPrompt(name="a", prompt="find a", expect=["has_benchmark"])
    summary = outcome_summary(run_eval_prompts([prompt], lambda prompt: answer_for())[0])

    assert json.loads(json.dumps(summary))["checks"] == [{"name": "has_benchmark", "passed": True}]
    assert summary["plan"]["item_class"] == EXAMPLE_PLAN.item_class and summary["cost_usd"] == 0.0
    assert outcome_summary(EvalOutcome(prompt, None, "boom")) == {"name": "a", "prompt": "find a", "error": "boom"}
    assert attribute_sources(FULL_PLAN) == {"structured_filter": 1, "card_field": 1, "text_extraction": 1,
                                            "derived": 1}


def test_block_watch_notices_a_block_and_passes_everything_else_through() -> None:
    blocked = BlockWatch(FakeCarousellSource(search_result=CarousellBlockedError("HTTP 429")))
    fine = BlockWatch(FakeCarousellSource())

    with pytest.raises(CarousellBlockedError):
        blocked.search(SearchRequest(query="x"))
    fine.search(SearchRequest(query="x"))

    assert blocked.blocked and not fine.blocked
    assert fine.get_listing("1").id and fine.get_category_filters("1")


def test_a_custom_eval_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"prompts": [{"name": "one", "prompt": "p", "expect": ["ranking_stated"]}]}))

    assert [prompt.name for prompt in load_eval_prompts(path)] == ["one"]
