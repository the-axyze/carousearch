"""Screen a page of search results: tag multi-item posts and find the price for the searched variant.

The rules are pure (screening.rules); Carousell, the caches and the price resolver arrive in a
`ScreeningServices`, so tests pass fakes. `screen_listings` runs:

1. the title rule and the below-market rule on every listing (free, no requests);
2. for the listings step 1 tagged, the listing detail through the detail cache, at most
   `max_detail_fetches` Carousell requests per call, then the description rules;
3. for listings that are now multi-item, the price resolver through the resolution cache. A `not_stated`
   answer adds the PRICE_NOT_FOR_YOUR_ITEM tag.

Every other listing keeps its listed price.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from carousearch.carousell.errors import CarousellBlockedError, CarousellError
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest
from carousearch.screening.models import PriceSource, ResolvedPrice, ScreenedListing, Tag
from carousearch.screening.price_resolver import PriceResolutionError, PriceResolver
from carousearch.screening.rules import (
    description_tags,
    price_below_market,
    price_not_for_your_item,
    title_lists_multiple_variants,
)
from carousearch.screening.store import ScreeningStore

logger = logging.getLogger(__name__)

# Carousell detail requests one screening call may make; a page of 20 to 48 results never costs more than this.
DEFAULT_MAX_DETAIL_FETCHES = 10


class ListingDetailSource(Protocol):
    """What the pipeline needs from Carousell (CarousellClient satisfies it)."""

    def get_listing(self, listing_id: str) -> ListingDetail: ...


@dataclass(frozen=True)
class ScreeningServices:
    client: ListingDetailSource
    store: ScreeningStore
    resolver: PriceResolver
    clock: Callable[[], datetime]
    max_detail_fetches: int = DEFAULT_MAX_DETAIL_FETCHES


def screen_listings(
    listings: Sequence[Listing], request: SearchRequest, services: ScreeningServices, fetch_details: bool = True
) -> list[ScreenedListing]:
    """Screen one page of results, in their order. With `fetch_details=False` only step 1 runs (no I/O)."""
    first_pass = screen_without_details(listings, request.query)
    if not fetch_details:
        return first_pass
    details = load_details([item.listing.id for item in first_pass if item.tags], services)
    return [finish_screening(item, details.get(item.listing.id), request.query, services) for item in first_pass]


def screen_without_details(listings: Sequence[Listing], query: str | None) -> list[ScreenedListing]:
    """Step 1: the title and below-market rules. Pure, so the bot can run it on every results page."""
    return [
        ScreenedListing(listing=listing, tags=card_tags(listing, listings, query), resolved_price=listed_price(listing))
        for listing in listings
    ]


def card_tags(listing: Listing, page: Sequence[Listing], query: str | None) -> list[Tag]:
    """The tags that need only the search card: the title rule, and the price against the rest of the page."""
    peer_prices = [other.price for other in page if other.id != listing.id]
    tags = [title_lists_multiple_variants(listing.title, query), price_below_market(listing.price, peer_prices)]
    return [tag for tag in tags if tag is not None]


def load_details(listing_ids: Sequence[str], services: ScreeningServices) -> dict[str, ListingDetail]:
    """Details for these listings: cached ones first, then Carousell for at most `max_detail_fetches` of the rest."""
    now = services.clock()
    details: dict[str, ListingDetail] = {}
    fetches_left = services.max_detail_fetches
    for listing_id in listing_ids:
        cached = services.store.load_detail(listing_id, now)
        if cached is not None:
            details[listing_id] = cached
            continue
        if fetches_left <= 0:
            logger.info("Detail budget used up; listing %s is screened without its description", listing_id)
            continue
        fetches_left -= 1
        try:
            detail = services.client.get_listing(listing_id)
        except CarousellBlockedError as error:
            logger.warning("Carousell blocked the detail of %s; fetching no more details: %s", listing_id, error)
            fetches_left = 0
            continue
        except CarousellError as error:
            logger.warning("Could not fetch the detail of %s: %s", listing_id, error)
            continue
        logger.info("Fetched the detail of %s for screening", listing_id)
        services.store.save_detail(detail, now)
        details[listing_id] = detail
    return details


def finish_screening(
    item: ScreenedListing, detail: ListingDetail | None, query: str | None, services: ScreeningServices
) -> ScreenedListing:
    """Steps 2 and 3 for one listing: description rules, then the price for the searched variant."""
    if detail is None:
        if not item.is_multi_item:
            return item
        return item.model_copy(update={"resolved_price": listed_price(item.listing, "description not checked")})
    tags = [*item.tags, *description_tags(item.listing.price, detail.description or "")]
    screened = item.model_copy(update={"detail": detail, "tags": tags})
    if not screened.is_multi_item:
        return screened
    if not query:
        return screened.model_copy(update={"resolved_price": listed_price(item.listing, "no search keywords")})
    resolved = resolve_price(item.listing, detail, query, services)
    not_for_your_item = price_not_for_your_item(resolved, query)
    if not_for_your_item is not None:
        tags = [*tags, not_for_your_item]
    return screened.model_copy(update={"tags": tags, "resolved_price": resolved})


def resolve_price(listing: Listing, detail: ListingDetail, query: str, services: ScreeningServices) -> ResolvedPrice:
    """The resolver's answer, from the cache when this listing was resolved for the same search before.

    Only answers read from the description are cached. A `listed` result (no resolver configured) or a failed
    call is not, so adding an API key later, or a retry, still resolves the listing.
    """
    query_key = normalise_query(query)
    cached = services.store.load_resolved_price(listing.id, query_key)
    if cached is not None:
        return cached
    try:
        resolved = services.resolver.resolve(query, listing.title, detail.description or "", listing.price)
    except PriceResolutionError as error:
        logger.warning("Price resolution failed for %s: %s", listing.id, error)
        return listed_price(listing, f"price check failed: {error}")
    if resolved.source != PriceSource.LISTED:
        services.store.save_resolved_price(listing.id, query_key, resolved, services.clock())
    return resolved


def listed_price(listing: Listing, note: str | None = None) -> ResolvedPrice:
    return ResolvedPrice(amount=listing.price, source=PriceSource.LISTED, note=note)


def normalise_query(query: str) -> str:
    """The cache key for a search: lowercase, single spaces ("iPhone  15 Pro" and "iphone 15 pro" match)."""
    return " ".join(query.lower().split())
