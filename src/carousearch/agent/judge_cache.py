"""Where the judge tools and extract_fields keep their answers, by (kind, listing id, criterion, worker model), so
asking the same question about the same listing again costs no model call. For extract_fields the criterion is the
field spec's hash and the kind of text it was read from (agent.extraction.spec_cache_key).

`JudgementCache` is what the tools use; `SqliteJudgementCache` keeps it in the `judgement_cache` table of the
main database (I/O through carousearch.storage), and `MemoryJudgementCache` in a dict (tests, and the web app's
default before it is given the database). Callers hold McpServices.lock around every call, as for the detail cache,
because the connection is shared.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Literal, Protocol

from carousearch import storage

JudgementKind = Literal["listing", "photo", "extraction"]


class JudgementCache(Protocol):
    def load(self, kind: JudgementKind, listing_id: str, criterion: str, model: str) -> dict[str, Any] | None: ...

    def save(
        self,
        kind: JudgementKind,
        listing_id: str,
        criterion: str,
        model: str,
        judgement: dict[str, Any],
        judged_at: datetime,
    ) -> None: ...


class SqliteJudgementCache:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def load(self, kind: JudgementKind, listing_id: str, criterion: str, model: str) -> dict[str, Any] | None:
        text = storage.load_judgement(self._connection, kind, listing_id, criterion, model)
        return json.loads(text) if text is not None else None

    def save(
        self,
        kind: JudgementKind,
        listing_id: str,
        criterion: str,
        model: str,
        judgement: dict[str, Any],
        judged_at: datetime,
    ) -> None:
        storage.save_judgement(
            self._connection, kind, listing_id, criterion, model, json.dumps(judgement, ensure_ascii=False), judged_at
        )


class MemoryJudgementCache:
    def __init__(self) -> None:
        self.judgements: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def load(self, kind: JudgementKind, listing_id: str, criterion: str, model: str) -> dict[str, Any] | None:
        return self.judgements.get((kind, listing_id, criterion, model))

    def save(
        self,
        kind: JudgementKind,
        listing_id: str,
        criterion: str,
        model: str,
        judgement: dict[str, Any],
        judged_at: datetime,
    ) -> None:
        self.judgements[(kind, listing_id, criterion, model)] = judgement
