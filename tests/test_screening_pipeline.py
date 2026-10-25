from datetime import UTC, datetime

import pytest

from carousearch.carousell.errors import CarousellBlockedError, CarousellHTTPError
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest
from carousearch.screening.models import PriceSource, ResolvedPrice, TagCode
from carousearch.screening.pipeline import ScreeningServices, normalise_query, screen_listings
from carousearch.screening.price_resolver import PriceResolutionError

from fixture_files import load_json_fixture

NOON = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
QUERY = "iphone 15 pro 256gb"
REQUEST = SearchRequest(query=QUERY)
CATALOGUE_TITLE = "iPhone 15 / 15 Pro / 15 Pro Max all storage"
PRICE_LIST_DESCRIPTION = "iPhone 15 128GB - $579\niPhone 15 Pro 256GB - $850\niPhone 15 Pro Max 256GB - $1,050"

TEMPLATE_LISTING = parse_search_response(load_json_fixture("search_price_condition.json")).listings[0]
TEMPLATE_DETAIL = parse_listing_detail_response(load_json_fixture("listing_detail.json"))


def make_listing(listing_id: str, title: str = "iPhone 15 Pro 256GB Natural Titanium", price: float = 800) -> Listing:
    return TEMPLATE_LISTING.model_copy(update={"id": listing_id, "title": title, "price": price, "raw": {}})


def single_items(count: int, first_id: int = 100) -> list[Listing]:
    return [make_listing(str(first_id + index), price=800 + index) for index in range(count)]


class FakeDetailClient:
    def __init__(self, descriptions: dict[str, str] | None = None, errors: dict[str, Exception] | None = None) -> None:
        self.descriptions = descriptions or {}
        self.errors = errors or {}
        self.requested_ids: list[str] = []

    def get_listing(self, listing_id: str) -> ListingDetail:
        self.requested_ids.append(listing_id)
        if listing_id in self.errors:
            raise self.errors[listing_id]
        description = self.descriptions.get(listing_id, "Selling as I upgraded. No box.")
        return TEMPLATE_DETAIL.model_copy(update={"id": listing_id, "description": description, "raw": {}})


class FakeStore:
    def __init__(self) -> None:
        self.details: dict[str, ListingDetail] = {}
        self.resolved: dict[tuple[str, str], ResolvedPrice] = {}

    def load_detail(self, listing_id: str, now: datetime) -> ListingDetail | None:
        return self.details.get(listing_id)

    def save_detail(self, detail: ListingDetail, fetched_at: datetime) -> None:
        self.details[detail.id] = detail

    def load_resolved_price(self, listing_id: str, query: str) -> ResolvedPrice | None:
        return self.resolved.get((listing_id, query))

    def save_resolved_price(self, listing_id: str, query: str, resolved: ResolvedPrice, resolved_at: datetime) -> None:
        self.resolved[(listing_id, query)] = resolved


class FakeResolver:
    def __init__(self, answer: ResolvedPrice | None = None, error: Exception | None = None) -> None:
        self.answer = answer or ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="15 Pro 256GB - $850")
        self.error = error
        self.calls: list[tuple[str, str, str, float | None]] = []

    def resolve(self, query: str, title: str, description: str, listed_price: float | None) -> ResolvedPrice:
        self.calls.append((query, title, description, listed_price))
        if self.error is not None:
            raise self.error
        return self.answer


def make_services(
    client: FakeDetailClient | None = None,
    store: FakeStore | None = None,
    resolver: FakeResolver | None = None,
    max_detail_fetches: int = 10,
) -> ScreeningServices:
    return ScreeningServices(
        client=client or FakeDetailClient(),
        store=store or FakeStore(),
        resolver=resolver or FakeResolver(),
        clock=lambda: NOON,
        max_detail_fetches=max_detail_fetches,
    )


def by_id(screened: list) -> dict:
    return {item.listing.id: item for item in screened}


def test_details_are_fetched_only_for_tagged_or_below_market_listings() -> None:
    listings = [
        *single_items(5),
        make_listing("200", title=CATALOGUE_TITLE, price=579),
        make_listing("300", price=99),  # far below the page's median of about 800
    ]
    client = FakeDetailClient(descriptions={"200": PRICE_LIST_DESCRIPTION})
    services = make_services(client=client)

    screened = by_id(screen_listings(listings, REQUEST, services))

    assert client.requested_ids == ["200", "300"]
    assert [tag.code for tag in screened["300"].tags] == [TagCode.PRICE_BELOW_MARKET]
    assert screened["300"].detail is not None and not screened["300"].is_multi_item
    assert screened["100"].detail is None and screened["100"].tags == []
    assert screened["100"].resolved_price == ResolvedPrice(amount=800, source=PriceSource.LISTED)


def test_multi_item_listing_gets_its_variant_price_from_the_resolver() -> None:
    listings = [*single_items(5), make_listing("200", title=CATALOGUE_TITLE, price=579)]
    resolver = FakeResolver()
    services = make_services(client=FakeDetailClient(descriptions={"200": PRICE_LIST_DESCRIPTION}), resolver=resolver)

    screened = by_id(screen_listings(listings, REQUEST, services))["200"]

    assert resolver.calls == [(QUERY, CATALOGUE_TITLE, PRICE_LIST_DESCRIPTION, 579)]
    assert screened.is_multi_item
    assert [tag.code for tag in screened.tags] == [
        TagCode.MULTI_ITEM_TITLE,
        TagCode.PRICE_LIST_IN_DESCRIPTION,
        TagCode.LISTED_PRICE_IS_FLOOR,
    ]
    assert screened.resolved_price.amount == 850
    assert screened.summary_line() == "Multi-item · listed S$579 · your variant S$850 (description)"


def test_resolver_is_called_only_for_multi_item_listings() -> None:
    listings = [*single_items(5), make_listing("300", price=99)]
    resolver = FakeResolver()

    screened = screen_listings(listings, REQUEST, make_services(resolver=resolver))

    assert resolver.calls == []
    assert all(item.resolved_price.source == PriceSource.LISTED for item in screened)


def test_a_cheap_listing_with_a_price_list_becomes_multi_item() -> None:
    listings = [*single_items(5), make_listing("300", price=99)]
    client = FakeDetailClient(descriptions={"300": "Screen protector $99\nCase $15\nCharger $25"})
    resolver = FakeResolver()

    screened = by_id(screen_listings(listings, REQUEST, make_services(client=client, resolver=resolver)))["300"]

    assert screened.is_multi_item
    assert len(resolver.calls) == 1


def test_not_stated_adds_the_price_not_for_your_item_tag() -> None:
    listings = [make_listing("200", title=CATALOGUE_TITLE, price=579)]
    not_stated = ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only 128GB is priced")
    services = make_services(resolver=FakeResolver(answer=not_stated))

    [screened] = screen_listings(listings, REQUEST, services)

    assert screened.tags[-1].code == TagCode.PRICE_NOT_FOR_YOUR_ITEM
    assert screened.resolved_price == not_stated
    assert screened.summary_line() == "Multi-item · listed S$579 · no price stated for your variant"


def test_detail_fetches_respect_the_budget() -> None:
    listings = [make_listing(str(200 + index), title=CATALOGUE_TITLE, price=579) for index in range(15)]
    client = FakeDetailClient()

    screened = screen_listings(listings, REQUEST, make_services(client=client, max_detail_fetches=10))

    assert client.requested_ids == [str(200 + index) for index in range(10)]
    unchecked = [item for item in screened if item.detail is None]
    assert len(unchecked) == 5
    assert all(item.resolved_price.note == "description not checked" for item in unchecked)


def test_cached_details_do_not_use_the_budget() -> None:
    listings = [make_listing(str(200 + index), title=CATALOGUE_TITLE, price=579) for index in range(3)]
    store = FakeStore()
    store.details["200"] = TEMPLATE_DETAIL.model_copy(update={"id": "200", "description": PRICE_LIST_DESCRIPTION})
    client = FakeDetailClient()

    screened = screen_listings(listings, REQUEST, make_services(client=client, store=store, max_detail_fetches=1))

    assert client.requested_ids == ["201"]
    assert [item.detail is not None for item in screened] == [True, True, False]


def test_second_run_is_served_from_the_caches() -> None:
    listings = [*single_items(5), make_listing("200", title=CATALOGUE_TITLE, price=579), make_listing("300", price=99)]
    store = FakeStore()
    first_client, first_resolver = FakeDetailClient(), FakeResolver()
    first = screen_listings(listings, REQUEST, make_services(first_client, store, first_resolver))
    second_client, second_resolver = FakeDetailClient(), FakeResolver()

    same_search_typed_differently = SearchRequest(query="iPhone 15  Pro 256GB")
    second_services = make_services(second_client, store, second_resolver)
    second = screen_listings(listings, same_search_typed_differently, second_services)

    assert len(first_client.requested_ids) == 2 and len(first_resolver.calls) == 1
    assert second_client.requested_ids == []
    assert second_resolver.calls == []
    assert [item.model_dump() for item in second] == [item.model_dump() for item in first]
    assert ("200", normalise_query(QUERY)) in store.resolved


def test_listed_results_and_failures_are_not_cached() -> None:
    listings = [make_listing("200", title=CATALOGUE_TITLE, price=579)]
    store = FakeStore()
    listed = ResolvedPrice(amount=579, source=PriceSource.LISTED)

    screen_listings(listings, REQUEST, make_services(store=store, resolver=FakeResolver(answer=listed)))
    [failed] = screen_listings(
        listings, REQUEST, make_services(store=store, resolver=FakeResolver(error=PriceResolutionError("HTTP 529")))
    )

    assert store.resolved == {}
    assert failed.resolved_price == ResolvedPrice(
        amount=579, source=PriceSource.LISTED, note="price check failed: HTTP 529"
    )
    assert all(tag.code != TagCode.PRICE_NOT_FOR_YOUR_ITEM for tag in failed.tags)


def test_without_fetching_details_only_the_card_rules_run() -> None:
    listings = [*single_items(5), make_listing("200", title=CATALOGUE_TITLE, price=579)]
    client, resolver = FakeDetailClient(), FakeResolver()

    screened = by_id(screen_listings(listings, REQUEST, make_services(client, resolver=resolver), fetch_details=False))

    assert client.requested_ids == [] and resolver.calls == []
    assert screened["200"].is_multi_item
    assert screened["200"].summary_line() == "Multi-item · listed S$579 · may not be your variant's price"


def test_a_failed_detail_is_skipped_and_a_block_stops_fetching() -> None:
    listings = [make_listing(str(200 + index), title=CATALOGUE_TITLE, price=579) for index in range(4)]
    client = FakeDetailClient(
        errors={"200": CarousellHTTPError("HTTP 404", 404), "201": CarousellBlockedError("Cloudflare challenge")}
    )

    screened = screen_listings(listings, REQUEST, make_services(client=client))

    assert client.requested_ids == ["200", "201"]
    assert all(item.detail is None for item in screened)


def test_category_only_search_is_not_resolved() -> None:
    listings = [make_listing("200", title=CATALOGUE_TITLE, price=579)]
    resolver = FakeResolver()

    [screened] = screen_listings(listings, SearchRequest(category="5707"), make_services(resolver=resolver))

    assert resolver.calls == []
    assert screened.is_multi_item
    assert screened.resolved_price.note == "no search keywords"


@pytest.mark.parametrize(("query", "key"), [("iPhone  15 Pro", "iphone 15 pro"), (" R15 ", "r15")])
def test_normalise_query(query: str, key: str) -> None:
    assert normalise_query(query) == key
