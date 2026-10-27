"""listings.db: every listing one agent run has seen, in its run directory, for run_python to query.

The run's tool hook (toolsets.ToolCallGate) hands each search_listings page and listing_details result to
RunListingsStore, and the judge tools hand it the details they fetch. A listing is one row, updated in place: a
search fills the card columns, details fill the detail columns, and neither erases the other's. listings.json next
to it holds the same rows, rewritten after every change. extract_fields adds one typed column per field it reads,
named x_<field name>, and keeps every extracted value in the JSON column `extracted` too.

Pure: the row builders (search_listing_row, detail_row) and merge_extracted. I/O: RunListingsStore, which owns the
SQLite file and the JSON file. Only the run's event loop thread uses a store.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from carousearch.mcp_server import DETAILS_TOOL, SEARCH_TOOL

LISTINGS_DATABASE_FILE_NAME = "listings.db"
LISTINGS_JSON_FILE_NAME = "listings.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    price               REAL,
    currency            TEXT,
    price_display       TEXT,
    condition           TEXT,
    condition_label     TEXT,
    like_count          INTEGER,
    seller_username     TEXT,
    seller_id           TEXT,
    url                 TEXT,
    thumbnail_url       TEXT,
    posted_at           TEXT,
    bumped_at           TEXT,
    category            TEXT,
    category_name       TEXT,
    snippet             TEXT,
    raw_json            TEXT,
    has_details         INTEGER NOT NULL DEFAULT 0,
    description         TEXT,
    seller_rating       REAL,
    review_count        INTEGER,
    seller_joined_at    TEXT,
    response_rate       TEXT,
    image_urls          TEXT,
    meetup_locations    TEXT,
    attributes          TEXT,
    detail_json         TEXT,
    extracted           TEXT
);
"""
# Columns holding JSON text; listings.json carries them decoded.
JSON_COLUMNS = ("raw_json", "image_urls", "meetup_locations", "attributes", "detail_json", "extracted")
CARD_COLUMNS = (
    "title", "price", "currency", "price_display", "condition", "condition_label", "like_count", "seller_username",
    "seller_id", "url", "thumbnail_url", "snippet", "raw_json",
)
DETAIL_COLUMNS = (
    "title", "price", "currency", "price_display", "condition", "condition_label", "like_count", "seller_username",
    "seller_id", "url", "has_details", "description", "seller_rating", "review_count", "seller_joined_at",
    "response_rate", "image_urls", "meetup_locations", "attributes", "detail_json", "category_name",
)
# Filled only when the new value is known, so a later search or detail never blanks them.
KEEP_IF_UNKNOWN_COLUMNS = ("posted_at", "bumped_at", "category")


# --- rows (pure) ------------------------------------------------------------------------------------------------


def search_listing_row(listing: Mapping[str, Any], category: str | None) -> dict[str, Any]:
    """A search result listing (mcp_server.search_response's shape) as a row; `category` is the one searched."""
    kind = listing.get("posted_or_bumped_kind")
    moment = listing.get("posted_or_bumped_at")
    return {
        "id": str(listing["id"]),
        "title": listing.get("title") or "",
        "price": listing.get("price"),
        "currency": listing.get("currency"),
        "price_display": listing.get("price_display"),
        "condition": listing.get("condition"),
        "condition_label": listing.get("condition_label"),
        "like_count": listing.get("like_count"),
        "seller_username": listing.get("seller_username"),
        "seller_id": listing.get("seller_id"),
        "url": listing.get("listing_url"),
        "thumbnail_url": listing.get("thumbnail_url"),
        "posted_at": moment if kind == "time_created" else None,
        "bumped_at": moment if kind in ("active_bump", "expired_bump") else None,
        "category": category,
        "snippet": listing.get("description_snippet"),
        "raw_json": json.dumps(listing, ensure_ascii=False),
    }


def detail_row(detail: Mapping[str, Any]) -> dict[str, Any]:
    """A listing_details result (mcp_server.detail_response's shape) as a row."""
    meetups = [
        {"name": meetup.get("name"), "address": meetup.get("address")}
        for meetup in detail.get("meetup_locations") or []
        if isinstance(meetup, Mapping)
    ]
    return {
        "id": str(detail["id"]),
        "title": detail.get("title") or "",
        "price": detail.get("price"),
        "currency": detail.get("currency"),
        "price_display": detail.get("price_display"),
        "condition": detail.get("condition"),
        "condition_label": detail.get("condition_label"),
        "like_count": detail.get("like_count"),
        "seller_username": detail.get("seller_username"),
        "seller_id": detail.get("seller_id"),
        "url": detail.get("listing_url"),
        "posted_at": detail.get("created_at"),
        "category": detail.get("category_id"),
        "category_name": detail.get("category_name"),
        "has_details": 1,
        "description": detail.get("description"),
        "seller_rating": detail.get("seller_rating"),
        "review_count": detail.get("seller_review_count"),
        "seller_joined_at": detail.get("seller_joined_at"),
        "response_rate": detail.get("seller_response_rate"),
        "image_urls": json.dumps(detail.get("image_urls") or []),
        "meetup_locations": json.dumps(meetups, ensure_ascii=False),
        "attributes": json.dumps(detail.get("attributes") or {}, ensure_ascii=False),
        "detail_json": json.dumps(dict(detail), ensure_ascii=False),
    }


def merge_extracted(current_json: str | None, values: Mapping[str, Any]) -> str:
    """The `extracted` column's JSON with `values` merged in; a newer value, null included, replaces an older one."""
    current = json.loads(current_json) if current_json else {}
    return json.dumps({**current, **values}, ensure_ascii=False, sort_keys=True)


def upsert_statement(row: Mapping[str, Any], update_columns: Sequence[str]) -> str:
    """INSERT the row; on a known id, overwrite `update_columns` and fill KEEP_IF_UNKNOWN_COLUMNS when known."""
    columns = list(row)
    assignments = [f"{column} = excluded.{column}" for column in update_columns if column in row]
    assignments += [
        f"{column} = COALESCE(excluded.{column}, listings.{column})"
        for column in KEEP_IF_UNKNOWN_COLUMNS
        if column in row
    ]
    placeholders = ", ".join(f":{column}" for column in columns)
    return (
        f"INSERT INTO listings ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {', '.join(assignments)}"
    )


def decoded_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row with its JSON columns decoded, for listings.json and for the judge tools."""
    return {
        column: json.loads(value) if column in JSON_COLUMNS and isinstance(value, str) else value
        for column, value in row.items()
    }


# --- the store (I/O) --------------------------------------------------------------------------------------------


class RunListingsStore:
    """listings.db and listings.json in one run's directory."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.database_path = directory / LISTINGS_DATABASE_FILE_NAME
        self.json_path = directory / LISTINGS_JSON_FILE_NAME
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._write_json()

    def record_tool_result(self, tool_name: str, arguments: Mapping[str, Any], result: Any) -> None:
        """The gate's hook: keep what search_listings and listing_details returned; other tools are ignored."""
        if tool_name == SEARCH_TOOL and isinstance(result, Mapping):
            request = arguments.get("request")
            category = request.get("category") if isinstance(request, Mapping) else None
            self.record_search_page(result.get("listings") or [], category)
        elif tool_name == DETAILS_TOOL and isinstance(result, Mapping) and "id" in result:
            self.record_detail(result)

    def record_search_page(self, listings: Sequence[Mapping[str, Any]], category: str | None) -> None:
        rows = [search_listing_row(listing, category) for listing in listings if isinstance(listing, Mapping)]
        self._upsert(rows, CARD_COLUMNS)

    def record_detail(self, detail: Mapping[str, Any]) -> None:
        self._upsert([detail_row(detail)], DETAIL_COLUMNS)

    def record_extracted(
        self, values_by_listing: Mapping[str, Mapping[str, Any]], columns: Mapping[str, tuple[str, str]]
    ) -> None:
        """Store extracted values ({listing id: {field name: value}}) in `extracted` and in typed columns, given as
        {field name: (column name, SQLite type)} from validated field specs; missing columns are added first."""
        self._add_columns(dict(columns.values()))
        with self._connection:
            for listing_id, values in values_by_listing.items():
                row = self._connection.execute("SELECT extracted FROM listings WHERE id = ?", (listing_id,)).fetchone()
                if row is None:
                    continue
                assignments = "".join(f", {columns[name][0]} = :value_{name}" for name in values)
                parameters = {f"value_{name}": value for name, value in values.items()}
                parameters |= {"listing_id": listing_id, "extracted": merge_extracted(row["extracted"], values)}
                self._connection.execute(
                    f"UPDATE listings SET extracted = :extracted{assignments} WHERE id = :listing_id", parameters
                )
        self._write_json()

    def _add_columns(self, columns: Mapping[str, str]) -> None:
        existing = {row["name"] for row in self._connection.execute("PRAGMA table_info(listings)")}
        with self._connection:
            for name, sqlite_type in columns.items():
                if name not in existing:
                    self._connection.execute(f"ALTER TABLE listings ADD COLUMN {name} {sqlite_type}")

    def listings(self, listing_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The known rows among `listing_ids`, decoded, by id."""
        if not listing_ids:
            return {}
        placeholders = ", ".join("?" for _ in listing_ids)
        cursor = self._connection.execute(f"SELECT * FROM listings WHERE id IN ({placeholders})", list(listing_ids))
        return {row["id"]: decoded_row(dict(row)) for row in cursor}

    def close(self) -> None:
        self._connection.close()

    def _upsert(self, rows: Sequence[dict[str, Any]], update_columns: Sequence[str]) -> None:
        if not rows:
            return
        with self._connection:
            for row in rows:
                self._connection.execute(upsert_statement(row, update_columns), row)
        self._write_json()

    def _write_json(self) -> None:
        rows = [decoded_row(dict(row)) for row in self._connection.execute("SELECT * FROM listings ORDER BY rowid")]
        self.json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
