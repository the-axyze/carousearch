"""A run's shortlist, read back from its run directory (<data dir>/runs/<run id>/): the listings the answer kept
(answer.json), with what listings.db holds about each (title, price, seller, description, seller-entered fields,
extracted values).

A run made before answer.json existed has no record of what it kept; its shortlist is then every listing it saw,
those it fetched details for first, and `from_answer` is False so the caller can say so.

Pure: listing_from_row, shortlist_from_answer, fallback_shortlist. I/O: load_run_shortlist reads the two files.
"""

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carousearch.agent.run_directory import ANSWER_FILE_NAME, RUNS_DIRECTORY_NAME
from carousearch.outreach.models import ShortlistedListing

LISTINGS_DATABASE_FILE_NAME = "listings.db"  # agent/listings_db.py's file; that module pulls in the MCP SDK
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JSON_COLUMNS = ("attributes", "extracted")


class UnknownRunError(ValueError):
    """No run directory with that name, or a name that cannot be one."""


@dataclass(frozen=True)
class RunShortlist:
    run_id: str
    listings: list[ShortlistedListing]
    from_answer: bool  # False: the run has no answer.json and `listings` is every listing it saw


def listing_from_row(row: Mapping[str, Any], kept_reason: str | None = None) -> ShortlistedListing:
    """A listings.db row (JSON columns still as text) as a ShortlistedListing."""
    return ShortlistedListing(
        listing_id=str(row["id"]),
        title=row.get("title") or "",
        price=row.get("price"),
        url=row.get("url"),
        seller_id=row.get("seller_id"),
        seller_username=row.get("seller_username"),
        condition=row.get("condition_label") or row.get("condition"),
        description=row.get("description") or row.get("snippet"),
        attributes=decode_json_object(row.get("attributes")),
        extracted=decode_json_object(row.get("extracted")),
        kept_reason=kept_reason,
    )


def shortlist_from_answer(kept: Sequence[Mapping[str, Any]], rows_by_id: Mapping[str, Mapping[str, Any]]) -> list[
    ShortlistedListing
]:
    """The kept listings in the answer's order; one the run's database lacks keeps the answer's own fields."""
    shortlist = []
    for item in kept:
        listing_id = str(item["listing_id"])
        row = rows_by_id.get(listing_id)
        if row is not None:
            shortlist.append(listing_from_row(row, item.get("reason")))
        else:
            shortlist.append(
                ShortlistedListing(
                    listing_id=listing_id, title=item.get("title") or "", price=item.get("price"),
                    url=item.get("url"), seller_username=item.get("seller"), kept_reason=item.get("reason"),
                )
            )
    return shortlist


def fallback_shortlist(rows: Sequence[Mapping[str, Any]]) -> list[ShortlistedListing]:
    """Every row, those with details first, otherwise in the order the run saw them."""
    ordered = sorted(rows, key=lambda row: 0 if row.get("has_details") else 1)  # sorted() is stable
    return [listing_from_row(row) for row in ordered]


def decode_json_object(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


# --- reading the run directory (I/O) -------------------------------------------------------------------------------


def run_directory_for(runs_parent: Path, run_id: str) -> Path:
    """<runs_parent>/runs/<run id>; raises UnknownRunError for a malformed name or a missing folder."""
    if not RUN_ID_PATTERN.match(run_id):
        raise UnknownRunError(f"not a run id: {run_id!r}")
    directory = runs_parent / RUNS_DIRECTORY_NAME / run_id
    if not directory.is_dir():
        raise UnknownRunError(f"no run folder {directory}")
    return directory


def load_run_shortlist(runs_parent: Path, run_id: str) -> RunShortlist:
    """The run's shortlist from answer.json and listings.db (read-only)."""
    directory = run_directory_for(runs_parent, run_id)
    rows = read_listing_rows(directory / LISTINGS_DATABASE_FILE_NAME)
    answer_path = directory / ANSWER_FILE_NAME
    if answer_path.is_file():
        kept = json.loads(answer_path.read_text(encoding="utf-8")).get("kept") or []
        rows_by_id = {str(row["id"]): row for row in rows}
        return RunShortlist(run_id, shortlist_from_answer(kept, rows_by_id), from_answer=True)
    return RunShortlist(run_id, fallback_shortlist(rows), from_answer=False)


def read_listing_rows(database_path: Path) -> list[dict[str, Any]]:
    if not database_path.is_file():
        return []
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM listings ORDER BY rowid")]
    finally:
        connection.close()
