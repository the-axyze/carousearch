"""SQLite persistence: listings already seen, cached listing details, saved searches, the market tables, and
the screening price cache.

I/O module: every function takes an open connection, so tests can use a temporary database.
Timestamps are stored as ISO 8601 strings in UTC, written and read only through `to_text` / `from_text`.

The market tables (`listings`, `listing_changes`, `harvest_runs`, `harvest_run_listings`) hold one row
per Carousell listing, overwritten in place, plus a log of content changes and of each harvest run
(docs/ANALYSIS_AND_SCREENING_PLAN.md section 3.1).

`price_resolution_cache` holds the price the screening resolver found for a listing and a search, so a listing
is never resolved twice for the same search.
"""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from carousearch.filters.schema import Condition, Listing, ListingDetail, PostedOrBumpedKind, SearchRequest
from carousearch.market.hashing import listing_content_hash
from carousearch.market.limits import HarvestLimits
from carousearch.screening.models import ResolvedPrice

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
CREATE TABLE IF NOT EXISTS listings (
    listing_id            TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    price                 REAL,
    currency              TEXT,
    price_display         TEXT NOT NULL,
    condition             TEXT,
    condition_label       TEXT,
    like_count            INTEGER NOT NULL,
    seller_username       TEXT,
    seller_id             TEXT,
    thumbnail_url         TEXT,
    listing_url           TEXT NOT NULL,
    posted_or_bumped_at   TEXT,
    posted_or_bumped_kind TEXT,
    content_hash          TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    last_changed_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    old_price     REAL,
    new_price     REAL,
    old_condition TEXT,
    new_condition TEXT,
    old_title     TEXT NOT NULL,
    new_title     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listing_changes_listing ON listing_changes (listing_id, changed_at);
CREATE TABLE IF NOT EXISTS harvest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_hash  TEXT NOT NULL,
    request_json  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    listing_count INTEGER NOT NULL DEFAULT 0,
    stop_reason   TEXT,
    limits_json   TEXT NOT NULL DEFAULT '{}'    -- market.limits.HarvestLimits the run was made with
);
CREATE INDEX IF NOT EXISTS harvest_runs_lookup ON harvest_runs (request_hash, started_at);
CREATE TABLE IF NOT EXISTS harvest_run_listings (
    run_id     INTEGER NOT NULL,
    listing_id TEXT NOT NULL,
    position   INTEGER NOT NULL,
    PRIMARY KEY (run_id, listing_id)
);
CREATE TABLE IF NOT EXISTS price_resolution_cache (
    listing_id    TEXT NOT NULL,
    query         TEXT NOT NULL,              -- normalised search keywords (screening.pipeline.normalise_query)
    resolved_json TEXT NOT NULL,              -- screening.models.ResolvedPrice
    resolved_at   TEXT NOT NULL,
    PRIMARY KEY (listing_id, query)
);
"""

# SQLite limits the number of `?` parameters in one statement; stay well under the oldest default (999).
_QUERY_CHUNK_SIZE = 500


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


@dataclass(frozen=True)
class ListingRow:
    """One row of `listings`; the input type for all analysis functions."""

    listing_id: str
    title: str
    price: float | None
    currency: str | None
    price_display: str
    condition: Condition | None
    condition_label: str | None
    like_count: int
    seller_username: str | None
    seller_id: str | None
    thumbnail_url: str | None
    listing_url: str
    posted_or_bumped_at: datetime | None
    posted_or_bumped_kind: PostedOrBumpedKind | None
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime


@dataclass(frozen=True)
class ListingChange:
    """One row of `listing_changes`: a listing whose title, price or condition changed between pulls."""

    id: int
    listing_id: str
    changed_at: datetime
    old_price: float | None
    new_price: float | None
    old_condition: Condition | None
    new_condition: Condition | None
    old_title: str
    new_title: str


@dataclass(frozen=True)
class HarvestRun:
    """One row of `harvest_runs`. `finished_at` and `stop_reason` are None while the run is in progress."""

    id: int
    request_hash: str
    request: SearchRequest
    started_at: datetime
    finished_at: datetime | None
    pages_fetched: int
    listing_count: int
    stop_reason: str | None
    limits: HarvestLimits


@dataclass(frozen=True)
class UpsertSummary:
    new_ids: list[str]
    changed_ids: list[str]
    unchanged_ids: list[str]


def open_database(database_path: Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (and create on first use) the database file and its tables.

    `check_same_thread=False` lets worker threads share the connection; the caller must then make sure only one
    thread uses it at a time (the web app runs every blocking call under one lock).
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, check_same_thread=check_same_thread)
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
                (listing.id, to_text(seen_at), to_text(seen_at), listing.title, listing.price),
            )
    return new_listing_ids


def get_seen_listing(connection: sqlite3.Connection, listing_id: str) -> SeenListing | None:
    row = connection.execute(
        "SELECT listing_id, first_seen_at, last_seen_at, title, price FROM seen_listings WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        return None
    return SeenListing(row[0], from_text(row[1]), from_text(row[2]), row[3], row[4])


# --- listing_detail_cache --------------------------------------------------


def cache_listing_detail(connection: sqlite3.Connection, detail: ListingDetail, fetched_at: datetime) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO listing_detail_cache (listing_id, fetched_at, json) VALUES (?, ?, ?)",
            (detail.id, to_text(fetched_at), detail.model_dump_json()),
        )


def load_cached_listing_detail(
    connection: sqlite3.Connection, listing_id: str, now: datetime, max_age: timedelta
) -> ListingDetail | None:
    """The cached detail if it was fetched within `max_age` of `now`, otherwise None."""
    row = connection.execute(
        "SELECT fetched_at, json FROM listing_detail_cache WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None or now - from_text(row[0]) > max_age:
        return None
    return ListingDetail.model_validate_json(row[1])


# --- price_resolution_cache ------------------------------------------------


def save_price_resolution(
    connection: sqlite3.Connection, listing_id: str, query: str, resolved: ResolvedPrice, resolved_at: datetime
) -> None:
    """Store (or replace) the resolved price of one listing for one search."""
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO price_resolution_cache (listing_id, query, resolved_json, resolved_at)
            VALUES (?, ?, ?, ?)
            """,
            (listing_id, query, resolved.model_dump_json(), to_text(resolved_at)),
        )


def load_price_resolution(connection: sqlite3.Connection, listing_id: str, query: str) -> ResolvedPrice | None:
    row = connection.execute(
        "SELECT resolved_json FROM price_resolution_cache WHERE listing_id = ? AND query = ?", (listing_id, query)
    ).fetchone()
    return ResolvedPrice.model_validate_json(row[0]) if row is not None else None


# --- listings --------------------------------------------------------------

_LISTING_COLUMNS = (
    "listing_id, title, price, currency, price_display, condition, condition_label, like_count, "
    "seller_username, seller_id, thumbnail_url, listing_url, posted_or_bumped_at, posted_or_bumped_kind, "
    "content_hash, first_seen_at, last_seen_at, last_changed_at"
)


def upsert_listings(connection: sqlite3.Connection, listings: Iterable[Listing], seen_at: datetime) -> UpsertSummary:
    """Write a pull's listings, one row per listing id, overwritten in place.

    `like_count`, the bump timestamp and `last_seen_at` are always written. The rest of the row is rewritten, and a
    `listing_changes` row appended, only when the content hash (title, price, condition) differs from the
    stored one. A listing id repeated within one call (the same listing on two pages) is processed once,
    from its first occurrence.
    """
    seen_text = to_text(seen_at)
    new_ids: list[str] = []
    changed_ids: list[str] = []
    unchanged_ids: list[str] = []
    processed_ids: set[str] = set()
    with connection:
        for listing in listings:
            if listing.id in processed_ids:
                continue
            processed_ids.add(listing.id)
            content_hash = listing_content_hash(listing)
            existing = connection.execute(
                "SELECT content_hash, price, condition, title FROM listings WHERE listing_id = ?", (listing.id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"INSERT INTO listings ({_LISTING_COLUMNS}) VALUES ({', '.join('?' * 18)})",
                    (*_listing_content_values(listing), content_hash, seen_text, seen_text, seen_text),
                )
                new_ids.append(listing.id)
            elif existing[0] == content_hash:
                connection.execute(
                    """
                    UPDATE listings SET like_count = ?, posted_or_bumped_at = ?, posted_or_bumped_kind = ?, last_seen_at = ?
                    WHERE listing_id = ?
                    """,
                    (
                        listing.like_count,
                        to_text(listing.posted_or_bumped_at) if listing.posted_or_bumped_at else None,
                        listing.posted_or_bumped_kind.value if listing.posted_or_bumped_kind else None,
                        seen_text,
                        listing.id,
                    ),
                )
                unchanged_ids.append(listing.id)
            else:
                connection.execute(
                    """
                    UPDATE listings SET
                        title = ?, price = ?, currency = ?, price_display = ?, condition = ?, condition_label = ?,
                        like_count = ?, seller_username = ?, seller_id = ?, thumbnail_url = ?, listing_url = ?,
                        posted_or_bumped_at = ?, posted_or_bumped_kind = ?, content_hash = ?,
                        last_seen_at = ?, last_changed_at = ?
                    WHERE listing_id = ?
                    """,
                    (*_listing_content_values(listing)[1:], content_hash, seen_text, seen_text, listing.id),
                )
                connection.execute(
                    """
                    INSERT INTO listing_changes (
                        listing_id, changed_at, old_price, new_price, old_condition, new_condition, old_title, new_title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing.id,
                        seen_text,
                        existing[1],
                        listing.price,
                        existing[2],
                        listing.condition.value if listing.condition else None,
                        existing[3],
                        listing.title,
                    ),
                )
                changed_ids.append(listing.id)
    return UpsertSummary(new_ids=new_ids, changed_ids=changed_ids, unchanged_ids=unchanged_ids)


def load_listing_rows(connection: sqlite3.Connection, listing_ids: Sequence[str]) -> list[ListingRow]:
    """The stored rows for `listing_ids`, in the order given. Ids with no row are skipped."""
    rows_by_id: dict[str, ListingRow] = {}
    unique_ids = list(dict.fromkeys(listing_ids))
    for start in range(0, len(unique_ids), _QUERY_CHUNK_SIZE):
        chunk = unique_ids[start : start + _QUERY_CHUNK_SIZE]
        placeholders = ", ".join("?" * len(chunk))
        for row in connection.execute(
            f"SELECT {_LISTING_COLUMNS} FROM listings WHERE listing_id IN ({placeholders})", chunk
        ):
            rows_by_id[row[0]] = _listing_row_from_tuple(row)
    return [rows_by_id[listing_id] for listing_id in listing_ids if listing_id in rows_by_id]


def load_listing_changes(connection: sqlite3.Connection, listing_id: str) -> list[ListingChange]:
    """Every recorded change of one listing, oldest first."""
    rows = connection.execute(
        """
        SELECT id, listing_id, changed_at, old_price, new_price, old_condition, new_condition, old_title, new_title
        FROM listing_changes WHERE listing_id = ? ORDER BY changed_at, id
        """,
        (listing_id,),
    ).fetchall()
    return [
        ListingChange(
            id=row[0],
            listing_id=row[1],
            changed_at=from_text(row[2]),
            old_price=row[3],
            new_price=row[4],
            old_condition=_optional_condition(row[5]),
            new_condition=_optional_condition(row[6]),
            old_title=row[7],
            new_title=row[8],
        )
        for row in rows
    ]


def _listing_content_values(listing: Listing) -> tuple:
    """The `listings` columns taken from the Listing itself, in `_LISTING_COLUMNS` order (listing_id first)."""
    return (
        listing.id,
        listing.title,
        listing.price,
        listing.currency,
        listing.price_display,
        listing.condition.value if listing.condition else None,
        listing.condition_label,
        listing.like_count,
        listing.seller_username,
        listing.seller_id,
        listing.thumbnail_url,
        listing.listing_url,
        to_text(listing.posted_or_bumped_at) if listing.posted_or_bumped_at else None,
        listing.posted_or_bumped_kind.value if listing.posted_or_bumped_kind else None,
    )


def _listing_row_from_tuple(row: tuple) -> ListingRow:
    return ListingRow(
        listing_id=row[0],
        title=row[1],
        price=row[2],
        currency=row[3],
        price_display=row[4],
        condition=_optional_condition(row[5]),
        condition_label=row[6],
        like_count=row[7],
        seller_username=row[8],
        seller_id=row[9],
        thumbnail_url=row[10],
        listing_url=row[11],
        posted_or_bumped_at=from_text(row[12]) if row[12] is not None else None,
        posted_or_bumped_kind=PostedOrBumpedKind(row[13]) if row[13] is not None else None,
        content_hash=row[14],
        first_seen_at=from_text(row[15]),
        last_seen_at=from_text(row[16]),
        last_changed_at=from_text(row[17]),
    )


def _optional_condition(value: str | None) -> Condition | None:
    return Condition(value) if value is not None else None


# --- harvest_runs ------------------------------------------------------------

_HARVEST_RUN_COLUMNS = (
    "id, request_hash, request_json, started_at, finished_at, pages_fetched, listing_count, stop_reason, limits_json"
)


def create_harvest_run(
    connection: sqlite3.Connection,
    request: SearchRequest,
    request_hash: str,
    started_at: datetime,
    limits: HarvestLimits | None = None,
) -> int:
    """Record the start of a harvest and return its run id. The request is stored without its page cursor."""
    request_json = request.model_copy(update={"page_cursor": None}).model_dump_json()
    limits_json = (limits or HarvestLimits()).model_dump_json()
    with connection:
        cursor = connection.execute(
            "INSERT INTO harvest_runs (request_hash, request_json, started_at, limits_json) VALUES (?, ?, ?, ?)",
            (request_hash, request_json, to_text(started_at), limits_json),
        )
    return cursor.lastrowid


def add_run_listings(
    connection: sqlite3.Connection, run_id: int, listing_ids: Sequence[str], first_position: int
) -> None:
    """Attach listings to a run at positions `first_position + index`.

    `INSERT OR IGNORE`, so a listing already in the run (seen on an earlier page) keeps its first position.
    """
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO harvest_run_listings (run_id, listing_id, position) VALUES (?, ?, ?)",
            [(run_id, listing_id, first_position + index) for index, listing_id in enumerate(listing_ids)],
        )


def finish_harvest_run(
    connection: sqlite3.Connection,
    run_id: int,
    finished_at: datetime,
    pages_fetched: int,
    listing_count: int,
    stop_reason: str,
) -> None:
    """Close a run with its final counts and why it stopped (a `market.harvest.StopReason` value)."""
    with connection:
        connection.execute(
            """
            UPDATE harvest_runs SET finished_at = ?, pages_fetched = ?, listing_count = ?, stop_reason = ?
            WHERE id = ?
            """,
            (to_text(finished_at), pages_fetched, listing_count, str(stop_reason), run_id),
        )


def find_recent_runs(
    connection: sqlite3.Connection, request_hash: str, now: datetime, max_age: timedelta
) -> list[HarvestRun]:
    """Finished, non-error runs of this request that finished within `max_age` of `now`, newest first.

    Runs still in progress (`finished_at` NULL) and runs that stopped on an error are never candidates for
    reuse. Whether a candidate actually covers what a new harvest asks for is decided by the caller
    (`market.harvest.run_covers`), because that depends on the limits of both.
    """
    rows = connection.execute(
        f"""
        SELECT {_HARVEST_RUN_COLUMNS} FROM harvest_runs
        WHERE request_hash = ? AND finished_at IS NOT NULL AND stop_reason != 'error'
        ORDER BY started_at DESC, id DESC
        """,
        (request_hash,),
    ).fetchall()
    return [_harvest_run_from_tuple(row) for row in rows if now - from_text(row[4]) <= max_age]


def find_reusable_run(
    connection: sqlite3.Connection, request_hash: str, now: datetime, max_age: timedelta
) -> HarvestRun | None:
    """The newest run `find_recent_runs` would return, or None."""
    runs = find_recent_runs(connection, request_hash, now, max_age)
    return runs[0] if runs else None


def load_run_listing_ids(connection: sqlite3.Connection, run_id: int) -> list[str]:
    """The listing ids of a run in Carousell's order across pages."""
    rows = connection.execute(
        "SELECT listing_id FROM harvest_run_listings WHERE run_id = ? ORDER BY position", (run_id,)
    ).fetchall()
    return [row[0] for row in rows]


def _harvest_run_from_tuple(row: tuple) -> HarvestRun:
    return HarvestRun(
        id=row[0],
        request_hash=row[1],
        request=SearchRequest.model_validate_json(row[2]),
        started_at=from_text(row[3]),
        finished_at=from_text(row[4]) if row[4] is not None else None,
        pages_fetched=row[5],
        listing_count=row[6],
        stop_reason=row[7],
        limits=HarvestLimits.model_validate_json(row[8]),
    )


# --- saved_searches --------------------------------------------------------


def save_search(connection: sqlite3.Connection, name: str, request: SearchRequest, created_at: datetime) -> int:
    """Store a search under a unique name and return its id."""
    with connection:
        cursor = connection.execute(
            "INSERT INTO saved_searches (name, request_json, created_at) VALUES (?, ?, ?)",
            (name, request.model_dump_json(), to_text(created_at)),
        )
    return cursor.lastrowid


def list_saved_searches(connection: sqlite3.Connection) -> list[SavedSearch]:
    rows = connection.execute("SELECT id, name, request_json, created_at FROM saved_searches ORDER BY id").fetchall()
    return [SavedSearch(row[0], row[1], SearchRequest.model_validate_json(row[2]), from_text(row[3])) for row in rows]


def delete_saved_search(connection: sqlite3.Connection, saved_search_id: int) -> bool:
    """Delete a saved search. Returns False if no search had that id."""
    with connection:
        cursor = connection.execute("DELETE FROM saved_searches WHERE id = ?", (saved_search_id,))
    return cursor.rowcount > 0


# --- timestamps --------------------------------------------------------------


def to_text(moment: datetime) -> str:
    """A timezone-aware datetime as ISO 8601 text in UTC; naive datetimes are rejected."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat()


def from_text(text: str) -> datetime:
    return datetime.fromisoformat(text)


# The original private names, kept for existing callers.
_to_text = to_text
_from_text = from_text
