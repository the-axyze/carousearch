"""Progress events (pure): the budget snapshot, the events, and the listings rebuilt from a search result."""

import logging

import pytest

from carousearch.agent.budget import ToolBudget
from carousearch.agent.progress import (
    BudgetUsed,
    budget_used,
    finished_event,
    listings_from_search_result,
    started_event,
)
from carousearch.agent.settings import AgentBudget
from carousearch.mcp_server import search_response

from fake_mcp_services import SEARCH_RESULT

SEARCH = {"request": {"query": "iphone 15", "price_max": 700}, "limit": 5}
LISTING_IDS = [listing.id for listing in SEARCH_RESULT.listings]


def search_tool_result() -> dict:
    """What the tool hook receives for a search: the tool's structured content, as search_response builds it."""
    return search_response(SEARCH_RESULT, limit=48)


def test_the_listings_of_a_search_result_are_rebuilt_from_its_json() -> None:
    listings = listings_from_search_result(search_tool_result())

    assert [listing.id for listing in listings] == LISTING_IDS
    assert listings[0].price_display == SEARCH_RESULT.listings[0].price_display
    assert listings[0].thumbnail_url == SEARCH_RESULT.listings[0].thumbnail_url
    assert listings[0].raw == {}  # search_response leaves Carousell's raw objects out


@pytest.mark.parametrize(
    "result",
    [None, "Refused: budget used up", [], {}, {"listings": "none"}, {"listing_id": "1", "tags": []}, {"listings": []}],
)
def test_a_result_that_is_not_a_search_response_gives_no_listings(result: object) -> None:
    assert listings_from_search_result(result) == []


def test_a_listing_that_does_not_validate_is_skipped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    result = search_tool_result()
    result["listings"][1] = {"id": "999", "title": "no price or url"}
    result["listings"].append("not a listing")

    with caplog.at_level(logging.WARNING, logger="carousearch.agent.progress"):
        listings = listings_from_search_result(result)

    assert [listing.id for listing in listings] == [LISTING_IDS[0], *LISTING_IDS[2:]]
    assert len(caplog.records) == 2
    assert "id 999" in caplog.records[0].getMessage()


def test_budget_used_reads_the_counters_and_the_caps() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=10, max_photos=3, max_listings=100))
    budget.admit("search_listings", SEARCH)
    budget.admit("listing_photo", {"url": "https://media.karousell.com/a.jpg"})

    assert budget_used(budget) == BudgetUsed(
        tool_calls_used=2, max_tool_calls=10, photos_used=1, max_photos=3, listings_used=5, max_listings=100
    )


def test_a_started_event_has_no_status_duration_or_listings() -> None:
    event = started_event("search_listings", SEARCH, ToolBudget(AgentBudget()))

    assert (event.phase, event.tool_name, event.arguments) == ("started", "search_listings", 'query="iphone 15" price_max=700 limit=5')
    assert (event.status, event.duration_seconds, event.listings) == (None, 0.0, ())


def test_a_finished_search_carries_the_listings_it_returned() -> None:
    event = finished_event("search_listings", SEARCH, "ok", 1.5, ToolBudget(AgentBudget()), search_tool_result())

    assert (event.phase, event.status, event.duration_seconds) == ("finished", "ok", 1.5)
    assert [listing.id for listing in event.listings] == LISTING_IDS


def test_only_an_ok_search_carries_listings() -> None:
    budget = ToolBudget(AgentBudget())

    assert finished_event("search_listings", SEARCH, "failed", 0.2, budget).listings == ()
    assert finished_event("listing_details", {"listing_id": "1"}, "ok", 0.2, budget, search_tool_result()).listings == ()
