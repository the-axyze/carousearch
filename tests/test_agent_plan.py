"""The search plan model, its consistency checks and the plan review (pure)."""

import pytest
from pydantic import ValidationError

from carousearch.agent.plan import (
    NOT_REPORTED_NOTE,
    AttributeSpec,
    BenchmarkSpec,
    PlanItemStatus,
    SearchPlan,
    complete_plan_review,
    normalise_review_key,
    plan_problems,
    review_item_label,
    review_items,
)
from carousearch.agent.templates import template_names

from plan_examples import EXAMPLE_PLAN, example_plan


def test_a_plan_round_trips_through_json() -> None:
    assert SearchPlan.model_validate_json(EXAMPLE_PLAN.model_dump_json()) == EXAMPLE_PLAN


@pytest.mark.parametrize("name", ["Size", "size cm", "1size", "x" * 41, ""])
def test_attribute_names_must_be_short_snake_case(name: str) -> None:
    with pytest.raises(ValidationError):
        AttributeSpec(name=name, purpose="p", source="derived", detail="d")


def test_an_unknown_attribute_source_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AttributeSpec(name="size", purpose="p", source="guesswork", detail="d")


def test_a_plan_needs_at_least_one_attribute_and_a_ranking() -> None:
    data = EXAMPLE_PLAN.model_dump()
    with pytest.raises(ValidationError):
        SearchPlan.model_validate(data | {"attributes": []})
    with pytest.raises(ValidationError):
        SearchPlan.model_validate(data | {"ranking": ""})


def test_a_consistent_plan_has_no_problems() -> None:
    assert plan_problems(EXAMPLE_PLAN, template_names()) == []


def test_plan_problems_name_duplicates_unknown_groupings_and_unknown_templates() -> None:
    duplicated = example_plan(attributes=[*EXAMPLE_PLAN.attributes, EXAMPLE_PLAN.attributes[0]])
    ungrouped = example_plan(benchmark=BenchmarkSpec(population="all", grouping=["colour"], statistic="median"))
    untemplated = example_plan(template="magic")

    assert plan_problems(duplicated, template_names()) == ["Attribute names must be unique; repeated: price."]
    assert plan_problems(ungrouped, template_names()) == [
        "Benchmark grouping names attributes that the plan does not define: colour."
    ]
    assert "Unknown template 'magic'" in plan_problems(untemplated, template_names())[0]


def test_review_items_cover_every_attribute_the_benchmark_each_exclusion_and_the_ranking() -> None:
    assert review_items(EXAMPLE_PLAN) == [
        "attribute:price", "attribute:size_cm", "attribute:days_listed", "benchmark", "exclusion:1", "ranking"
    ]
    assert "benchmark" not in review_items(example_plan(benchmark=None, exclusions=[]))


def test_complete_plan_review_fills_missing_items_as_not_done_and_keeps_extra_ones_last() -> None:
    review = [
        PlanItemStatus(item="Attribute: Size_cm", status="partial", note="stated in 12 of 30"),
        PlanItemStatus(item="ranking", status="done", note="by percent below"),
        PlanItemStatus(item="photos", status="not_done", note="no budget left"),
    ]

    completed = complete_plan_review(EXAMPLE_PLAN, review)

    assert [(status.item, status.status) for status in completed] == [
        ("attribute:price", "not_done"), ("Attribute: Size_cm", "partial"), ("attribute:days_listed", "not_done"),
        ("benchmark", "not_done"), ("exclusion:1", "not_done"), ("ranking", "done"), ("photos", "not_done"),
    ]
    assert completed[0].note == NOT_REPORTED_NOTE


def test_review_keys_and_labels_read_well() -> None:
    assert normalise_review_key(" Attribute : Size_CM ") == "attribute:size_cm"
    assert review_item_label(EXAMPLE_PLAN, "exclusion:1") == "Exclusion 1: posts offering several items at one price"
    assert review_item_label(EXAMPLE_PLAN, "attribute:size_cm") == "Attribute size_cm"
    assert review_item_label(None, "benchmark") == "Benchmark"
    assert review_item_label(EXAMPLE_PLAN, "exclusion:9") == "Exclusion 9"


def test_a_plan_with_text_extraction_must_budget_worker_calls() -> None:
    no_worker_calls = example_plan(budget_estimate=EXAMPLE_PLAN.budget_estimate.model_copy(update={"judgements": 0}))

    (problem,) = plan_problems(no_worker_calls, template_names())

    assert problem.startswith("The text_extraction attributes (size_cm) need worker calls (extract_fields)")
