"""The agent's answer: result shaping and the `carousearch ask` text output (pure)."""

from carousearch.agent.result import AgentAnswer, DroppedListing, KeptListing, ModelAnswer, ToolCall, build_agent_answer
from carousearch.text_render import render_agent_answer

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


def test_budget_notes_are_appended_to_the_models_notes() -> None:
    model_answer = ModelAnswer(kept=[KEPT], dropped=[DROPPED], notes="Assumed V2 means the 2019 model. ")

    answer = build_agent_answer(model_answer, TRACE, ["Photo budget reached: 15 photos judged, 1 more refused."])

    assert answer.notes == "Assumed V2 means the 2019 model.\nPhoto budget reached: 15 photos judged, 1 more refused."
    assert (answer.kept, answer.dropped, answer.tool_trace) == ([KEPT], [DROPPED], TRACE)


def test_empty_model_notes_leave_only_the_budget_notes() -> None:
    answer = build_agent_answer(ModelAnswer(kept=[], dropped=[], notes=""), [], ["Tool-call budget reached."])

    assert answer.notes == "Tool-call budget reached."


def test_text_output_has_the_trace_then_kept_then_dropped_then_notes() -> None:
    answer = AgentAnswer(kept=[KEPT], dropped=[DROPPED], notes="Check the dock in person.", tool_trace=TRACE)

    lines = render_agent_answer(answer).splitlines()

    assert lines[0] == "Tool calls (2):"
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
    assert lines[11:] == ["Notes:", "  Check the dock in person."]


def test_text_output_for_an_empty_answer() -> None:
    text = render_agent_answer(AgentAnswer(kept=[], dropped=[], notes="", tool_trace=[]))

    assert text == "Tool calls (0):\n\nKept: none.\n\nDropped: none.\n\nNotes: none."
