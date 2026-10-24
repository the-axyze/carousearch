"""Hits the real carousell.sg (4 requests + 1 bootstrap). Deselected by default; run with `pytest -m live`."""

import pytest

from carousearch.carousell.client import create_carousell_client
from carousearch.filters.schema import Condition, SearchRequest, SortOrder

pytestmark = pytest.mark.live

FILTERED_SEARCH = SearchRequest(
    query="iphone",
    price_min=200,
    price_max=900,
    conditions=[Condition.LIKE_NEW, Condition.LIGHTLY_USED],
    sort=SortOrder.PRICE_LOW_TO_HIGH,
    page_size=10,
)


def test_live_filtered_search_next_page_and_listing_detail() -> None:
    with create_carousell_client() as client:
        first_page = client.search(FILTERED_SEARCH)
        second_page = client.search(FILTERED_SEARCH.model_copy(update={"page_cursor": first_page.next_page}))
        detail = client.get_listing(first_page.listings[0].id)

    assert len(first_page.listings) >= 5
    for listing in first_page.listings + second_page.listings:
        assert FILTERED_SEARCH.price_min <= listing.price <= FILTERED_SEARCH.price_max
        # Carousell also returns old listings whose only condition is the legacy "Used".
        assert listing.condition in FILTERED_SEARCH.conditions or listing.condition_label == "Used"
    prices = [listing.price for listing in first_page.listings]
    assert prices == sorted(prices)
    assert first_page.next_page
    first_ids = {listing.id for listing in first_page.listings}
    assert second_page.listings and first_ids.isdisjoint(listing.id for listing in second_page.listings)
    assert detail.id == first_page.listings[0].id
    assert detail.image_urls and detail.seller_username
