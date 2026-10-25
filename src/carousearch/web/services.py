"""The web app's blocking work: a screened Carousell search and a listing's detail, plus two short memories.

Everything here is synchronous. Routes run it in a worker thread, one call at a time (`WebServices.turn`),
because the Carousell client, its rate limiter and the SQLite connection must not be used by two threads at once.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from carousearch.filters.schema import ListingDetail, SearchRequest, SearchResult
from carousearch.screening.models import ScreenedListing
from carousearch.screening.pipeline import ScreeningServices, screen_listings
from carousearch.web.view_models import ScreenedPage

logger = logging.getLogger(__name__)

# The same search again within this window (the "Hide multi-item" switch, a debounced keystroke that lands
# after Enter, the back button) is answered from memory instead of from Carousell.
RECENT_SEARCH_WINDOW = timedelta(minutes=2)
RECENT_SEARCH_LIMIT = 20
RECENT_SCREENING_LIMIT = 2000


class CarousellSource(Protocol):
    """What the web app needs from Carousell (CarousellClient satisfies it)."""

    def search(self, request: SearchRequest) -> SearchResult: ...

    def get_listing(self, listing_id: str) -> ListingDetail: ...


class RecentSearches:
    """The screened pages of the last few searches, each kept for `window`."""

    def __init__(self, window: timedelta = RECENT_SEARCH_WINDOW, limit: int = RECENT_SEARCH_LIMIT) -> None:
        self._window = window
        self._limit = limit
        self._pages: OrderedDict[str, tuple[datetime, ScreenedPage]] = OrderedDict()

    def get(self, request: SearchRequest, now: datetime) -> ScreenedPage | None:
        entry = self._pages.get(request.model_dump_json())
        if entry is None or now - entry[0] > self._window:
            return None
        return entry[1]

    def remember(self, request: SearchRequest, page: ScreenedPage, now: datetime) -> None:
        key = request.model_dump_json()
        self._pages[key] = (now, page)
        keep_newest(self._pages, key, self._limit)


class RecentScreenings:
    """The latest screening of each listing shown, so a card's Details panel lists the same tags as the card."""

    def __init__(self, limit: int = RECENT_SCREENING_LIMIT) -> None:
        self._limit = limit
        self._by_listing_id: OrderedDict[str, ScreenedListing] = OrderedDict()

    def get(self, listing_id: str) -> ScreenedListing | None:
        return self._by_listing_id.get(listing_id)

    def remember(self, screened: Sequence[ScreenedListing]) -> None:
        for item in screened:
            self._by_listing_id[item.listing.id] = item
            keep_newest(self._by_listing_id, item.listing.id, self._limit)


def keep_newest(entries: OrderedDict, newest_key: str, limit: int) -> None:
    entries.move_to_end(newest_key)
    while len(entries) > limit:
        entries.popitem(last=False)


@dataclass
class WebServices:
    """Everything the routes need. Built once when the app starts (web.app) and kept in `app.state.services`."""

    carousell: CarousellSource
    screening: ScreeningServices  # its client is the same Carousell client; its clock is the app's clock
    price_lookup_enabled: bool  # False when no ANTHROPIC_API_KEY is set (NoopPriceResolver)
    close: Callable[[], None] = field(default=lambda: None)
    recent_searches: RecentSearches = field(default_factory=RecentSearches)
    recent_screenings: RecentScreenings = field(default_factory=RecentScreenings)
    # Held around every call into the functions below; see the module docstring.
    turn: asyncio.Lock = field(default_factory=asyncio.Lock)


def search_and_screen(request: SearchRequest, services: WebServices) -> ScreenedPage:
    """One page of results, screened (rules, budgeted detail fetches, price lookup); a recent repeat is free."""
    now = services.screening.clock()
    remembered = services.recent_searches.get(request, now)
    if remembered is not None:
        logger.info("Search answered from memory")
        return remembered
    filters = request.model_dump(mode="json", exclude_defaults=True, exclude={"page_cursor", "page_size"})
    logger.info("Carousell search %s (%s)", filters, "next page" if request.page_cursor else "first page")
    result = services.carousell.search(request)
    screened = screen_listings(result.listings[: request.page_size], request, services.screening)
    page = ScreenedPage(listings=screened, total_display=result.total_display, next_page=result.next_page)
    services.recent_searches.remember(request, page, now)
    services.recent_screenings.remember(screened)
    return page


@dataclass(frozen=True)
class LoadedListing:
    detail: ListingDetail
    screened: ScreenedListing | None  # None when the listing was not in a recent search (e.g. after a restart)


def load_listing(listing_id: str, services: WebServices) -> LoadedListing:
    """The detail from the listing's screening, else the 12-hour detail cache, else Carousell (then cached)."""
    screened = services.recent_screenings.get(listing_id)
    if screened is not None and screened.detail is not None:
        return LoadedListing(screened.detail, screened)
    store = services.screening.store
    now = services.screening.clock()
    detail = store.load_detail(listing_id, now)
    if detail is None:
        logger.info("Carousell listing detail %s", listing_id)
        detail = services.carousell.get_listing(listing_id)
        store.save_detail(detail, now)
    return LoadedListing(detail, screened)
