"""run_prompt end to end, offline: a scripted FunctionModel drives the agent, whose tools are the real MCP server
(in this process) over a fake Carousell. No network, no provider key."""

import logging
from collections.abc import Callable

import pytest
from pydantic_ai import models
from pydantic_ai.messages import BinaryContent
from pydantic_ai.models.function import FunctionModel

from carousearch.agent.progress import ToolCallEvent
from carousearch.agent.result import AgentAnswer
from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME, run_prompt_sync
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.mcp_server import load_usage_notes

from fake_mcp_services import DETAIL, PHOTO_URL, SEARCH_RESULT, FakeCarousellSource, fake_services, jpeg_bytes
from scripted_model import ScriptedModel, Turn, calls, final_answer

models.ALLOW_MODEL_REQUESTS = False  # a test that reaches a real provider fails instead of spending money

MCP_TOOLS = {"search_listings", "listing_details", "listing_photo", "screening_tags", "usage_notes"}
FIRST_LISTING = SEARCH_RESULT.listings[0]
SECOND_PHOTO_URL = PHOTO_URL.replace(".jpg", "_2.jpg")


def ask(
    script: ScriptedModel,
    carousell: FakeCarousellSource | None = None,
    budget: AgentBudget | None = None,
    observer: Callable[[ToolCallEvent], None] | None = None,
) -> AgentAnswer:
    settings = AgentSettings(budget=budget or AgentBudget())  # no API key: the scripted model needs none
    services = fake_services(carousell or FakeCarousellSource())
    return run_prompt_sync(
        "Find a used iPhone 15 under S$700.", services, settings, model=FunctionModel(script), observer=observer
    )


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


# --- progress observer ----------------------------------------------------------------------------------------


def observed_run(
    *turns: Turn, carousell: FakeCarousellSource | None = None, budget: AgentBudget | None = None
) -> tuple[AgentAnswer, list[ToolCallEvent]]:
    events: list[ToolCallEvent] = []
    answer = ask(ScriptedModel(*turns), carousell, budget, observer=events.append)
    return answer, events


def test_the_observer_sees_each_call_start_and_finish_in_order_with_the_budget_used() -> None:
    _, events = observed_run(calls(SEARCH_CALL), calls(DETAILS_CALL), final_answer())

    assert [(event.phase, event.tool_name, event.status) for event in events] == [
        ("started", "search_listings", None),
        ("finished", "search_listings", "ok"),
        ("started", "listing_details", None),
        ("finished", "listing_details", "ok"),
    ]
    assert [event.budget.tool_calls_used for event in events] == [1, 1, 2, 2]
    assert {(event.budget.max_tool_calls, event.budget.max_photos) for event in events} == {(40, 15)}
    assert events[1].budget.listings_used == 5
    assert events[0].arguments == 'query="iphone 15" price_max=700 limit=5'
    assert events[0].duration_seconds == 0.0 and events[1].duration_seconds >= 0.0


def test_a_finished_search_event_carries_the_listings_carousell_returned() -> None:
    _, events = observed_run(calls(SEARCH_CALL), calls(DETAILS_CALL), final_answer())

    search_finished, details_finished = events[1], events[3]
    assert [listing.id for listing in search_finished.listings] == [listing.id for listing in SEARCH_RESULT.listings]
    assert search_finished.listings[0].price_display == FIRST_LISTING.price_display
    assert details_finished.listings == ()


def test_a_refused_call_has_only_a_finished_event_marked_refused() -> None:
    carousell = FakeCarousellSource(photo=jpeg_bytes(400, 300))
    first_photo = calls(("listing_photo", {"url": PHOTO_URL}))
    second_photo = calls(("listing_photo", {"url": SECOND_PHOTO_URL}))
    _, events = observed_run(
        first_photo, second_photo, final_answer(), carousell=carousell, budget=AgentBudget(max_photos=1)
    )

    assert [(event.phase, event.status) for event in events] == [
        ("started", None),
        ("finished", "ok"),
        ("finished", "refused"),
    ]
    refused = events[2]
    assert refused.arguments == f"url={SECOND_PHOTO_URL.rsplit('/', 1)[-1]}"
    assert (refused.duration_seconds, refused.budget.photos_used, refused.budget.tool_calls_used) == (0.0, 1, 1)


def test_a_failed_search_is_reported_as_failed_without_listings() -> None:
    carousell = FakeCarousellSource(search_result=CarousellBlockedError("rate limited by Carousell (HTTP 429)"))
    _, events = observed_run(calls(SEARCH_CALL), final_answer(), carousell=carousell)

    assert [(event.phase, event.status) for event in events] == [("started", None), ("finished", "failed")]
    assert events[1].listings == ()


def test_an_observer_that_raises_is_logged_and_does_not_change_the_answer(caplog: pytest.LogCaptureFixture) -> None:
    def broken_observer(event: ToolCallEvent) -> None:
        raise RuntimeError("display went away")

    turns = (calls(SEARCH_CALL), calls(DETAILS_CALL), final_answer("Prices checked.", [KEPT], [DROPPED]))
    carousell = FakeCarousellSource()
    with caplog.at_level(logging.ERROR, logger="carousearch.agent.run_prompt"):
        observed = ask(ScriptedModel(*turns), carousell, observer=broken_observer)
    unobserved = ask(ScriptedModel(*turns))

    assert observed.model_dump(exclude={"tool_trace"}) == unobserved.model_dump(exclude={"tool_trace"})
    assert [(call.tool_name, call.status) for call in observed.tool_trace] == [
        ("search_listings", "ok"),
        ("listing_details", "ok"),
    ]
    assert carousell.detail_requests == [DETAIL.id]  # the calls went ahead
    assert len(caplog.records) == 4  # one per event
    assert all("progress observer failed" in record.getMessage() for record in caplog.records)
