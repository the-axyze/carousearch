"""Harvest tests: scripted multi-page searches through FakeCarousell, a temporary SQLite database, no network."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from carousearch import storage
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.carousell.parse import parse_search_response
from carousearch.filters.schema import Listing, SearchRequest, SortOrder
from carousearch.market.harvest import HarvestOutcome, StopReason, decide_stop, harvest
from carousearch.market.limits import HarvestLimits

from fake_carousell import FakeCarousell
from fixture_files import listing_cards, load_json_fixture

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
REQUEST = SearchRequest(query="iphone 15", sort=SortOrder.RECENT)


def fixed_clock(moment: datetime = NOW):
    return lambda: moment


def search_page(
    listing_ids: list[str],
    next_page: str | None,
    posted_at: list[datetime] | None = None,
    prices: list[str] | None = None,
) -> httpx.Response:
    """The saved search fixture with its first cards rewritten: ids, timestamps, prices and the paging fields."""
    response_json = copy.deepcopy(load_json_fixture("search_price_condition.json"))
    cards = listing_cards(response_json)
    assert len(listing_ids) <= len(cards), "the fixture has fewer cards than requested"
    for index, card in enumerate(cards):
        if index >= len(listing_ids):
            card["id"] = f"999{index}"  # keep the page size; these become fixed extra ids
            continue
        card["id"] = listing_ids[index]
        card["title"] = f"Listing {listing_ids[index]}"
        if prices is not None:
            card["price"] = prices[index]
        if posted_at is not None:
            for component in card["aboveFold"]:
                if "timestampContent" in component:
                    component["timestampContent"]["seconds"] = str(int(posted_at[index].timestamp()))
    # Any card beyond the requested ids is dropped so a page holds exactly the ids asked for.
    response_json["data"]["results"] = [
        item for item in response_json["data"]["results"] if "listingCard" not in item or item["listingCard"]["id"] in listing_ids
    ]
    response_json["data"]["hasMore"] = next_page is not None
    if next_page is not None:
        response_json["data"]["session"] = next_page
    return httpx.Response(200, json=response_json)


def page_listings(response: httpx.Response) -> list[Listing]:
    return parse_search_response(response.json()).listings


@pytest.fixture
def connection(tmp_path: Path):
    database = storage.open_database(tmp_path / "test.db")
    yield database
    database.close()


# --- decide_stop (pure) -------------------------------------------------------------


def test_stops_when_there_is_no_next_page():
    listings = page_listings(search_page(["1", "2"], None))
    assert decide_stop(1, listings, 2, None, HarvestLimits(max_pages=5), NOW) == StopReason.NO_MORE_PAGES


def test_stops_when_a_page_adds_nothing_new():
    listings = page_listings(search_page(["1", "2"], "cursor-2"))
    assert decide_stop(1, listings, 0, "cursor-2", HarvestLimits(max_pages=5), NOW) == StopReason.NO_MORE_PAGES


def test_stops_at_the_page_cap():
    listings = page_listings(search_page(["1", "2"], "cursor-2"))
    assert decide_stop(3, listings, 2, "cursor-2", HarvestLimits(max_pages=3), NOW) == StopReason.MAX_PAGES
    assert decide_stop(2, listings, 2, "cursor-2", HarvestLimits(max_pages=3), NOW) is None


def test_stops_when_the_last_dated_listing_is_too_old():
    old = NOW - timedelta(days=10)
    fresh = NOW - timedelta(days=1)
    listings = page_listings(search_page(["1", "2"], "cursor-2", posted_at=[fresh, old]))
    limits = HarvestLimits(max_pages=5, max_age_days=7)
    assert decide_stop(1, listings, 2, "cursor-2", limits, NOW) == StopReason.MAX_AGE
    still_fresh = page_listings(search_page(["1", "2"], "cursor-2", posted_at=[fresh, fresh]))
    assert decide_stop(1, still_fresh, 2, "cursor-2", limits, NOW) is None


def test_age_cutoff_ignores_listings_without_a_timestamp():
    listings = [listing.model_copy(update={"posted_or_bumped_at": None}) for listing in page_listings(search_page(["1"], "c"))]
    assert decide_stop(1, listings, 1, "c", HarvestLimits(max_pages=5, max_age_days=1), NOW) is None


# --- harvest (I/O against the fake) -------------------------------------------------------


def test_walks_pages_until_the_last_one_and_records_the_run(connection):
    fake = FakeCarousell(
        page_responses={None: search_page(["1", "2"], "cursor-2"), "cursor-2": search_page(["3", "4"], None)}
    )
    outcome = harvest(fake.client(), connection, REQUEST, HarvestLimits(max_pages=5), fixed_clock())

    assert outcome.listing_ids == ["1", "2", "3", "4"]
    assert outcome.pages_fetched == 2
    assert outcome.stop_reason == StopReason.NO_MORE_PAGES
    assert outcome.reused is False
    assert outcome.summary is not None and outcome.summary.new_ids == ["1", "2", "3", "4"]
    assert len(fake.api_requests) == 2
    assert storage.load_run_listing_ids(connection, outcome.run_id) == ["1", "2", "3", "4"]
    assert [row.listing_id for row in storage.load_listing_rows(connection, ["1", "2", "3", "4"])] == ["1", "2", "3", "4"]


def test_de_duplicates_a_listing_that_appears_on_two_pages(connection):
    fake = FakeCarousell(
        page_responses={None: search_page(["1", "2"], "cursor-2"), "cursor-2": search_page(["2", "3"], None)}
    )
    outcome = harvest(fake.client(), connection, REQUEST, HarvestLimits(max_pages=5), fixed_clock())
    assert outcome.listing_ids == ["1", "2", "3"]
    assert storage.load_run_listing_ids(connection, outcome.run_id) == ["1", "2", "3"]


def test_stops_at_max_pages_and_sends_the_page_size_from_the_limits(connection):
    fake = FakeCarousell(
        page_responses={None: search_page(["1"], "cursor-2"), "cursor-2": search_page(["2"], "cursor-3")}
    )
    outcome = harvest(fake.client(), connection, REQUEST, HarvestLimits(max_pages=2, page_size=5), fixed_clock())
    assert outcome.stop_reason == StopReason.MAX_PAGES
    assert outcome.pages_fetched == 2
    assert [json.loads(request.content)["count"] for request in fake.api_requests] == [5, 5]


def test_reuses_a_recent_successful_run(connection):
    fake = FakeCarousell(page_responses={None: search_page(["1", "2"], None)})
    first = harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock())
    later = fixed_clock(NOW + timedelta(minutes=10))
    second = harvest(fake.client(), connection, REQUEST, HarvestLimits(), later)

    assert second.reused is True
    assert second.run_id == first.run_id
    assert second.listing_ids == ["1", "2"]
    assert second.summary is None
    assert len(fake.api_requests) == 1


def test_does_not_reuse_an_old_run_or_when_reuse_is_off(connection):
    fake = FakeCarousell(page_responses={None: search_page(["1"], None)})
    harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock())
    much_later = fixed_clock(NOW + timedelta(hours=2))
    assert harvest(fake.client(), connection, REQUEST, HarvestLimits(), much_later).reused is False
    assert harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock(), reuse_within=None).reused is False
    assert len(fake.api_requests) == 3


def test_a_different_request_is_a_different_run(connection):
    fake = FakeCarousell(page_responses={None: search_page(["1"], None)})
    harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock())
    other = harvest(fake.client(), connection, REQUEST.model_copy(update={"price_max": 900.0}), HarvestLimits(), fixed_clock())
    assert other.reused is False
    assert len(fake.api_requests) == 2


def test_a_failed_run_keeps_its_listings_but_is_never_reused(connection):
    blocked = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<title>Just a moment</title>")
    fake = FakeCarousell(page_responses={None: search_page(["1", "2"], "cursor-2"), "cursor-2": blocked})
    with pytest.raises(CarousellBlockedError):
        harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock())

    assert [row.listing_id for row in storage.load_listing_rows(connection, ["1", "2"])] == ["1", "2"]
    runs = connection.execute("SELECT stop_reason, finished_at, pages_fetched, listing_count FROM harvest_runs").fetchall()
    assert runs == [("error", NOW.isoformat(), 1, 2)]
    fresh = FakeCarousell(page_responses={None: search_page(["1", "2"], None)})
    assert harvest(fresh.client(), connection, REQUEST, HarvestLimits(), fixed_clock()).reused is False


def test_second_harvest_records_a_price_change(connection):
    first = FakeCarousell(page_responses={None: search_page(["1"], None, prices=["S$800"])})
    harvest(first.client(), connection, REQUEST, HarvestLimits(), fixed_clock())
    second = FakeCarousell(page_responses={None: search_page(["1"], None, prices=["S$750"])})
    outcome = harvest(second.client(), connection, REQUEST, HarvestLimits(), fixed_clock(NOW + timedelta(hours=1)), reuse_within=None)

    assert outcome.summary is not None and outcome.summary.changed_ids == ["1"]
    changes = storage.load_listing_changes(connection, "1")
    assert [(change.old_price, change.new_price) for change in changes] == [(800.0, 750.0)]


def test_max_age_days_requires_sort_recent(connection):
    fake = FakeCarousell()
    with pytest.raises(ValueError, match="sort=recent"):
        harvest(fake.client(), connection, SearchRequest(query="x"), HarvestLimits(max_age_days=3), fixed_clock())
    assert fake.api_requests == []


def test_reports_progress_per_page(connection):
    fake = FakeCarousell(
        page_responses={None: search_page(["1", "2"], "cursor-2"), "cursor-2": search_page(["3"], None)}
    )
    reported: list[tuple[int, int]] = []
    outcome = harvest(fake.client(), connection, REQUEST, HarvestLimits(), fixed_clock(), progress=lambda p, n: reported.append((p, n)))
    assert isinstance(outcome, HarvestOutcome)
    assert reported == [(1, 2), (2, 3)]
