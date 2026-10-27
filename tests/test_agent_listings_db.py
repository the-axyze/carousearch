"""listings.db: rows from search pages and details, kept side by side, and readable by run_python."""

import json
import sqlite3
import statistics
from pathlib import Path

from carousearch.agent.listings_db import RunListingsStore, detail_row, search_listing_row, upsert_statement
from carousearch.agent.sandbox import SandboxLimits, run_python_in_sandbox
from carousearch.mcp_server import detail_response, search_response

from fake_mcp_services import DETAIL, SEARCH_RESULT

SEARCH_PAGE = search_response(SEARCH_RESULT, 20)
FIRST = SEARCH_PAGE["listings"][0]


def rows(directory: Path) -> dict[str, dict]:
    connection = sqlite3.connect(directory / "listings.db")
    connection.row_factory = sqlite3.Row
    return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM listings")}


def test_a_search_listing_becomes_a_row_with_the_searched_category() -> None:
    row = search_listing_row(FIRST, "5707")

    assert row["id"] == FIRST["id"] and row["title"] == FIRST["title"] and row["price"] == FIRST["price"]
    assert row["url"] == FIRST["listing_url"] and row["category"] == "5707"
    assert json.loads(row["raw_json"]) == FIRST
    assert (row["posted_at"], row["bumped_at"]) == (FIRST["posted_or_bumped_at"], None)  # a time_created card
    bumped = search_listing_row(SEARCH_PAGE["listings"][1], None)
    assert (bumped["posted_at"], bumped["bumped_at"]) == (None, SEARCH_PAGE["listings"][1]["posted_or_bumped_at"])


def test_a_detail_becomes_a_row_with_the_seller_and_description_columns() -> None:
    row = detail_row(detail_response(DETAIL))

    assert (row["has_details"], row["description"]) == (1, DETAIL.description)
    assert row["seller_rating"] == DETAIL.seller_rating and row["review_count"] == DETAIL.seller_review_count
    assert json.loads(row["image_urls"]) == DETAIL.image_urls
    assert [meetup["name"] for meetup in json.loads(row["meetup_locations"])] == [
        location.name for location in DETAIL.meetup_locations
    ]


def test_the_upsert_keeps_known_times_and_categories_when_new_values_are_unknown() -> None:
    statement = upsert_statement({"id": "1", "title": "t", "category": None}, ["title"])

    assert "title = excluded.title" in statement
    assert "category = COALESCE(excluded.category, listings.category)" in statement


def test_search_pages_and_details_fill_one_row_each_without_erasing_each_other(tmp_path: Path) -> None:
    store = RunListingsStore(tmp_path)
    store.record_tool_result("search_listings", {"request": {"query": "iphone", "category": "5707"}}, SEARCH_PAGE)
    detail = {**detail_response(DETAIL), "id": FIRST["id"]}
    store.record_tool_result("listing_details", {"listing_id": FIRST["id"]}, detail)
    store.record_tool_result("search_listings", {"request": {"query": "iphone"}}, SEARCH_PAGE)  # again, no category
    store.record_tool_result("listing_photo", {"url": "x"}, b"not a listing")
    store.close()

    table = rows(tmp_path)
    assert len(table) == len(SEARCH_PAGE["listings"])
    first = table[FIRST["id"]]
    assert first["has_details"] == 1 and first["description"] == DETAIL.description
    assert first["thumbnail_url"] == FIRST["thumbnail_url"]  # from the search, kept after the details
    assert (first["category"], first["category_name"]) == ("6359", "iPhone 15 Series")  # the details' own category
    others = [row for listing_id, row in table.items() if listing_id != FIRST["id"]]
    assert all(row["category"] == "5707" and row["has_details"] == 0 for row in others)


def test_listings_json_holds_the_same_rows_decoded(tmp_path: Path) -> None:
    store = RunListingsStore(tmp_path)
    store.record_detail(detail_response(DETAIL))
    store.close()

    (row,) = json.loads((tmp_path / "listings.json").read_text())
    assert row["id"] == DETAIL.id and row["image_urls"] == DETAIL.image_urls
    assert isinstance(row["detail_json"], dict)


def test_the_store_finds_known_rows_by_id(tmp_path: Path) -> None:
    store = RunListingsStore(tmp_path)
    store.record_search_page(SEARCH_PAGE["listings"], None)

    found = store.listings([FIRST["id"], "123"])

    assert list(found) == [FIRST["id"]] and found[FIRST["id"]]["title"] == FIRST["title"]
    assert store.listings([]) == {}


def test_run_python_can_query_the_listings(tmp_path: Path) -> None:
    store = RunListingsStore(tmp_path)
    store.record_search_page(SEARCH_PAGE["listings"], "5707")
    store.close()
    code = (
        "import sqlite3, statistics\n"
        "prices = [row[0] for row in sqlite3.connect('listings.db').execute("
        "'SELECT price FROM listings WHERE price IS NOT NULL')]\n"
        "print(len(prices), statistics.median(prices))\n"
    )

    result = run_python_in_sandbox(code, tmp_path, SandboxLimits(wall_seconds=10), network_prefix=())

    prices = [listing["price"] for listing in SEARCH_PAGE["listings"] if listing["price"] is not None]
    assert result.stdout.split() == [str(len(prices)), str(statistics.median(prices))]
