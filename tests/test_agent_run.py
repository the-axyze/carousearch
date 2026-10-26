"""run_prompt end to end, offline: a scripted FunctionModel drives the agent, whose tools are the real MCP server
(in this process) over a fake Carousell. No network, no provider key."""

from collections.abc import Callable

from pydantic_ai import models
from pydantic_ai.messages import BinaryContent, ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.agent.result import AgentAnswer
from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME, run_prompt_sync
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.mcp_server import load_usage_notes

from fake_mcp_services import DETAIL, PHOTO_URL, SEARCH_RESULT, FakeCarousellSource, fake_services, jpeg_bytes

models.ALLOW_MODEL_REQUESTS = False  # a test that reaches a real provider fails instead of spending money

MCP_TOOLS = {"search_listings", "listing_details", "listing_photo", "screening_tags", "usage_notes"}
FIRST_LISTING = SEARCH_RESULT.listings[0]
SECOND_PHOTO_URL = PHOTO_URL.replace(".jpg", "_2.jpg")

Turn = ModelResponse | Callable[[list[ModelMessage], AgentInfo], ModelResponse]


class ScriptedModel:
    """A FunctionModel function: plays its turns in order (the last one repeats) and keeps every request."""

    def __init__(self, *turns: Turn) -> None:
        self.turns = turns
        self.requests: list[tuple[list[ModelMessage], AgentInfo]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append((list(messages), info))
        turn = self.turns[min(len(self.requests), len(self.turns)) - 1]
        return turn(messages, info) if callable(turn) else turn

    def tool_returns_seen_last(self) -> list[ToolReturnPart]:
        """The tool results in the last request the agent sent the model."""
        messages, _ = self.requests[-1]
        last_request = messages[-1]
        assert isinstance(last_request, ModelRequest)
        return [part for part in last_request.parts if isinstance(part, ToolReturnPart)]


def calls(*tool_calls: tuple[str, dict]) -> ModelResponse:
    """One model turn with these tool calls (several run in parallel)."""
    return ModelResponse(parts=[ToolCallPart(name, arguments) for name, arguments in tool_calls])


def final_answer(notes: str = "", kept: list[dict] | None = None, dropped: list[dict] | None = None) -> ModelResponse:
    arguments = {"kept": kept or [], "dropped": dropped or [], "notes": notes}
    return ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, arguments)])


def ask(
    script: ScriptedModel, carousell: FakeCarousellSource | None = None, budget: AgentBudget | None = None
) -> AgentAnswer:
    settings = AgentSettings(budget=budget or AgentBudget())  # no API key: the scripted model needs none
    services = fake_services(carousell or FakeCarousellSource())
    return run_prompt_sync("Find a used iPhone 15 under S$700.", services, settings, model=FunctionModel(script))


SEARCH_CALL = ("search_listings", {"request": {"query": "iphone 15", "price_max": 700}, "limit": 5})
DETAILS_CALL = ("listing_details", {"listing_id": DETAIL.id})
KEPT = {
    "listing_id": FIRST_LISTING.id,
    "title": FIRST_LISTING.title,
    "price": FIRST_LISTING.price,
    "url": FIRST_LISTING.listing_url,
    "seller": FIRST_LISTING.seller_username,
    "reason": "Single phone, within budget, seller rated 5.0",
}
DROPPED = {"listing_id": SEARCH_RESULT.listings[1].id, "title": SEARCH_RESULT.listings[1].title, "reason": "over S$700"}


def test_the_agent_offers_exactly_the_five_mcp_tools_and_reads_the_usage_notes_as_instructions() -> None:
    script = ScriptedModel(final_answer())

    ask(script)

    (_, info) = script.requests[0]
    assert {tool.name for tool in info.function_tools} == MCP_TOOLS
    assert [tool.name for tool in info.output_tools] == [OUTPUT_TOOL_NAME]
    assert "shopping assistant for Carousell Singapore" in info.instructions
    assert "40 tool calls in total, 200 listings from search pages, 15 photos" in info.instructions
    assert load_usage_notes().strip() in info.instructions


def test_a_scripted_run_searches_then_fetches_details_and_returns_an_agent_answer() -> None:
    carousell = FakeCarousellSource()
    script = ScriptedModel(calls(SEARCH_CALL), calls(DETAILS_CALL), final_answer("Prices checked.", [KEPT], [DROPPED]))

    answer = ask(script, carousell)

    assert isinstance(answer, AgentAnswer)
    assert [item.listing_id for item in answer.kept] == [FIRST_LISTING.id]
    assert answer.kept[0].url == "https://www.carousell.sg/p/1469923094/"
    assert [(item.listing_id, item.reason) for item in answer.dropped] == [(DROPPED["listing_id"], "over S$700")]
    assert answer.notes == "Prices checked."
    assert [(request.query, request.page_size) for request in carousell.search_requests] == [("iphone 15", 5)]
    assert carousell.detail_requests == [DETAIL.id]
    search_result = script.requests[1][0][-1].parts[0]
    assert [listing["id"] for listing in search_result.content["listings"]] == [listing.id for listing in SEARCH_RESULT.listings]


def test_the_tool_trace_lists_the_calls_in_order_with_their_arguments() -> None:
    script = ScriptedModel(calls(SEARCH_CALL), calls(DETAILS_CALL), final_answer())

    trace = ask(script).tool_trace

    assert [(call.tool_name, call.arguments, call.status) for call in trace] == [
        ("search_listings", 'query="iphone 15" price_max=700 limit=5', "ok"),
        ("listing_details", f"listing_id={DETAIL.id}", "ok"),
    ]
    assert all(call.duration_seconds >= 0 for call in trace)


def test_the_tool_call_cap_refuses_the_next_call_and_the_answer_notes_say_so() -> None:
    carousell = FakeCarousellSource()
    script = ScriptedModel(calls(SEARCH_CALL), calls(DETAILS_CALL), calls(("screening_tags", {"listing_id": "99"})),
                           final_answer("Stopped early."))

    answer = ask(script, carousell, AgentBudget(max_tool_calls=2))

    (refusal,) = script.tool_returns_seen_last()
    assert refusal.outcome == "failed"
    assert "tool-call budget of 2 calls is used up" in refusal.content
    assert [call.status for call in answer.tool_trace] == ["ok", "ok", "refused"]
    assert carousell.detail_requests == [DETAIL.id]  # the refused call never reached Carousell
    assert answer.notes == "Stopped early.\nTool-call budget reached: 2 calls used, 1 more refused."


def test_a_model_that_ignores_refusals_is_stopped_and_the_notes_say_so() -> None:
    carousell = FakeCarousellSource()
    script = ScriptedModel(calls(SEARCH_CALL))  # searches forever

    answer = ask(script, carousell, AgentBudget(max_tool_calls=3))

    assert len(script.requests) == 3 + 5  # the budget plus EXTRA_MODEL_REQUESTS
    assert len(carousell.search_requests) == 3
    assert (answer.kept, answer.dropped) == ([], [])
    assert answer.notes.startswith("Stopped after 8 model requests without an answer")
    assert "Tool-call budget reached: 3 calls used" in answer.notes


def test_the_photo_cap_refuses_photos_beyond_it() -> None:
    carousell = FakeCarousellSource(photo=jpeg_bytes(400, 300))
    script = ScriptedModel(calls(("listing_photo", {"url": PHOTO_URL}), ("listing_photo", {"url": SECOND_PHOTO_URL})),
                           final_answer())

    answer = ask(script, carousell, AgentBudget(max_photos=1))

    assert len(carousell.photo_requests) == 1
    assert sorted(call.status for call in answer.tool_trace) == ["ok", "refused"]
    assert sorted(part.outcome for part in script.tool_returns_seen_last()) == ["failed", "success"]
    assert "Photo budget reached: 1 photo judged, 1 more refused." in answer.notes


def test_the_model_receives_a_photo_as_an_image() -> None:
    script = ScriptedModel(calls(("listing_photo", {"url": PHOTO_URL, "max_width": 200})), final_answer())

    ask(script, FakeCarousellSource(photo=jpeg_bytes(400, 300)))

    (photo,) = script.tool_returns_seen_last()
    assert isinstance(photo.content, BinaryContent)
    assert photo.content.is_image and photo.content.media_type == "image/jpeg"


def test_a_search_is_cut_to_the_listing_budget() -> None:
    carousell = FakeCarousellSource()
    script = ScriptedModel(calls(("search_listings", {"request": {"query": "iphone 15"}, "limit": 48})), final_answer())

    answer = ask(script, carousell, AgentBudget(max_listings=3))

    assert [request.page_size for request in carousell.search_requests] == [3]
    assert answer.tool_trace[0].arguments == 'query="iphone 15" limit=3'
    assert "Listing budget reached: 3 listings pulled from search pages (1 page cut short" in answer.notes


def test_a_carousell_block_reaches_the_model_as_a_failed_result_and_the_run_still_answers() -> None:
    carousell = FakeCarousellSource(search_result=CarousellBlockedError("rate limited by Carousell (HTTP 429)"))
    script = ScriptedModel(calls(SEARCH_CALL), final_answer("Carousell is blocking requests."))

    answer = ask(script, carousell)

    (failure,) = script.tool_returns_seen_last()
    assert failure.outcome == "failed"
    assert "blocking or rate-limiting" in failure.content
    assert [call.status for call in answer.tool_trace] == ["failed"]
    assert answer.notes == "Carousell is blocking requests."
