"""Pull every page of one Carousell search into the `listings` table and record the run (Carousell and SQLite I/O).

A harvest is the data-collection half of a market analysis: it walks Carousell's `next_page` cursor until a
limit is hit, upserts what it saw (`storage.upsert_listings`) and records which listings the run returned
(`harvest_runs`, `harvest_run_listings`), so an analysis reads exactly that set. An identical request
harvested recently is reused instead of scraped again. `decide_stop` is the pure stop logic.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from carousearch import storage
from carousearch.carousell.client import CarousellClient
from carousearch.filters.schema import Listing, SearchRequest, SortOrder
from carousearch.market.hashing import search_request_hash
from carousearch.market.limits import HarvestLimits
from carousearch.storage import HarvestRun, UpsertSummary

logger = logging.getLogger(__name__)

DEFAULT_REUSE_WINDOW = timedelta(minutes=30)

ProgressCallback = Callable[[int, int], None]  # (pages fetched so far, listings so far)


class StopReason(StrEnum):
    NO_MORE_PAGES = "no_more_pages"
    MAX_PAGES = "max_pages"
    MAX_AGE = "max_age"
    ERROR = "error"


@dataclass(frozen=True)
class HarvestOutcome:
    run_id: int
    listing_ids: list[str]  # Carousell's order, de-duplicated across pages
    pages_fetched: int
    stop_reason: StopReason
    reused: bool  # True when an earlier run was reused and no request was made
    summary: UpsertSummary | None  # None when reused


def harvest(
    client: CarousellClient,
    connection: sqlite3.Connection,
    request: SearchRequest,
    limits: HarvestLimits,
    clock: Callable[[], datetime],
    reuse_within: timedelta | None = DEFAULT_REUSE_WINDOW,
    progress: ProgressCallback | None = None,
) -> HarvestOutcome:
    """Fetch up to `limits.max_pages` pages of `request` (or reuse a recent run) and return what the run holds.

    On a Carousell error the run is closed with stop reason `error` and the error is re-raised; listings
    written so far stay in the table (they are real data) but the run is never reused.
    """
    if limits.max_age_days is not None and request.sort != SortOrder.RECENT:
        raise ValueError("max_age_days needs sort=recent: the age cutoff is only sound when pages arrive newest first")
    request = request.model_copy(update={"page_cursor": None, "page_size": limits.page_size})
    request_hash = search_request_hash(request)
    now = clock()

    if reuse_within is not None:
        candidates = storage.find_recent_runs(connection, request_hash, now, reuse_within)
        reusable = next((run for run in candidates if run_covers(run, limits)), None)
        if reusable is not None:
            listing_ids = storage.load_run_listing_ids(connection, reusable.id)
            logger.info("reused run %d (%d listings, finished %s)", reusable.id, len(listing_ids), reusable.finished_at)
            return HarvestOutcome(
                run_id=reusable.id,
                listing_ids=listing_ids,
                pages_fetched=reusable.pages_fetched,
                stop_reason=StopReason(reusable.stop_reason or StopReason.ERROR),
                reused=True,
                summary=None,
            )

    run_id = storage.create_harvest_run(connection, request, request_hash, now, limits)
    logger.info("Harvest run %d started: %s", run_id, request.model_dump(mode="json", exclude_defaults=True))
    listing_ids: list[str] = []
    seen_ids: set[str] = set()
    pages_fetched = 0
    new_ids: list[str] = []
    changed_ids: list[str] = []
    unchanged_ids: list[str] = []
    cursor: str | None = None
    try:
        while True:
            result = client.search(request.model_copy(update={"page_cursor": cursor}))
            pages_fetched += 1
            seen_at = clock()
            page_listings: list[Listing] = []
            for listing in result.listings:  # a listing can repeat across pages and, rarely, within one page
                if listing.id not in seen_ids:
                    seen_ids.add(listing.id)
                    page_listings.append(listing)
            summary = storage.upsert_listings(connection, page_listings, seen_at)
            new_ids.extend(summary.new_ids)
            changed_ids.extend(summary.changed_ids)
            unchanged_ids.extend(summary.unchanged_ids)
            new_page_ids = [listing.id for listing in page_listings]
            storage.add_run_listings(connection, run_id, new_page_ids, len(listing_ids))
            listing_ids.extend(new_page_ids)
            if progress is not None:
                progress(pages_fetched, len(listing_ids))
            logger.info("Harvest run %d page %d: %d listings (%d new on this page)", run_id, pages_fetched, len(result.listings), len(new_page_ids))
            stop_reason = decide_stop(pages_fetched, result.listings, len(new_page_ids), result.next_page, limits, seen_at)
            if stop_reason is not None:
                break
            cursor = result.next_page
    except Exception:
        # Any failure (Carousell, SQLite, a bad clock) closes the run as an error so it never looks "running"
        # and is never reused; the listings already written stay, they are real data.
        storage.finish_harvest_run(connection, run_id, clock(), pages_fetched, len(listing_ids), StopReason.ERROR)
        logger.warning("Harvest run %d failed after %d pages", run_id, pages_fetched)
        raise

    storage.finish_harvest_run(connection, run_id, clock(), pages_fetched, len(listing_ids), stop_reason)
    logger.info("Harvest run %d finished: %d listings, %d pages, stopped: %s", run_id, len(listing_ids), pages_fetched, stop_reason)
    return HarvestOutcome(
        run_id=run_id,
        listing_ids=listing_ids,
        pages_fetched=pages_fetched,
        stop_reason=stop_reason,
        reused=False,
        summary=UpsertSummary(new_ids=new_ids, changed_ids=changed_ids, unchanged_ids=unchanged_ids),
    )


def run_covers(run: HarvestRun, requested: HarvestLimits) -> bool:
    """Whether an earlier run of the same request holds at least what `requested` would fetch. Pure.

    A run that exhausted the pages covers everything. A run that stopped on its age cutoff covers any request
    with the same or a shorter cutoff. A run that stopped at its page cap covers a request for no more pages
    than it fetched, at the same or a smaller page size.
    """
    match run.stop_reason:
        case StopReason.NO_MORE_PAGES:
            return True
        case StopReason.MAX_AGE:
            return (
                requested.max_age_days is not None
                and run.limits.max_age_days is not None
                and requested.max_age_days <= run.limits.max_age_days
            )
        case StopReason.MAX_PAGES:
            return run.pages_fetched >= requested.max_pages and run.limits.page_size >= requested.page_size
    return False


def decide_stop(
    pages_fetched: int,
    page_listings: list[Listing],
    new_ids_on_page: int,
    next_page: str | None,
    limits: HarvestLimits,
    now: datetime,
) -> StopReason | None:
    """Why to stop after this page, or None to fetch the next one. Pure.

    `pages_fetched` counts the page just fetched (1 for the first page).

    Checked in this order: no further page (or a page that added nothing new, which means Carousell is
    repeating itself), the age cutoff (the last dated listing on the page is older than `max_age_days`),
    then the page cap.
    """
    if next_page is None or new_ids_on_page == 0:
        return StopReason.NO_MORE_PAGES
    if limits.max_age_days is not None:
        dated = [listing.posted_or_bumped_at for listing in page_listings if listing.posted_or_bumped_at is not None]
        if dated and dated[-1] < now - timedelta(days=limits.max_age_days):
            return StopReason.MAX_AGE
    if pages_fetched >= limits.max_pages:
        return StopReason.MAX_PAGES
    return None
