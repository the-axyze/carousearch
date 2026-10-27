"""The method templates (pure): generic, complete, and fillable."""

import re

from carousearch.agent.templates import METHOD_TEMPLATES, find_template, template_names, templates_for_planner


def test_the_five_templates_are_offered_by_name() -> None:
    assert template_names() == [
        "under_benchmark", "best_under_budget", "neglected_listings", "compare_variants", "watch_for"
    ]
    assert find_template("compare_variants").slots[1] == "variant_attribute"
    assert find_template("nothing") is None and find_template(None) is None


def test_every_method_sentence_uses_exactly_its_slots() -> None:
    for template in METHOD_TEMPLATES:
        assert set(re.findall(r"\{(\w+)\}", template.method)) == set(template.slots), template.name


def test_every_example_request_has_placeholders_and_no_unfilled_slot() -> None:
    for template in METHOD_TEMPLATES:
        assert "<item>" in template.example_request, template.name
        assert "{" not in template.example_request


def test_the_planner_sees_one_line_per_template() -> None:
    lines = templates_for_planner().splitlines()
    assert len(lines) == len(METHOD_TEMPLATES)
    assert lines[0].startswith("- under_benchmark: Listings priced well below")
