"""SQLite persistence: listings already seen, cached listing details, and saved searches.

Every function takes an open connection, so tests can use a temporary database.
Timestamps are stored as ISO 8601 strings in UTC.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from carousearch.filters.schema import Listing, ListingDetail, SearchRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    listing_id    TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    title         TEXT NOT NULL,
    price         REAL
);
CREATE TABLE IF NOT EXISTS listing_detail_cache (
    listing_id TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    json       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_searches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    request_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SeenListing:
    listing_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    title: str
    price: float | None


@dataclass(frozen=True)
class SavedSearch:
    id: int
    name: str
    request: SearchRequest
    created_at: datetime


def open_database(database_path: Path) -> sqlite3.Connection:
    """Open (and create on first use) the database file and its tables."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.executescript(SCHEMA)
    return connection


# --- seen_listings ---------------------------------------------------------


def record_seen_listings(connection: sqlite3.Connection, listings: Iterable[Listing], seen_at: datetime) -> list[str]:
    """Mark listings as seen at `seen_at`. Returns the ids that had never been seen before."""
    new_listing_ids = []
    with connection:
        for listing in listings:
            if get_seen_listing(connection, listing.id) is None:
                new_listing_ids.append(listing.id)
            connection.execute(
                """
                INSERT INTO seen_listings (listing_id, first_seen_at, last_seen_at, title, price)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (listing_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at, title = excluded.title, price = excluded.price
                """,
                (listing.id, _to_text(seen_at), _to_text(seen_at), listing.title, listing.price),
            )
    return new_listing_ids


def get_seen_listing(connection: sqlite3.Connection, listing_id: str) -> SeenListing | None:
    row = connection.execute(
        "SELECT listing_id, first_seen_at, last_seen_at, title, price FROM seen_listings WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        return None
    return SeenListing(row[0], _from_text(row[1]), _from_text(row[2]), row[3], row[4])


# --- listing_detail_cache --------------------------------------------------


def cache_listing_detail(connection: sqlite3.Connection, detail: ListingDetail, fetched_at: datetime) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO listing_detail_cache (listing_id, fetched_at, json) VALUES (?, ?, ?)",
            (detail.id, _to_text(fetched_at), detail.model_dump_json()),
        )


def load_cached_listing_detail(
    connection: sqlite3.Connection, listing_id: str, now: datetime, max_age: timedelta
) -> ListingDetail | None:
    """The cached detail if it was fetched within `max_age` of `now`, otherwise None."""
    row = connection.execute(
        "SELECT fetched_at, json FROM listing_detail_cache WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None or now - _from_text(row[0]) > max_age:
        return None
    return ListingDetail.model_validate_json(row[1])


# --- saved_searches --------------------------------------------------------


def save_search(connection: sqlite3.Connection, name: str, request: SearchRequest, created_at: datetime) -> int:
    """Store a search under a unique name and return its id."""
    with connection:
        cursor = connection.execute(
            "INSERT INTO saved_searches (name, request_json, created_at) VALUES (?, ?, ?)",
            (name, request.model_dump_json(), _to_text(created_at)),
        )
    return cursor.lastrowid


def list_saved_searches(connection: sqlite3.Connection) -> list[SavedSearch]:
    rows = connection.execute("SELECT id, name, request_json, created_at FROM saved_searches ORDER BY id").fetchall()
    return [SavedSearch(row[0], row[1], SearchRequest.model_validate_json(row[2]), _from_text(row[3])) for row in rows]


def delete_saved_search(connection: sqlite3.Connection, saved_search_id: int) -> bool:
    """Delete a saved search. Returns False if no search had that id."""
    with connection:
        cursor = connection.execute("DELETE FROM saved_searches WHERE id = ?", (saved_search_id,))
    return cursor.rowcount > 0


def _to_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def _from_text(text: str) -> datetime:
    return datetime.fromisoformat(text)
