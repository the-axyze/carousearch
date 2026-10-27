"""The search plan: how the agent means to answer a request, decided before any listing is judged, and the review it
returns against that plan afterwards.

SearchPlan is the planning phase's structured output (agent/planning.py) and the execution phase's brief. The CLI
prints and saves it (`ask --plan-only`) and reads an edited copy back (`ask --plan-file`); the web app shows it
before Execute. PlanItemStatus is one line of the executor's self-check against it.

Pure: the models, plan_problems (cross-field checks the planner is asked to fix), review_items and
complete_plan_review (every plan item ends up with a status) and review_item_label.
"""

from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ATTRIBUTE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
NOT_REPORTED_NOTE = "The agent did not report on this item."


class AttributeSource(StrEnum):
    """Where an attribute's value comes from."""

    STRUCTURED_FILTER = "structured_filter"  # a Carousell search filter, applied server-side
    CARD_FIELD = "card_field"  # a field of the search result or the listing detail (price, posted time, photos)
    TEXT_EXTRACTION = "text_extraction"  # stated by sellers in titles or descriptions (extract_fields)
    DERIVED = "derived"  # computed in run_python from other attributes
    EXTERNAL_FACT = "external_fact"  # a fact from general knowledge, web search or the owner's notes


class AttributeSpec(BaseModel):
    name: str = Field(
        pattern=ATTRIBUTE_NAME_PATTERN,
        description="snake_case, at most 40 characters; extracted values land in listings.db column x_<name>.",
    )
    purpose: str = Field(min_length=1, description="Why the goal needs this attribute.")
    source: AttributeSource
    detail: str = Field(
        min_length=1,
        description="structured_filter: the filter's field name and value; card_field: the field; text_extraction: "
        "the extraction hint and, when sellers phrase it regularly, a regex; derived: the formula; external_fact: "
        "the fact and where it came from.",
    )
    unit: str | None = Field(default=None, description="The unit of the value, when it has one.")


class BenchmarkSpec(BaseModel):
    population: str = Field(min_length=1, description="Which listings the benchmark is computed over.")
    grouping: list[str] = Field(
        default_factory=list,
        description="Attribute names to group by so like is compared with like; empty for one group.",
    )
    statistic: str = Field(min_length=1, description='For example "median" or "25th percentile".')
    normalisation: str | None = Field(
        default=None, description='A formula that puts listings on one scale, e.g. "price / x_size"; null for none.'
    )


class Exclusion(BaseModel):
    rule: str = Field(min_length=1, description="Which listings are left out, checkable on the data.")
    reason: str = Field(min_length=1, description="Why leaving them out serves the goal.")


class BudgetEstimate(BaseModel):
    searches: int = Field(ge=0, description="Search pages.")
    details: int = Field(ge=0, description="Listing details fetched (also by extract_fields and judge_listings).")
    photos: int = Field(ge=0)
    judgements: int = Field(ge=0, description="Worker-model calls: extract_fields and judge batches.")


class SearchPlan(BaseModel):
    """How the request will be answered: what is searched, what drives value, which attributes are needed and where
    each comes from, how prices are compared, what is left out and why, and how the result is ranked."""

    goal: str = Field(min_length=1, description="The request restated as a checkable goal.")
    item_class: str = Field(min_length=1, description="What is being searched, in your own words.")
    value_drivers: list[str] = Field(description="What makes one listing of this kind worth more than another.")
    attributes: list[AttributeSpec] = Field(min_length=1)
    benchmark: BenchmarkSpec | None = Field(description="How prices are compared; null when the goal needs none.")
    exclusions: list[Exclusion] = Field(default_factory=list)
    ranking: str = Field(min_length=1, description="How the kept listings are ordered.")
    template: str | None = Field(default=None, description="The method template followed, or null.")
    budget_estimate: BudgetEstimate
    assumptions: list[str] = Field(default_factory=list, description="Every guess made about the request.")


ReviewStatus = Literal["done", "partial", "not_done"]


class PlanItemStatus(BaseModel):
    """The executor's report on one plan item."""

    item: str = Field(description='The review item key, e.g. "attribute:<name>", "benchmark", "exclusion:1".')
    status: ReviewStatus
    note: str = Field(description="What was done, with the numbers, or why it was not.")


def plan_problems(plan: SearchPlan, template_names: Sequence[str]) -> list[str]:
    """What is inconsistent in the plan, one sentence each; empty when it is usable."""
    problems = []
    names = [attribute.name for attribute in plan.attributes]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        problems.append(f"Attribute names must be unique; repeated: {', '.join(duplicates)}.")
    if plan.benchmark is not None:
        unknown = [name for name in plan.benchmark.grouping if name not in names]
        if unknown:
            problems.append(f"Benchmark grouping names attributes that the plan does not define: {', '.join(unknown)}.")
    if plan.template is not None and plan.template not in template_names:
        problems.append(f"Unknown template {plan.template!r}; use one of {', '.join(template_names)} or null.")
    extracted = [attribute.name for attribute in plan.attributes if attribute.source == AttributeSource.TEXT_EXTRACTION]
    if extracted and plan.budget_estimate.judgements == 0:
        problems.append(
            f"The text_extraction attributes ({', '.join(extracted)}) need worker calls (extract_fields); "
            "count them in budget_estimate.judgements."
        )
    return problems


def review_items(plan: SearchPlan) -> list[str]:
    """The keys the executor's plan_review must cover, in plan order."""
    items = [f"attribute:{attribute.name}" for attribute in plan.attributes]
    if plan.benchmark is not None:
        items.append("benchmark")
    items += [f"exclusion:{number}" for number in range(1, len(plan.exclusions) + 1)]
    items.append("ranking")
    return items


def complete_plan_review(plan: SearchPlan, review: Sequence[PlanItemStatus]) -> list[PlanItemStatus]:
    """One status per review item, in plan order: the executor's own, or not_done when it left one out. Statuses for
    keys the plan does not have are kept at the end."""
    by_key = {normalise_review_key(status.item): status for status in review}
    expected = review_items(plan)
    completed = [
        by_key.get(key) or PlanItemStatus(item=key, status="not_done", note=NOT_REPORTED_NOTE) for key in expected
    ]
    extra = [status for status in review if normalise_review_key(status.item) not in expected]
    return completed + extra


def normalise_review_key(item: str) -> str:
    """ "Attribute: Storage_GB " -> "attribute:storage_gb"."""
    kind, separator, rest = item.strip().lower().partition(":")
    return f"{kind.strip()}:{rest.strip()}" if separator else kind.strip()


def review_item_label(plan: SearchPlan | None, item: str) -> str:
    """A readable label for a review key: "Attribute x", "Exclusion 2: <rule>", "Benchmark", "Ranking"."""
    kind, _, rest = normalise_review_key(item).partition(":")
    if kind == "exclusion" and rest.isdigit() and plan is not None and 0 < int(rest) <= len(plan.exclusions):
        return f"Exclusion {rest}: {plan.exclusions[int(rest) - 1].rule}"
    if kind in {"attribute", "exclusion"} and rest:
        return f"{kind.capitalize()} {rest}"
    return item.strip().capitalize() if kind not in {"benchmark", "ranking"} else kind.capitalize()
