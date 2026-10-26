"""The web app through FastAPI's TestClient, with fake Carousell, store and resolver. No network."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.config import ScreeningSettings
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest, SearchResult
from carousearch.screening.models import PriceSource, ResolvedPrice
from carousearch.screening.pipeline import ScreeningServices
from carousearch.web.app import WebSettings, create_app
from carousearch.web.routes import RequestAbandoned, call_in_turn
from carousearch.web.services import WebServices

from fake_carousell import FakeCarousell as FakeCarousellSite
from fixture_files import load_json_fixture

START = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEMPLATE_LISTING = parse_search_response(load_json_fixture("search_price_condition.json")).listings[0]
TEMPLATE_DETAIL = parse_listing_detail_response(load_json_fixture("listing_detail.json"))

CATALOGUE_TITLE = "iPhone 15 / 15 Pro / 15 Pro Max all storage"  # resolved from its description: S$850
MAX_ONLY_TITLE = "iPhone 15 Pro Max 256GB / 512GB / 1TB"  # the description has no price for the 15 Pro
PRICE_LIST = "iPhone 15 128GB - $579\niPhone 15 Pro 256GB - $850\niPhone 15 Pro Max 256GB - $1,050"


def make_listing(listing_id: str, title: str, price: float) -> Listing:
    return TEMPLATE_LISTING.model_copy(
        update={"id": listing_id, "title": title, "price": price, "price_display": f"S${price:,.0f}", "raw": {}}
    )


def first_page(next_page: str | None = "cursor-2") -> SearchResult:
    """A catalogue post, a Max-only catalogue post, a suspiciously cheap listing and five ordinary ones."""
    ordinary = [make_listing(str(300 + index), "iPhone 15 Pro 256GB Natural", 800 + 10 * index) for index in range(5)]
    listings = [
        make_listing("200", CATALOGUE_TITLE, 579),
        make_listing("201", MAX_ONLY_TITLE, 1199),
        make_listing("202", "iPhone 15 Pro 256GB Blue", 100),
        *ordinary,
    ]
    return SearchResult(listings=listings, total=1000, total_display="1,000+", next_page=next_page)


def second_page() -> SearchResult:
    listings = [make_listing("400", CATALOGUE_TITLE, 579), make_listing("401", "iPhone 15 Pro 256GB White", 900)]
    return SearchResult(listings=listings, total=1000, total_display="1,000+", next_page=None)


class FakeCarousell:
    """Answers searches by cursor and details from the fixture; can be told to fail."""

    def __init__(self, pages: dict[str | None, SearchResult] | None = None, error: Exception | None = None) -> None:
        self.pages = pages if pages is not None else {None: first_page(), "cursor-2": second_page()}
        self.error = error
        self.detail_error: Exception | None = None
        self.search_requests: list[SearchRequest] = []
        self.detail_requests: list[str] = []

    def search(self, request: SearchRequest) -> SearchResult:
        self.search_requests.append(request)
        if self.error is not None:
            raise self.error
        return self.pages[request.page_cursor]

    def get_listing(self, listing_id: str) -> ListingDetail:
        self.detail_requests.append(listing_id)
        if self.detail_error is not None:
            raise self.detail_error
        description = PRICE_LIST if listing_id in {"200", "201", "400"} else "Selling as I upgraded. No box."
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
    def resolve(self, query: str, title: str, description: str, listed_price: float | None) -> ResolvedPrice:
        if title == MAX_ONLY_TITLE:
            return ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only Pro Max prices are listed")
        return ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="iPhone 15 Pro 256GB - $850")


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def build_services(carousell: FakeCarousell, clock: Clock | None = None) -> WebServices:
    screening = ScreeningServices(client=carousell, store=FakeStore(), resolver=FakeResolver(), clock=clock or Clock())
    return WebServices(carousell=carousell, screening=screening, price_lookup_enabled=True)


def open_client(carousell: FakeCarousell, clock: Clock | None = None) -> TestClient:
    settings = WebSettings(data_dir=Path("unused"), screening=ScreeningSettings())
    services = build_services(carousell, clock)
    return TestClient(create_app(settings, services_factory=lambda: services))


SEARCH = "/results?q=iphone+15+pro+256gb&price_max=1200"


def card_html(page: str, listing_id: str) -> str:
    """The <article> of one listing."""
    pattern = rf'<article class="card">((?:(?!</article>).)*?/listing/{listing_id}".*?)</article>'
    match = re.search(pattern, page, re.DOTALL)
    assert match, f"no card for listing {listing_id}"
    return match.group(1)


# --- the page -----------------------------------------------------------------------------------------------------


def test_page_renders_the_search_form_without_searching() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert 'id="search-form"' in response.text
    assert 'placeholder="What are you looking for?"' in response.text
    assert 'name="hide_multi_item"' in response.text
    assert "Press <kbd>/</kbd> to start typing" in response.text
    assert carousell.search_requests == []


def test_page_with_search_parameters_includes_the_results_and_the_filled_form() -> None:
    with open_client(FakeCarousell()) as client:
        response = client.get("/?q=iphone+15+pro+256gb&condition=like_new&sort=recent")
    assert response.text.count('class="card"') == 8
    assert 'value="iphone 15 pro 256gb"' in response.text
    assert re.search(r'value="like_new"\s+checked', response.text)
    assert '<option value="recent" selected>' in response.text


def test_static_files_are_served() -> None:
    with open_client(FakeCarousell()) as client:
        assert client.get("/static/htmx.min.js").text.startswith("/* htmx 2.0.11")
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200


# --- the results partial ------------------------------------------------------------------------------------------


def test_results_show_cards_with_prices_and_badges() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        response = client.get(SEARCH)
    page = response.text
    assert response.status_code == 200
    assert page.count('class="card"') == 8
    assert "<html" not in page

    catalogue = card_html(page, "200")
    assert '<span class="price">S$850</span>' in catalogue
    assert '<s class="price-struck">S$579</s>' in catalogue
    assert "from description" in catalogue
    assert 'class="badge badge-amber"' in catalogue and "Multi-item title:" in catalogue

    max_only = card_html(page, "201")
    assert '<span class="price">S$1,199</span>' in max_only
    assert "price for your item not stated" in max_only

    cheap = card_html(page, "202")
    assert ">Low price<" in cheap and "Multi-item" not in cheap
    assert '<span class="price">S$100</span>' in cheap

    ordinary = card_html(page, "300")
    assert "badge" not in ordinary and "price-caption" not in ordinary

    assert "8 of 1,000+ results · 2 multi-item · screened 3 descriptions" in page
    assert carousell.search_requests[0].price_max == 1200


def test_results_push_the_page_url_not_their_own() -> None:
    with open_client(FakeCarousell()) as client:
        response = client.get(SEARCH + "&hide_multi_item=1&sort=bogus")
    assert response.headers["HX-Push-Url"] == "/?q=iphone+15+pro+256gb&price_max=1200&hide_multi_item=1"


def test_results_honour_hide_multi_item() -> None:
    with open_client(FakeCarousell()) as client:
        page = client.get(SEARCH + "&hide_multi_item=1").text
    assert page.count('class="card"') == 6
    assert "/listing/200" not in page and "/listing/201" not in page
    assert "6 of 1,000+ results · 2 multi-item hidden" in page


def test_load_more_button_appears_only_when_there_is_a_next_page() -> None:
    with open_client(FakeCarousell()) as client:
        page = client.get(SEARCH).text
    button = re.search(r'<button[^>]*class="load-more-button"[^>]*hx-get="([^"]+)"', page)
    assert button and "cursor=cursor-2" in button.group(1)

    last_page_only = FakeCarousell(pages={None: first_page(next_page=None)})
    with open_client(last_page_only) as client:
        page = client.get(SEARCH).text
    assert "load-more-button" not in page
    assert "8 results" in page


def test_load_more_returns_the_next_cards_and_replaces_the_line_and_button() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        response = client.get(SEARCH + "&cursor=cursor-2&shown=8&multi_item=2&hidden=0&screened=3")
    page = response.text
    assert carousell.search_requests[-1].page_cursor == "cursor-2"
    assert page.count('class="card"') == 2
    assert 'id="results-summary" class="results-summary" hx-swap-oob="true">10 results · 3 multi-item' in page
    assert 'id="load-more" class="load-more" hx-swap-oob="true"></div>' in page
    assert "HX-Push-Url" not in response.headers


def test_results_without_keywords_or_category_show_the_hint() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        response = client.get("/results?q=&price_max=100")
    assert "Press <kbd>/</kbd> to start typing" in response.text
    assert response.headers["HX-Push-Url"] == "/"
    assert carousell.search_requests == []


def test_an_empty_search_says_nothing_matches() -> None:
    nothing = FakeCarousell(pages={None: SearchResult(listings=[], total=0, total_display="0", next_page=None)})
    with open_client(nothing) as client:
        assert "No listings match" in client.get("/results?q=unobtainium").text


def test_carousells_real_zero_match_response_shows_the_empty_state_not_an_error() -> None:
    # The captured response omits `data.results`; here it goes through the real client and parser.
    no_results = httpx.Response(200, json=load_json_fixture("search_no_results.json"))
    site = FakeCarousellSite(page_responses={None: no_results})
    with site.client() as carousell, open_client(carousell) as client:
        page = client.get("/results?q=r15+bike+with+low+quality+image").text
    assert "No listings match" in page
    assert 'class="error-box"' not in page


def test_a_repeated_search_is_answered_from_memory_until_it_goes_stale() -> None:
    carousell = FakeCarousell()
    clock = Clock()
    with open_client(carousell, clock) as client:
        client.get(SEARCH)
        client.get(SEARCH + "&hide_multi_item=1")
        assert len(carousell.search_requests) == 1
        clock.now += timedelta(minutes=3)
        client.get(SEARCH)
    assert len(carousell.search_requests) == 2


# --- errors -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CarousellBlockedError("Cloudflare challenge"), "Carousell is rate-limiting us, try again in a minute."),
        (
            CarousellSchemaError("missing key 'data'"),
            "Carousell changed its response; run <code>carousearch canary</code>",
        ),
        (RuntimeError("a bug"), "Something went wrong. Try again in a moment."),
    ],
)
def test_failures_render_a_calm_error_with_status_200(error: Exception, expected: str) -> None:
    for url in (SEARCH, "/?q=iphone"):
        with open_client(FakeCarousell(error=error)) as client:
            response = client.get(url)
        assert response.status_code == 200
        assert 'class="error-box"' in response.text and expected in response.text
        assert "Traceback" not in response.text and str(error) not in response.text


def test_a_failed_load_more_offers_to_try_again_in_place() -> None:
    with open_client(FakeCarousell(error=CarousellBlockedError("429"))) as client:
        page = client.get(SEARCH + "&cursor=cursor-2&shown=8").text
    assert 'id="load-more" class="load-more" hx-swap-oob="true"' in page
    assert "rate-limiting" in page and ">Try again</button>" in page
    assert 'class="card"' not in page


# --- the detail partial -------------------------------------------------------------------------------------------


def test_detail_of_a_screened_listing_shows_description_photos_facts_and_tags() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        client.get(SEARCH)
        details_fetched = len(carousell.detail_requests)
        response = client.get("/listing/200")
    page = response.text
    assert response.status_code == 200
    assert 'class="detail-panel"' in page and "<html" not in page
    assert "iPhone 15 Pro 256GB - $850" in page
    assert page.count('<img src="https://media.karousell.com') == 7
    assert "4.95 ★ · 88 reviews" in page and "May 2019" in page and "High" in page
    assert "Punggol MRT/LRT Interchange (NE17/PTC)" in page
    assert "<strong>Multi-item title</strong>" in page and "<strong>Price list</strong>" in page
    assert len(carousell.detail_requests) == details_fetched  # the screening already had the detail


def test_detail_of_an_unscreened_listing_is_fetched_once_then_cached() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        first = client.get("/listing/987654")
        client.get("/listing/987654")
    assert carousell.detail_requests == ["987654"]
    assert "Selling as I upgraded" in first.text
    assert "No screening flags" not in first.text and "<strong>" not in first.text


def test_detail_failure_renders_the_calm_error() -> None:
    carousell = FakeCarousell()
    carousell.detail_error = CarousellBlockedError("challenge")
    with open_client(carousell) as client:
        response = client.get("/listing/987654")
    assert response.status_code == 200
    assert 'class="error-box"' in response.text and "rate-limiting" in response.text


def test_detail_needs_a_numeric_listing_id() -> None:
    carousell = FakeCarousell()
    with open_client(carousell) as client:
        assert client.get("/listing/abc").status_code == 422
    assert carousell.detail_requests == []


# --- turns --------------------------------------------------------------------------------------------------------


class DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


def test_work_for_a_browser_that_gave_up_is_skipped() -> None:
    carousell = FakeCarousell()
    services = build_services(carousell)

    async def call() -> None:
        await call_in_turn(DisconnectedRequest(), services, carousell.search, SearchRequest(query="x"))

    with pytest.raises(RequestAbandoned):
        asyncio.run(call())
    assert carousell.search_requests == []
