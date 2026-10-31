"""Where outreach keeps every drafted and sent message: data/outreach.db, one row per message, kept forever so
that a listing or seller is never messaged twice across runs.

I/O: SQLite through the stdlib. The connection may be shared by the web app's request threads and its send
thread, so every call holds one lock. Timestamps are ISO 8601 text in UTC (carousearch.storage.to_text).

The reply columns (reply_text, reply_at, reply_class, counter_price) are for the later reply-reading phase;
nothing writes them yet.
"""

import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from carousearch.outreach.models import MessageStatus, OutreachDraft, OutreachMessage
from carousearch.storage import from_text, to_text

OUTREACH_DATABASE_FILE_NAME = "outreach.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    listing_id      TEXT NOT NULL,
    seller_id       TEXT,
    seller_username TEXT,
    listing_title   TEXT NOT NULL,
    listing_url     TEXT,
    listing_price   REAL,
    offer_price     REAL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    sent_at         TEXT,
    error           TEXT,
    skip_reason     TEXT,
    chat_url        TEXT,
    reply_text      TEXT,
    reply_at        TEXT,
    reply_class     TEXT,
    counter_price   REAL
);
CREATE INDEX IF NOT EXISTS outreach_messages_run ON outreach_messages (run_id);
CREATE INDEX IF NOT EXISTS outreach_messages_listing ON outreach_messages (listing_id);
CREATE INDEX IF NOT EXISTS outreach_messages_seller ON outreach_messages (seller_id);
CREATE INDEX IF NOT EXISTS outreach_messages_status ON outreach_messages (status, sent_at);
"""
DRAFT_COLUMNS = (
    "run_id", "listing_id", "seller_id", "seller_username", "listing_title", "listing_url", "listing_price",
    "offer_price", "body",
)


class SqliteOutreachStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._lock = threading.Lock()

    @classmethod
    def open(cls, database_path: Path) -> "SqliteOutreachStore":
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(database_path, check_same_thread=False))

    def close(self) -> None:
        self._connection.close()

    # --- writes ---------------------------------------------------------------------------------------------------

    def add_drafts(self, drafts: Sequence[OutreachDraft], created_at: datetime) -> list[OutreachMessage]:
        """Store new drafts with status "drafted"; returns them with their ids, in order."""
        placeholders = ", ".join("?" for _ in DRAFT_COLUMNS)
        statement = (
            f"INSERT INTO outreach_messages ({', '.join(DRAFT_COLUMNS)}, status, created_at) "
            f"VALUES ({placeholders}, 'drafted', ?)"
        )
        ids = []
        with self._lock, self._connection:
            for draft in drafts:
                values = [getattr(draft, column) for column in DRAFT_COLUMNS]
                ids.append(self._connection.execute(statement, [*values, to_text(created_at)]).lastrowid)
        return self.get_many(ids)

    def update_body(self, message_id: int, body: str) -> None:
        """The owner's edit of a draft; only drafted or approved messages change."""
        self._execute(
            "UPDATE outreach_messages SET body = ? WHERE id = ? AND status IN ('drafted', 'approved')",
            (body, message_id),
        )

    def approve(self, message_ids: Sequence[int], approved_at: datetime) -> None:
        """Drafted messages among `message_ids` become approved; others are left as they are."""
        self._execute_many(
            "UPDATE outreach_messages SET status = 'approved', approved_at = ? WHERE id = ? AND status = 'drafted'",
            [(to_text(approved_at), message_id) for message_id in message_ids],
        )

    def mark_sent(self, message_id: int, sent_at: datetime, chat_url: str | None, note: str | None = None) -> None:
        self._execute(
            "UPDATE outreach_messages SET status = 'sent', sent_at = ?, chat_url = ?, error = ? WHERE id = ?",
            (to_text(sent_at), chat_url, note, message_id),
        )

    def mark_failed(self, message_id: int, error: str) -> None:
        self._execute("UPDATE outreach_messages SET status = 'failed', error = ? WHERE id = ?", (error, message_id))

    def mark_skipped(self, message_ids: Sequence[int], reason: str) -> None:
        self._execute_many(
            "UPDATE outreach_messages SET status = 'skipped', skip_reason = ? WHERE id = ? AND status != 'sent'",
            [(reason, message_id) for message_id in message_ids],
        )

    # --- reads ----------------------------------------------------------------------------------------------------

    def get_many(self, message_ids: Sequence[int]) -> list[OutreachMessage]:
        """The messages with these ids, in the order given (unknown ids are left out)."""
        if not message_ids:
            return []
        placeholders = ", ".join("?" for _ in message_ids)
        rows = self._query(f"SELECT * FROM outreach_messages WHERE id IN ({placeholders})", list(message_ids))
        by_id = {message.id: message for message in map(message_from_row, rows)}
        return [by_id[message_id] for message_id in message_ids if message_id in by_id]

    def list_for_run(self, run_id: str, statuses: Sequence[MessageStatus] = ()) -> list[OutreachMessage]:
        """The run's messages, oldest first, optionally only those with one of `statuses`."""
        status_filter, parameters = status_clause(statuses)
        rows = self._query(
            f"SELECT * FROM outreach_messages WHERE run_id = ?{status_filter} ORDER BY id", [run_id, *parameters]
        )
        return [message_from_row(row) for row in rows]

    def list_recent(self, limit: int = 200, statuses: Sequence[MessageStatus] = ()) -> list[OutreachMessage]:
        """Every run's messages, newest first."""
        status_filter, parameters = status_clause(statuses)
        where = f"WHERE 1 = 1{status_filter}"
        rows = self._query(f"SELECT * FROM outreach_messages {where} ORDER BY id DESC LIMIT ?", [*parameters, limit])
        return [message_from_row(row) for row in rows]

    def already_contacted(self, listing_id: str, seller_id: str | None, seller_username: str | None = None) -> bool:
        """Whether any run has sent a message to this listing, or to this seller (by id, else by username)."""
        row = self._query(
            "SELECT 1 FROM outreach_messages WHERE status = 'sent' AND (listing_id = ? "
            "OR (seller_id IS NOT NULL AND seller_id = ?) OR (seller_username IS NOT NULL AND seller_username = ?)) "
            "LIMIT 1",
            [listing_id, seller_id, seller_username],
        )
        return bool(row)

    def sent_count_since(self, since: datetime) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS sent FROM outreach_messages WHERE status = 'sent' AND sent_at >= ?", [to_text(since)]
        )
        return rows[0]["sent"]

    # --- plumbing -------------------------------------------------------------------------------------------------

    def _execute(self, statement: str, parameters: Sequence[object]) -> None:
        with self._lock, self._connection:
            self._connection.execute(statement, parameters)

    def _execute_many(self, statement: str, rows: Sequence[Sequence[object]]) -> None:
        with self._lock, self._connection:
            self._connection.executemany(statement, rows)

    def _query(self, statement: str, parameters: Sequence[object]) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(statement, parameters).fetchall()


def status_clause(statuses: Sequence[MessageStatus]) -> tuple[str, list[str]]:
    """An " AND status IN (...)" fragment and its parameters; empty when no statuses are given."""
    if not statuses:
        return "", []
    return f" AND status IN ({', '.join('?' for _ in statuses)})", list(statuses)


def message_from_row(row: sqlite3.Row) -> OutreachMessage:
    values = dict(row)
    for column in ("created_at", "sent_at"):
        values[column] = from_text(values[column]) if values[column] else None
    return OutreachMessage.model_validate(values)
