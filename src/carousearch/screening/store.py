"""Where screening keeps listing details and resolved prices.

`ScreeningStore` is the interface the pipeline uses; `SqliteScreeningStore` implements it over the
`listing_detail_cache` and `price_resolution_cache` tables (I/O through carousearch.storage).
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Protocol

from carousearch import storage
from carousearch.filters.schema import ListingDetail
from carousearch.screening.models import ResolvedPrice

# The same freshness the bot's Details button uses.
DEFAULT_DETAIL_MAX_AGE = timedelta(hours=12)


class ScreeningStore(Protocol):
    def load_detail(self, listing_id: str, now: datetime) -> ListingDetail | None:
        """A cached detail that is still fresh at `now`, or None."""
        ...

    def save_detail(self, detail: ListingDetail, fetched_at: datetime) -> None: ...

    def load_resolved_price(self, listing_id: str, query: str) -> ResolvedPrice | None: ...

    def save_resolved_price(self, listing_id: str, query: str, resolved: ResolvedPrice, resolved_at: datetime) -> None:
        ...


class SqliteScreeningStore:
    def __init__(self, connection: sqlite3.Connection, detail_max_age: timedelta = DEFAULT_DETAIL_MAX_AGE) -> None:
        self._connection = connection
        self._detail_max_age = detail_max_age

    def load_detail(self, listing_id: str, now: datetime) -> ListingDetail | None:
        return storage.load_cached_listing_detail(self._connection, listing_id, now, self._detail_max_age)

    def save_detail(self, detail: ListingDetail, fetched_at: datetime) -> None:
        storage.cache_listing_detail(self._connection, detail, fetched_at)

    def load_resolved_price(self, listing_id: str, query: str) -> ResolvedPrice | None:
        return storage.load_price_resolution(self._connection, listing_id, query)

    def save_resolved_price(self, listing_id: str, query: str, resolved: ResolvedPrice, resolved_at: datetime) -> None:
        storage.save_price_resolution(self._connection, listing_id, query, resolved, resolved_at)
