from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from carousearch import storage
from carousearch.carousell.parse import parse_search_response
from carousearch.filters.schema import Condition, PostedOrBumpedKind, SearchRequest
from carousearch.market.hashing import listing_content_hash, search_request_hash
from carousearch.market.limits import HarvestLimits

from fixture_files import load_json_fixture

NOON = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
LATER = NOON + timedelta(hours=3)


@pytest.fixture
def connection(tmp_path: Path):
    database = storage.open_database(tmp_path / "carousearch.db")
    yield database
    database.close()


def sample_listings():
    return parse_search_response(load_json_fixture("search_price_condition.json")).listings


def listing_change_count(connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM listing_changes").fetchone()[0]


# --- schema and timestamps ---------------------------------------------------


def test_open_database_creates_the_market_tables_and_indexes(connection) -> None:
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}

    assert {"listings", "listing_changes", "harvest_runs", "harvest_run_listings"} <= names
    assert {"listing_changes_listing", "harvest_runs_lookup"} <= names


def test_open_database_twice_keeps_existing_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "carousearch.db"
    first = storage.open_database(database_path)
    storage.upsert_listings(first, sample_listings()[:1], NOON)
    first.close()

    second = storage.open_database(database_path)

    assert len(storage.load_listing_rows(second, [sample_listings()[0].id])) == 1
    second.close()


def test_public_timestamp_helpers_and_old_aliases() -> None:
    singapore = datetime(2026, 10, 24, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    assert storage.from_text(storage.to_text(singapore)) == singapore
    assert storage.from_text(storage.to_text(singapore)).tzinfo == UTC
    assert storage.to_text(NOON) == "2026-10-24T12:00:00+00:00"
    assert storage._to_text is storage.to_text
    assert storage._from_text is storage.from_text
    with pytest.raises(ValueError, match="timezone-aware"):
        storage.to_text(datetime(2026, 10, 24, 12, 0))


# --- upsert_listings -----------------------------------------------------------


def test_insert_stores_every_column(connection) -> None:
    listings = sample_listings()

    summary = storage.upsert_listings(connection, listings, NOON)
    rows = storage.load_listing_rows(connection, [listing.id for listing in listings])

    assert summary == storage.UpsertSummary(
        new_ids=[listing.id for listing in listings], changed_ids=[], unchanged_ids=[]
    )
    assert [row.listing_id for row in rows] == [listing.id for listing in listings]
    for listing, row in zip(listings, rows, strict=True):
        assert row == storage.ListingRow(
            listing_id=listing.id,
            title=listing.title,
            price=listing.price,
            currency=listing.currency,
            price_display=listing.price_display,
            condition=listing.condition,
            condition_label=listing.condition_label,
            like_count=listing.like_count,
            seller_username=listing.seller_username,
            seller_id=listing.seller_id,
            thumbnail_url=listing.thumbnail_url,
            listing_url=listing.listing_url,
            posted_or_bumped_at=listing.posted_or_bumped_at,
            posted_or_bumped_kind=listing.posted_or_bumped_kind,
            content_hash=listing_content_hash(listing),
            first_seen_at=NOON,
            last_seen_at=NOON,
            last_changed_at=NOON,
        )
        assert isinstance(row.condition, Condition)
        assert isinstance(row.posted_or_bumped_kind, PostedOrBumpedKind)
    assert listing_change_count(connection) == 0


def test_enums_are_stored_as_their_semantic_values(connection) -> None:
    listing = sample_listings()[0].model_copy(
        update={"condition": Condition.LIKE_NEW, "posted_or_bumped_kind": PostedOrBumpedKind.BUMPED}
    )

    storage.upsert_listings(connection, [listing], NOON)
    stored = connection.execute(
        "SELECT condition, posted_or_bumped_kind FROM listings WHERE listing_id = ?", (listing.id,)
    ).fetchone()

    assert stored == ("like_new", "active_bump")


def test_insert_with_missing_optional_fields(connection) -> None:
    listing = sample_listings()[0].model_copy(
        update={
            "price": None,
            "price_display": "Free",
            "condition": None,
            "condition_label": "Used",
            "posted_or_bumped_at": None,
            "posted_or_bumped_kind": None,
            "seller_username": None,
            "seller_id": None,
            "thumbnail_url": None,
            "currency": None,
        }
    )

    storage.upsert_listings(connection, [listing], NOON)
    [row] = storage.load_listing_rows(connection, [listing.id])

    assert row.price is None
    assert row.condition is None
    assert row.condition_label == "Used"
    assert row.posted_or_bumped_at is None
    assert row.posted_or_bumped_kind is None
    assert row.seller_username is None and row.seller_id is None and row.thumbnail_url is None


def test_unchanged_listing_moves_only_likes_bump_time_and_last_seen(connection) -> None:
    listing = sample_listings()[0]
    storage.upsert_listings(connection, [listing], NOON)
    [before] = storage.load_listing_rows(connection, [listing.id])
    # Likes, the bump time and the thumbnail are outside the content hash. Likes and the bump time are
    # refreshed anyway (demand and freshness move without the listing "changing"); the thumbnail is not rewritten.
    bumped_at = LATER - timedelta(hours=1)
    seen_again = listing.model_copy(
        update={
            "like_count": listing.like_count + 5,
            "thumbnail_url": "https://example.invalid/new.jpg",
            "posted_or_bumped_at": bumped_at,
            "posted_or_bumped_kind": PostedOrBumpedKind.BUMPED,
        }
    )

    summary = storage.upsert_listings(connection, [seen_again], LATER)
    [after] = storage.load_listing_rows(connection, [listing.id])

    assert summary == storage.UpsertSummary(new_ids=[], changed_ids=[], unchanged_ids=[listing.id])
    assert after.like_count == listing.like_count + 5
    assert after.last_seen_at == LATER
    assert after.first_seen_at == NOON
    assert after.last_changed_at == NOON
    assert after.thumbnail_url == before.thumbnail_url
    assert after == storage.ListingRow(
        **{
            **before.__dict__,
            "like_count": listing.like_count + 5,
            "last_seen_at": LATER,
            "posted_or_bumped_at": bumped_at,
            "posted_or_bumped_kind": PostedOrBumpedKind.BUMPED,
        }
    )
    assert storage.load_listing_changes(connection, listing.id) == []


def test_changed_listing_overwrites_row_and_logs_the_change(connection) -> None:
    listing = sample_listings()[0]
    storage.upsert_listings(connection, [listing], NOON)
    repriced = listing.model_copy(
        update={
            "price": 499.0,
            "price_display": "S$499",
            "title": listing.title + " price drop",
            "condition": Condition.WELL_USED,
            "condition_label": "Well used",
            "like_count": listing.like_count + 2,
            "thumbnail_url": "https://example.invalid/new.jpg",
        }
    )

    summary = storage.upsert_listings(connection, [repriced], LATER)
    [row] = storage.load_listing_rows(connection, [listing.id])
    changes = storage.load_listing_changes(connection, listing.id)

    assert summary == storage.UpsertSummary(new_ids=[], changed_ids=[listing.id], unchanged_ids=[])
    assert row.price == 499.0
    assert row.price_display == "S$499"
    assert row.title == repriced.title
    assert row.condition == Condition.WELL_USED
    assert row.condition_label == "Well used"
    assert row.like_count == listing.like_count + 2
    assert row.thumbnail_url == "https://example.invalid/new.jpg"
    assert row.content_hash == listing_content_hash(repriced)
    assert row.first_seen_at == NOON
    assert row.last_seen_at == LATER
    assert row.last_changed_at == LATER
    assert len(changes) == 1
    change = changes[0]
    assert (change.listing_id, change.changed_at) == (listing.id, LATER)
    assert (change.old_price, change.new_price) == (listing.price, 499.0)
    assert (change.old_condition, change.new_condition) == (listing.condition, Condition.WELL_USED)
    assert (change.old_title, change.new_title) == (listing.title, repriced.title)


def test_listing_changes_are_returned_oldest_first(connection) -> None:
    listing = sample_listings()[0]
    storage.upsert_listings(connection, [listing], NOON)
    storage.upsert_listings(connection, [listing.model_copy(update={"price": 500.0})], NOON + timedelta(hours=1))
    storage.upsert_listings(connection, [listing.model_copy(update={"price": 450.0})], NOON + timedelta(hours=2))
    # Same content again: no third change.
    storage.upsert_listings(connection, [listing.model_copy(update={"price": 450})], NOON + timedelta(hours=3))

    changes = storage.load_listing_changes(connection, listing.id)

    assert [(change.old_price, change.new_price) for change in changes] == [(listing.price, 500.0), (500.0, 450.0)]
    assert [change.changed_at for change in changes] == [NOON + timedelta(hours=1), NOON + timedelta(hours=2)]
    assert storage.load_listing_changes(connection, "does-not-exist") == []


def test_change_to_and_from_unpriced_and_legacy_condition(connection) -> None:
    listing = sample_listings()[0]
    storage.upsert_listings(connection, [listing], NOON)

    storage.upsert_listings(connection, [listing.model_copy(update={"price": None, "condition": None})], LATER)
    [change] = storage.load_listing_changes(connection, listing.id)

    assert change.new_price is None
    assert change.new_condition is None
    assert change.old_condition == listing.condition


def test_duplicate_ids_within_one_call_keep_the_first_occurrence(connection) -> None:
    first, second = sample_listings()[:2]
    duplicate = first.model_copy(update={"price": 1.0, "like_count": 999})

    summary = storage.upsert_listings(connection, [first, second, duplicate], NOON)
    [row] = storage.load_listing_rows(connection, [first.id])

    assert summary == storage.UpsertSummary(new_ids=[first.id, second.id], changed_ids=[], unchanged_ids=[])
    assert row.price == first.price
    assert row.like_count == first.like_count
    assert listing_change_count(connection) == 0


def test_mixed_new_changed_and_unchanged_in_one_call(connection) -> None:
    listings = sample_listings()
    storage.upsert_listings(connection, listings[:3], NOON)
    pull = [listings[0], listings[1].model_copy(update={"price": 1.0}), listings[3]]

    summary = storage.upsert_listings(connection, pull, LATER)

    assert summary == storage.UpsertSummary(
        new_ids=[listings[3].id], changed_ids=[listings[1].id], unchanged_ids=[listings[0].id]
    )


def test_upsert_rejects_naive_timestamps_and_writes_nothing(connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        storage.upsert_listings(connection, sample_listings()[:1], datetime(2026, 10, 24, 12, 0))

    assert connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_load_listing_rows_keeps_the_given_order_and_skips_missing_ids(connection) -> None:
    listings = sample_listings()
    storage.upsert_listings(connection, listings, NOON)
    wanted = [listings[2].id, "missing", listings[0].id, listings[1].id]

    rows = storage.load_listing_rows(connection, wanted)

    assert [row.listing_id for row in rows] == [listings[2].id, listings[0].id, listings[1].id]
    assert storage.load_listing_rows(connection, []) == []


def test_load_listing_rows_handles_more_ids_than_one_query_allows(connection) -> None:
    template = sample_listings()[0]
    listings = [template.model_copy(update={"id": str(10_000 + index)}) for index in range(1200)]
    storage.upsert_listings(connection, listings, NOON)

    rows = storage.load_listing_rows(connection, [listing.id for listing in reversed(listings)])

    assert [row.listing_id for row in rows] == [listing.id for listing in reversed(listings)]


# --- harvest_runs ----------------------------------------------------------------


def sample_request() -> SearchRequest:
    return SearchRequest(query="iphone 15 pro", price_max=1500, conditions=[Condition.LIKE_NEW])


def finished_run(connection, request: SearchRequest, started_at: datetime, stop_reason: str = "max_pages") -> int:
    run_id = storage.create_harvest_run(connection, request, search_request_hash(request), started_at)
    storage.finish_harvest_run(connection, run_id, started_at + timedelta(minutes=1), 2, 40, stop_reason)
    return run_id


def test_create_and_finish_harvest_run(connection) -> None:
    request = sample_request().model_copy(update={"page_cursor": "cursor-from-page-2"})
    request_hash = search_request_hash(request)

    run_id = storage.create_harvest_run(connection, request, request_hash, NOON)
    in_progress = connection.execute(
        "SELECT finished_at, pages_fetched, listing_count, stop_reason, request_json FROM harvest_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    storage.finish_harvest_run(connection, run_id, NOON + timedelta(minutes=2), 3, 120, "no_more_pages")
    run = storage.find_reusable_run(connection, request_hash, NOON + timedelta(minutes=5), timedelta(minutes=30))

    assert in_progress[:4] == (None, 0, 0, None)
    assert SearchRequest.model_validate_json(in_progress[4]).page_cursor is None
    assert run == storage.HarvestRun(
        id=run_id,
        request_hash=request_hash,
        request=sample_request(),
        started_at=NOON,
        finished_at=NOON + timedelta(minutes=2),
        pages_fetched=3,
        listing_count=120,
        stop_reason="no_more_pages",
        limits=HarvestLimits(),
    )


def test_find_reusable_run_respects_max_age(connection) -> None:
    request = sample_request()
    request_hash = search_request_hash(request)
    run_id = finished_run(connection, request, NOON)  # finished at NOON + 1 minute
    max_age = timedelta(minutes=30)

    at_limit = storage.find_reusable_run(connection, request_hash, NOON + timedelta(minutes=31), max_age)
    too_old = storage.find_reusable_run(connection, request_hash, NOON + timedelta(minutes=31, seconds=1), max_age)

    assert at_limit is not None and at_limit.id == run_id
    assert too_old is None


def test_find_reusable_run_ignores_unfinished_and_error_runs(connection) -> None:
    request = sample_request()
    request_hash = search_request_hash(request)
    storage.create_harvest_run(connection, request, request_hash, NOON)  # still running
    finished_run(connection, request, NOON, stop_reason="error")

    assert storage.find_reusable_run(connection, request_hash, NOON + timedelta(minutes=5), timedelta(hours=1)) is None


def test_find_reusable_run_returns_the_newest_matching_run(connection) -> None:
    request = sample_request()
    request_hash = search_request_hash(request)
    finished_run(connection, request, NOON)
    newest_good = finished_run(connection, request, NOON + timedelta(minutes=10))
    finished_run(connection, request, NOON + timedelta(minutes=20), stop_reason="error")
    other_request = request.model_copy(update={"query": "iphone 14"})
    finished_run(connection, other_request, NOON + timedelta(minutes=25))

    run = storage.find_reusable_run(connection, request_hash, NOON + timedelta(minutes=30), timedelta(minutes=30))

    assert run.id == newest_good
    assert storage.find_reusable_run(connection, "0" * 16, NOON, timedelta(hours=1)) is None


def test_run_listings_keep_first_position(connection) -> None:
    run_id = storage.create_harvest_run(connection, sample_request(), search_request_hash(sample_request()), NOON)
    other_run_id = storage.create_harvest_run(connection, sample_request(), search_request_hash(sample_request()), NOON)

    storage.add_run_listings(connection, run_id, ["a", "b", "c"], first_position=0)
    storage.add_run_listings(connection, run_id, ["d", "b", "e"], first_position=3)
    storage.add_run_listings(connection, other_run_id, ["e", "a"], first_position=0)
    positions = dict(
        connection.execute(
            "SELECT listing_id, position FROM harvest_run_listings WHERE run_id = ?", (run_id,)
        ).fetchall()
    )

    assert storage.load_run_listing_ids(connection, run_id) == ["a", "b", "c", "d", "e"]
    assert positions == {"a": 0, "b": 1, "c": 2, "d": 3, "e": 5}
    assert storage.load_run_listing_ids(connection, other_run_id) == ["e", "a"]
    assert storage.load_run_listing_ids(connection, 999) == []


def test_harvest_run_stores_its_limits(connection) -> None:
    limits = HarvestLimits(max_pages=3, max_age_days=7, page_size=20)
    run_id = storage.create_harvest_run(connection, sample_request(), "hash-l", NOON, limits)
    storage.finish_harvest_run(connection, run_id, NOON + timedelta(minutes=1), 3, 60, "max_pages")

    [run] = storage.find_recent_runs(connection, "hash-l", NOON + timedelta(minutes=2), timedelta(minutes=30))
    assert run.limits == limits
    assert storage.find_reusable_run(connection, "hash-l", NOON + timedelta(minutes=2), timedelta(minutes=30)) == run
