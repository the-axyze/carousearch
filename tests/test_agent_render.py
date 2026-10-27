"""The agent's answer: result shaping and the `carousearch ask` text output (pure)."""

from carousearch.agent.cost import ModelUsage
from carousearch.agent.result import (
    AgentAnswer,
    DroppedListing,
    KeptListing,
    ModelAnswer,
    ToolCall,
    build_agent_answer,
    tool_call_counts,
)
from carousearch.text_render import describe_benchmark, render_agent_answer, render_plan

from plan_examples import EXAMPLE_PLAN, example_plan

KEPT = KeptListing(
    listing_id="1469923094",
    title="Nintendo Switch V2 with box",
    price=180.0,
    url="https://www.carousell.sg/p/1469923094/",
    seller="gamer_sg",
    reason="312 likes; the photo shows a real used unit on a desk, with scuffs on the dock",
)
DROPPED = DroppedListing(listing_id="1463757293", title="Switch Lite / Switch OLED / Switch V2", reason="names three models")
TRACE = [
    ToolCall(tool_name="search_listings", arguments='query="nintendo switch" limit=48', duration_seconds=1.31, status="ok"),
    ToolCall(tool_name="listing_photo", arguments="url=a.jpg", duration_seconds=0.0, status="refused"),
]


USAGE = [ModelUsage(role="planner", model="openai:gpt-5.4-mini", requests=3, input_tokens=12_000,
                    cached_input_tokens=8_000, output_tokens=900, estimated_cost_usd=0.0076)]


def test_budget_notes_are_appended_to_the_models_notes() -> None:
    model_answer = ModelAnswer(kept=[KEPT], dropped=[DROPPED], not_shortlisted=9, notes="Assumed V2 means the 2019 model. ")

    answer = build_agent_answer(
        model_answer, TRACE, ["Photo budget reached: 15 photos judged, 1 more refused."], USAGE
    )

    assert answer.notes == "Assumed V2 means the 2019 model.\nPhoto budget reached: 15 photos judged, 1 more refused."
    assert (answer.kept, answer.dropped, answer.tool_trace) == ([KEPT], [DROPPED], TRACE)
    assert (answer.not_shortlisted, answer.usage) == (9, USAGE)


def test_empty_model_notes_leave_only_the_budget_notes() -> None:
    answer = build_agent_answer(ModelAnswer(kept=[], dropped=[], notes=""), [], ["Tool-call budget reached."])

    assert answer.notes == "Tool-call budget reached."


def test_text_output_has_the_trace_then_kept_then_dropped_then_notes() -> None:
    answer = AgentAnswer(
        kept=[KEPT], dropped=[DROPPED], not_shortlisted=12, notes="Check the dock in person.", tool_trace=TRACE,
        usage=USAGE,
    )

    lines = render_agent_answer(answer).splitlines()

    assert lines[0] == "Tool calls (2): search_listings 1, listing_photo 1:"
    assert lines[1] == '  1.   1.3 s  search_listings  query="nintendo switch" limit=48'
    assert lines[2] == "  2.   0.0 s  listing_photo    url=a.jpg  [refused]"
    assert lines[4] == "Kept (1):"
    assert lines[5].split()[:4] == ["PRICE", "TITLE", "SELLER", "URL"] and lines[5].rstrip().endswith("REASON")
    assert lines[6].startswith("S$180      Nintendo Switch V2 with box")  # PRICE is 9 wide, then two spaces
    assert "gamer_sg" in lines[6] and KEPT.url in lines[6]
    assert lines[6].endswith(KEPT.reason)  # the reason comes last and is never cut
    assert lines[8] == "Dropped (1):"
    assert lines[9].startswith("  1463757293  Switch Lite / Switch OLED / Switch V2")
    assert lines[9].endswith("names three models")
    assert lines[11] == "Not shortlisted: 12 more listings seen."
    assert lines[13:15] == ["Notes:", "  Check the dock in person."]
    assert lines[16].startswith("Cost (estimate, list prices of 2026-10-27): about US$0.0076. planner")


def test_text_output_for_an_empty_answer() -> None:
    text = render_agent_answer(AgentAnswer(kept=[], dropped=[], notes="", tool_trace=[]))

    assert text == (
        "Tool calls (0):\n\nKept: none.\n\nDropped: none.\n\nNot shortlisted: 0 more listings seen.\n\n"
        "Notes: none.\n\nCost (estimate): no model requests were made."
    )


def test_tool_call_counts_put_the_busiest_tool_first() -> None:
    trace = [TRACE[0], TRACE[1], TRACE[0]]

    assert tool_call_counts(trace) == [("search_listings", 2), ("listing_photo", 1)]


def test_a_plan_without_benchmark_exclusions_or_assumptions_still_reads_well() -> None:
    plan = example_plan(benchmark=None, exclusions=[], assumptions=[], template=None)
    trace = [ToolCall(tool_name="sample_descriptions", arguments="query=x", duration_seconds=1.0, status="ok")]

    text = render_plan(plan, trace)

    assert text.startswith("Plan (1 planning tool call: sample_descriptions 1):")
    assert "Benchmark:      none" in text and "Template:       none" in text
    assert "Exclusions (0):" in text and "  - none" in text
    assert describe_benchmark(EXAMPLE_PLAN) == (
        "median over single-item sale listings with a plain price, grouped by size_cm"
    )


def test_the_answer_shows_the_review_and_the_suggested_note() -> None:
    answer = build_agent_answer(
        ModelAnswer(kept=[], dropped=[], notes="", suggested_knowledge="applies_to: table\nSizes are in titles.",
                    plan_review=[{"item": "ranking", "status": "done", "note": "by percent below"}]),
        [], [], plan=EXAMPLE_PLAN,
    )

    text = render_agent_answer(answer)

    assert "  done      Ranking: by percent below" in text
    assert "  not_done  Exclusion 1: posts offering several items at one price: The agent did not report" in text
    assert "Suggested knowledge note:\n  applies_to: table\n  Sizes are in titles." in text
