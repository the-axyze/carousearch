"""The cross-category eval set (evals/prompts.json): prompts about unrelated kinds of items, each with the generic
plan properties a good plan has, checked against the plan the planner returns.

The checks know nothing about any item: whether a benchmark exists and is grouped, which attribute sources the plan
uses, whether it states assumptions, value drivers, exclusions with reasons and a ranking. Tests check them offline
against scripted plans; `carousearch eval --live` runs every prompt once for real through run_prompt and reports
the plan, the checks, the plan review, the tool counts and the cost.

Pure: the prompt file's model, the checks, the per-prompt summary. I/O: load_eval_prompts reads the file;
run_eval_prompts calls the injected runner (which reaches the model and Carousell); BlockWatch wraps a Carousell
source to notice a block, so a live eval stops instead of carrying on.
"""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from carousearch.agent.cost import total_cost_usd
from carousearch.agent.plan import AttributeSource, SearchPlan
from carousearch.agent.result import AgentAnswer, tool_call_counts
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.config import PROJECT_ROOT
from carousearch.filters.schema import CategoryFilter, ListingDetail, SearchRequest, SearchResult

if TYPE_CHECKING:  # the MCP SDK is imported only when a live eval runs
    from carousearch.mcp_server import CarousellSource

logger = logging.getLogger(__name__)

DEFAULT_EVAL_FILE = PROJECT_ROOT / "evals" / "prompts.json"

PlanCheck = Callable[[SearchPlan], bool]

# Named checks without a parameter; "source:<attribute source>" and "template:<name>" are parametrised.
PLAN_CHECKS: dict[str, tuple[str, PlanCheck]] = {
    "has_benchmark": ("the plan compares prices against a benchmark", lambda plan: plan.benchmark is not None),
    "benchmark_grouped": (
        "the benchmark is grouped by at least one attribute, so like is compared with like",
        lambda plan: plan.benchmark is not None and bool(plan.benchmark.grouping),
    ),
    "has_exclusion_with_reason": (
        "at least one exclusion, each with a reason",
        lambda plan: bool(plan.exclusions) and all(exclusion.reason.strip() for exclusion in plan.exclusions),
    ),
    "has_assumptions": ("the plan states its assumptions", lambda plan: bool(plan.assumptions)),
    "states_value_drivers": ("at least two value drivers", lambda plan: len(plan.value_drivers) >= 2),
    "ranking_stated": ("the ranking is stated", lambda plan: bool(plan.ranking.strip())),
}


def known_check(name: str) -> bool:
    kind, _, value = name.partition(":")
    if kind == "source":
        return value in {source.value for source in AttributeSource}
    if kind == "template":
        return bool(value)
    return name in PLAN_CHECKS


class EvalPrompt(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    prompt: str = Field(min_length=1)
    expect: list[str] = Field(min_length=1)

    @field_validator("expect")
    @classmethod
    def checks_exist(cls, expect: list[str]) -> list[str]:
        unknown = [name for name in expect if not known_check(name)]
        if unknown:
            raise ValueError(f"unknown plan checks: {', '.join(unknown)}")
        return expect


class EvalFile(BaseModel):
    about: str = ""
    prompts: list[EvalPrompt] = Field(min_length=1)


@dataclass(frozen=True)
class CheckResult:
    name: str
    description: str
    passed: bool


def check_plan(plan: SearchPlan, expectations: Sequence[str]) -> list[CheckResult]:
    """Each expected property of the plan, passed or not."""
    return [run_check(plan, name) for name in expectations]


def run_check(plan: SearchPlan, name: str) -> CheckResult:
    kind, _, value = name.partition(":")
    if kind == "source":
        passed = any(attribute.source.value == value for attribute in plan.attributes)
        return CheckResult(name, f"an attribute comes from {value.replace('_', ' ')}", passed)
    if kind == "template":
        return CheckResult(name, f"the plan follows the {value} template", plan.template == value)
    description, check = PLAN_CHECKS[name]
    return CheckResult(name, description, check(plan))


# --- one prompt's outcome -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalOutcome:
    prompt: EvalPrompt
    answer: AgentAnswer | None
    error: str | None = None  # why the run gave no answer
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return total_cost_usd(self.answer.usage) if self.answer else 0.0


def outcome_for(prompt: EvalPrompt, answer: AgentAnswer) -> EvalOutcome:
    checks = check_plan(answer.plan, prompt.expect) if answer.plan is not None else []
    return EvalOutcome(prompt=prompt, answer=answer, checks=checks)


def attribute_sources(plan: SearchPlan) -> dict[str, int]:
    """How many attributes come from each source, in AttributeSource order."""
    counts = Counter(attribute.source.value for attribute in plan.attributes)
    return {source.value: counts[source.value] for source in AttributeSource if counts[source.value]}


def review_counts(answer: AgentAnswer) -> dict[str, int]:
    counts = Counter(status.status for status in answer.plan_review)
    return {status: counts[status] for status in ("done", "partial", "not_done") if counts[status]}


def tool_counts(answer: AgentAnswer) -> dict[str, list[tuple[str, int]]]:
    return {"planning": tool_call_counts(answer.planning_trace), "execution": tool_call_counts(answer.tool_trace)}


def outcome_summary(outcome: EvalOutcome) -> dict:
    """One prompt's result as plain data, for the JSON report."""
    answer = outcome.answer
    summary: dict = {"name": outcome.prompt.name, "prompt": outcome.prompt.prompt, "error": outcome.error}
    if answer is None:
        return summary
    summary |= {
        "plan": answer.plan.model_dump(mode="json") if answer.plan else None,
        "checks": [{"name": check.name, "passed": check.passed} for check in outcome.checks],
        "plan_review": [status.model_dump(mode="json") for status in answer.plan_review],
        "kept": len(answer.kept),
        "dropped": len(answer.dropped),
        "not_shortlisted": answer.not_shortlisted,
        "tool_counts": tool_counts(answer),
        "usage": [usage.model_dump(mode="json") for usage in answer.usage],
        "cost_usd": outcome.cost_usd,
        "notes": answer.notes,
        "suggested_knowledge": answer.suggested_knowledge,
    }
    return summary


# --- running (I/O) ------------------------------------------------------------------------------------------------


def load_eval_prompts(path: Path = DEFAULT_EVAL_FILE) -> list[EvalPrompt]:
    """The prompts of an eval file; a malformed file raises ValidationError, a missing one OSError."""
    return EvalFile.model_validate_json(path.read_text(encoding="utf-8")).prompts


class BlockWatch:
    """A Carousell source that remembers whether Carousell blocked a request, so a live eval stops early."""

    def __init__(self, source: "CarousellSource") -> None:
        self.source = source
        self.blocked = False

    def search(self, request: SearchRequest) -> SearchResult:
        return self._watch(lambda: self.source.search(request))

    def get_listing(self, listing_id: str) -> ListingDetail:
        return self._watch(lambda: self.source.get_listing(listing_id))

    def get_photo(self, url: str) -> bytes:
        return self._watch(lambda: self.source.get_photo(url))

    def get_category_filters(self, category_id: str) -> list[CategoryFilter]:
        return self._watch(lambda: self.source.get_category_filters(category_id))

    def _watch[Result](self, call: Callable[[], Result]) -> Result:
        try:
            return call()
        except CarousellBlockedError:
            self.blocked = True
            raise


def run_eval_prompts(
    prompts: Sequence[EvalPrompt],
    run_one: Callable[[EvalPrompt], AgentAnswer],
    should_stop: Callable[[], bool] = lambda: False,
    on_outcome: Callable[[EvalOutcome], None] = lambda outcome: None,
) -> list[EvalOutcome]:
    """Run each prompt once, in order. A run that raises is recorded with its error and the next prompt goes ahead;
    once `should_stop()` (for example Carousell blocked us), the remaining prompts are skipped."""
    outcomes = []
    for prompt in prompts:
        if should_stop():
            outcome = EvalOutcome(prompt=prompt, answer=None, error="skipped: Carousell blocked an earlier request")
        else:
            try:
                outcome = outcome_for(prompt, run_one(prompt))
            except Exception as error:  # one prompt's failure must not lose the others' results
                logger.exception("Eval prompt %s failed", prompt.name)
                outcome = EvalOutcome(prompt=prompt, answer=None, error=f"{type(error).__name__}: {error}")
        on_outcome(outcome)
        outcomes.append(outcome)
    return outcomes
