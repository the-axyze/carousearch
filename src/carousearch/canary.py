"""Daily health check: does a known-good search (and one listing detail) still parse?

Meant to be run from cron through `carousearch canary`. Exit code 0 means Carousell access works.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from carousearch.carousell.client import CarousellClient
from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import SearchRequest

CANARY_SEARCH = SearchRequest(query="iphone", page_size=20)
DEFAULT_MINIMUM_LISTINGS = 10


@dataclass(frozen=True)
class CanaryOutcome:
    succeeded: bool
    message: str


def run_canary(client: CarousellClient, minimum_listings: int = DEFAULT_MINIMUM_LISTINGS) -> CanaryOutcome:
    """One search for CANARY_SEARCH, then a detail call for its first listing."""
    try:
        result = client.search(CANARY_SEARCH)
    except CarousellError as error:
        return CanaryOutcome(False, f"search failed: {type(error).__name__}: {error}")
    if len(result.listings) < minimum_listings:
        return CanaryOutcome(
            False, f"search for {CANARY_SEARCH.query!r} parsed {len(result.listings)} listings, expected >= {minimum_listings}"
        )
    first_listing_id = result.listings[0].id
    try:
        client.get_listing(first_listing_id)
    except CarousellError as error:
        return CanaryOutcome(False, f"listing detail {first_listing_id} failed: {type(error).__name__}: {error}")
    return CanaryOutcome(True, f"{len(result.listings)} listings parsed, detail {first_listing_id} parsed")


def format_canary_log_line(outcome: CanaryOutcome, finished_at: datetime, duration_seconds: float) -> str:
    status = "OK" if outcome.succeeded else "FAIL"
    return f"{finished_at.isoformat(timespec='seconds')} {status} {outcome.message} ({duration_seconds:.1f}s)"


def append_canary_log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")
