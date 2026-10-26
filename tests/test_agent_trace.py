"""The tool trace (pure): built from a message history and the hook's records."""

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from carousearch.agent.result import ToolCall
from carousearch.agent.trace import CallRecord, build_tool_trace, summarise_arguments

SEARCH_ARGUMENTS = {"request": {"query": "nintendo switch", "price_max": 200, "deal_methods": ["meetup"]}, "limit": 48}


def test_search_arguments_are_flattened_and_empty_values_left_out() -> None:
    arguments = {**SEARCH_ARGUMENTS, "request": {**SEARCH_ARGUMENTS["request"], "conditions": [], "category": None}}

    assert summarise_arguments(arguments) == 'query="nintendo switch" price_max=200 deal_methods=meetup limit=48'


def test_a_photo_url_is_shortened_to_its_file_name() -> None:
    arguments = {"url": "https://media.karousell.com/media/photos/products/2026/10/2/switch_1_ab12.jpg", "max_width": 400}

    assert summarise_arguments(arguments) == "url=switch_1_ab12.jpg max_width=400"


def test_a_long_summary_is_cut_to_100_characters() -> None:
    summary = summarise_arguments({"request": {"query": "x" * 200}})

    assert len(summary) == 100
    assert summary.endswith("…")


def test_the_trace_lists_tool_calls_in_order_without_the_answer_tool() -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart("find a switch")]),
        ModelResponse(parts=[TextPart("Searching."), ToolCallPart("search_listings", SEARCH_ARGUMENTS, "call-1")]),
        ModelRequest(parts=[ToolReturnPart("search_listings", {"listings": []}, "call-1")]),
        ModelResponse(
            parts=[
                ToolCallPart("listing_details", {"listing_id": "11"}, "call-2"),
                ToolCallPart("listing_photo", '{"url": "https://media.karousell.com/p/a.jpg"}', "call-3"),
            ]
        ),
        ModelResponse(parts=[ToolCallPart("final_answer", {"kept": [], "dropped": [], "notes": ""}, "call-4")]),
    ]
    records = {
        "call-1": CallRecord(1.25, "ok", {**SEARCH_ARGUMENTS, "limit": 12}),
        "call-2": CallRecord(0.5, "failed", {"listing_id": "11"}),
        "call-3": CallRecord(0.0, "refused", {"url": "https://media.karousell.com/p/a.jpg"}),
    }

    trace = build_tool_trace(messages, records, output_tool_name="final_answer")

    assert trace == [
        ToolCall(
            tool_name="search_listings",
            arguments='query="nintendo switch" price_max=200 deal_methods=meetup limit=12',  # as sent, after the budget
            duration_seconds=1.25,
            status="ok",
        ),
        ToolCall(tool_name="listing_details", arguments="listing_id=11", duration_seconds=0.5, status="failed"),
        ToolCall(tool_name="listing_photo", arguments="url=a.jpg", duration_seconds=0.0, status="refused"),
    ]


def test_a_call_that_never_reached_the_hook_is_marked_not_run() -> None:
    messages = [ModelResponse(parts=[ToolCallPart("listing_details", "{not json", "call-1")])]

    (entry,) = build_tool_trace(messages, {}, output_tool_name="final_answer")

    assert (entry.tool_name, entry.status, entry.duration_seconds) == ("listing_details", "not run", 0.0)
