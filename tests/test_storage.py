from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carousearch import storage
from carousearch.agent.judge_cache import SqliteJudgementCache
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.filters.schema import Condition, SearchRequest
from carousearch.screening.models import PriceSource, ResolvedPrice

from fixture_files import load_json_fixture

NOON = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path):
    database = storage.open_database(tmp_path / "nested" / "carousearch.db")
    yield database
    database.close()


def sample_listings():
    return parse_search_response(load_json_fixture("search_price_condition.json")).listings


def test_open_database_creates_the_file_and_folders(tmp_path: Path) -> None:
    database_path = tmp_path / "a" / "b" / "carousearch.db"

    storage.open_database(database_path).close()

    assert database_path.exists()


def test_record_seen_listings_reports_only_new_ids(connection) -> None:
    listings = sample_listings()

    first_new_ids = storage.record_seen_listings(connection, listings[:2], NOON)
    second_new_ids = storage.record_seen_listings(connection, listings, NOON + timedelta(hours=1))

    assert first_new_ids == [listing.id for listing in listings[:2]]
    assert second_new_ids == [listing.id for listing in listings[2:]]


def test_seen_listing_keeps_first_seen_and_updates_last_seen(connection) -> None:
    listing = sample_listings()[0]
    storage.record_seen_listings(connection, [listing], NOON)
    storage.record_seen_listings(connection, [listing.model_copy(update={"price": 1.0})], NOON + timedelta(days=1))

    seen = storage.get_seen_listing(connection, listing.id)

    assert seen.first_seen_at == NOON
    assert seen.last_seen_at == NOON + timedelta(days=1)
    assert seen.title == listing.title
    assert seen.price == 1.0
    assert storage.get_seen_listing(connection, "does-not-exist") is None


def test_listing_detail_cache_round_trip_and_expiry(connection) -> None:
    detail = parse_listing_detail_response(load_json_fixture("listing_detail.json"))
    storage.cache_listing_detail(connection, detail, NOON)

    fresh = storage.load_cached_listing_detail(connection, detail.id, NOON + timedelta(hours=1), timedelta(hours=12))
    stale = storage.load_cached_listing_detail(connection, detail.id, NOON + timedelta(hours=13), timedelta(hours=12))

    assert fresh == detail
    assert stale is None
    assert storage.load_cached_listing_detail(connection, "1", NOON, timedelta(hours=12)) is None


def test_saved_searches(connection) -> None:
    request = SearchRequest(query="aeron", price_max=600, conditions=[Condition.LIGHTLY_USED])

    saved_id = storage.save_search(connection, "chair", request, NOON)
    saved = storage.list_saved_searches(connection)

    assert [(search.id, search.name, search.request, search.created_at) for search in saved] == [
        (saved_id, "chair", request, NOON)
    ]
    assert storage.delete_saved_search(connection, saved_id) is True
    assert storage.delete_saved_search(connection, saved_id) is False
    assert storage.list_saved_searches(connection) == []


def test_naive_timestamps_are_rejected(connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        storage.record_seen_listings(connection, sample_listings()[:1], datetime(2026, 10, 24, 12, 0))


def test_price_resolution_cache_is_keyed_by_listing_and_query(connection) -> None:
    from_description = ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="15 Pro 256GB - $850")
    not_stated = ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only 128GB is priced")

    storage.save_price_resolution(connection, "1469923094", "iphone 15 pro 256gb", from_description, NOON)
    storage.save_price_resolution(connection, "1469923094", "iphone 15 pro max", not_stated, NOON)

    assert storage.load_price_resolution(connection, "1469923094", "iphone 15 pro 256gb") == from_description
    assert storage.load_price_resolution(connection, "1469923094", "iphone 15 pro max") == not_stated
    assert storage.load_price_resolution(connection, "1469923094", "iphone 15") is None
    assert storage.load_price_resolution(connection, "1", "iphone 15 pro 256gb") is None


def test_price_resolution_cache_replaces_an_entry(connection) -> None:
    first = ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION)
    second = ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="price removed")

    storage.save_price_resolution(connection, "1469923094", "iphone 15 pro", first, NOON)
    storage.save_price_resolution(connection, "1469923094", "iphone 15 pro", second, NOON + timedelta(hours=1))

    assert storage.load_price_resolution(connection, "1469923094", "iphone 15 pro") == second
    assert connection.execute("SELECT COUNT(*) FROM price_resolution_cache").fetchone() == (1,)


def test_judgement_cache_is_keyed_by_kind_listing_criterion_and_model(connection) -> None:
    cache = SqliteJudgementCache(connection)
    verdict = {"verdict": "yes", "reason": "COE 2028"}
    cache.save("listing", "1", "coe about 2 years", "openai:gpt-5.4-nano", verdict, NOON)

    assert cache.load("listing", "1", "coe about 2 years", "openai:gpt-5.4-nano") == verdict
    assert cache.load("photo", "1", "coe about 2 years", "openai:gpt-5.4-nano") is None
    assert cache.load("listing", "1", "coe about 2 years", "openai:gpt-5.4-mini") is None
    assert cache.load("listing", "1", "blurry photo", "openai:gpt-5.4-nano") is None


def test_judgement_cache_replaces_an_entry(connection) -> None:
    storage.save_judgement(connection, "photo", "1", "blurry", "m", '{"score_1_to_5": 2}', NOON)
    storage.save_judgement(connection, "photo", "1", "blurry", "m", '{"score_1_to_5": 5}', NOON)

    assert storage.load_judgement(connection, "photo", "1", "blurry", "m") == '{"score_1_to_5": 5}'
