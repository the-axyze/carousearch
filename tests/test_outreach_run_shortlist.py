"""Reading a run's shortlist back from its run folder: answer.json and listings.db."""

import json
from pathlib import Path

import pytest

from carousearch.agent.listings_db import LISTINGS_DATABASE_FILE_NAME, RunListingsStore
from carousearch.outreach import run_shortlist
from carousearch.outreach.run_shortlist import (
    UnknownRunError,
    fallback_shortlist,
    listing_from_row,
    load_run_shortlist,
    shortlist_from_answer,
)

RUN_ID = "20261030-090000-abc123"
DETAIL = {
    "id": "111", "title": "Walnut side table", "price": 80.0, "seller_id": "9", "seller_username": "tan",
    "listing_url": "https://www.carousell.sg/p/111/", "description": "Solid top, one small dent.",
    "condition_label": "Lightly used", "attributes": {"colour": "brown"},
}
CARD = {"id": "222", "title": "Oak shelf", "price": 40.0, "seller_id": "8", "seller_username": "lim",
        "listing_url": "https://www.carousell.sg/p/222/", "description_snippet": "Sturdy, five tiers"}


def make_run(tmp_path: Path, kept: list[dict] | None) -> Path:
    directory = tmp_path / "runs" / RUN_ID
    store = RunListingsStore(directory)
    store.record_search_page([CARD], None)
    store.record_detail(DETAIL)
    store.record_extracted({"111": {"height_cm": 60}}, {"height_cm": ("x_height_cm", "REAL")})
    store.close()
    if kept is not None:
        (directory / "answer.json").write_text(json.dumps({"kept": kept}), encoding="utf-8")
    return directory


def test_the_database_file_name_matches_the_agents() -> None:
    assert run_shortlist.LISTINGS_DATABASE_FILE_NAME == LISTINGS_DATABASE_FILE_NAME


def test_the_shortlist_is_the_answers_kept_listings_with_the_runs_data(tmp_path: Path) -> None:
    make_run(tmp_path, kept=[
        {"listing_id": "111", "title": "t", "price": 80, "url": "u", "seller": "tan", "reason": "cheapest solid top"},
        {"listing_id": "333", "title": "Unseen", "price": 10, "url": "https://x/333", "seller": "ng", "reason": "r"},
    ])

    shortlist = load_run_shortlist(tmp_path, RUN_ID)

    assert shortlist.from_answer and [item.listing_id for item in shortlist.listings] == ["111", "333"]
    first, unseen = shortlist.listings
    assert (first.title, first.price, first.seller_id, first.seller_username) == ("Walnut side table", 80.0, "9", "tan")
    assert first.description == "Solid top, one small dent." and first.condition == "Lightly used"
    assert first.attributes == {"colour": "brown"} and first.extracted == {"height_cm": 60}
    assert first.kept_reason == "cheapest solid top"
    assert (unseen.title, unseen.seller_id, unseen.seller_username) == ("Unseen", None, "ng")


def test_a_run_without_an_answer_offers_every_listing_details_first(tmp_path: Path) -> None:
    make_run(tmp_path, kept=None)

    shortlist = load_run_shortlist(tmp_path, RUN_ID)

    assert not shortlist.from_answer
    assert [item.listing_id for item in shortlist.listings] == ["111", "222"]
    assert shortlist.listings[1].description == "Sturdy, five tiers"  # the card's snippet stands in


@pytest.mark.parametrize("run_id", ["../etc", "no-such-run", "a/b", ""])
def test_unknown_or_malformed_run_ids_are_refused(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(UnknownRunError):
        load_run_shortlist(tmp_path, run_id)


def test_rows_with_no_json_columns_still_convert() -> None:
    listing = listing_from_row({"id": 5, "title": None, "price": None})

    assert (listing.listing_id, listing.title, listing.attributes, listing.extracted) == ("5", "", {}, {})


def test_the_pure_helpers_keep_order() -> None:
    rows = [{"id": "1", "has_details": 0}, {"id": "2", "has_details": 1}, {"id": "3", "has_details": 0}]

    assert [item.listing_id for item in fallback_shortlist(rows)] == ["2", "1", "3"]
    kept = [{"listing_id": "3", "reason": "a"}, {"listing_id": "1", "reason": "b"}]
    assert [item.listing_id for item in shortlist_from_answer(kept, {row["id"]: row for row in rows})] == ["3", "1"]
