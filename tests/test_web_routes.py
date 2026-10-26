"""The web app through FastAPI's TestClient, with fake Carousell, store and resolver. No network."""

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.function import FunctionModel

from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.config import ScreeningSettings
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest, SearchResult
from carousearch.screening.models import PriceSource, ResolvedPrice
from carousearch.screening.pipeline import ScreeningServices
from carousearch.web.app import WebSettings, create_app
from carousearch.web.routes import RequestAbandoned, call_in_turn
from carousearch.web.services import WebServices

from fake_mcp_services import jpeg_bytes
from fixture_files import load_json_fixture
from scripted_model import ScriptedModel, calls, final_answer

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
        self.photo_requests: list[str] = []

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

    def get_photo(self, url: str) -> bytes:
        self.photo_requests.append(url)
        return jpeg_bytes(400, 300)


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


def build_services(
    carousell: FakeCarousell,
    clock: Clock | None = None,
    agent: AgentSettings | None = None,
    agent_model: Model | None = None,
) -> WebServices:
    screening = ScreeningServices(client=carousell, store=FakeStore(), resolver=FakeResolver(), clock=clock or Clock())
    return WebServices(
        carousell=carousell,
        screening=screening,
        price_lookup_enabled=True,
        agent=agent or AgentSettings(),
        agent_model=agent_model,
    )


def open_client(
    carousell: FakeCarousell,
    clock: Clock | None = None,
    agent: AgentSettings | None = None,
    agent_model: Model | None = None,
) -> TestClient:
    settings = WebSettings(data_dir=Path("unused"), screening=ScreeningSettings())
    services = build_services(carousell, clock, agent, agent_model)
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


# --- Ask mode -----------------------------------------------------------------------------------------------------
#
# A scripted FunctionModel drives the real agent loop and the real MCP tools over the fake Carousell above. The
# provider key in KEYED is a dummy: the injected agent_model means no provider is ever built or called with it.

KEYED = AgentSettings(anthropic_api_key="test-key")
ASK_PROMPT = "A used iPhone 15 Pro 256GB under S$900, one phone per post"
POLL_TIMEOUT_SECONDS = 10.0
AGENT_SEARCH = ("search_listings", {"request": {"query": "iphone 15 pro 256gb"}, "limit": 8})
SEEN_KEPT = {
    "listing_id": "300", "title": "the model's own title", "price": 800.0, "url": "https://www.carousell.sg/p/300/",
    "seller": "seller", "reason": "One phone, 256GB, within budget",
}
UNSEEN_KEPT = {
    "listing_id": "999", "title": "A phone no search returned", "price": 650.0,
    "url": "https://www.carousell.sg/p/999/", "seller": "ghost", "reason": "Mentioned by the seller elsewhere",
}
DROPPED_ROWS = [
    {"listing_id": "200", "title": CATALOGUE_TITLE, "reason": "a catalogue of several models"},
    {"listing_id": "201", "title": MAX_ONLY_TITLE, "reason": "a Pro Max, not a Pro"},
]


class GatedCarousell(FakeCarousell):
    """Fails any call that starts while another is in progress, and can hold searches until the test lets go."""

    def __init__(self, search_error: Exception | None = None) -> None:
        super().__init__()
        self.search_error = search_error
        self.gate: threading.Event | None = None  # when set up, a search waits for it
        self.search_entered = threading.Event()
        self.in_call = False
        self.overlaps = 0

    def search(self, request: SearchRequest) -> SearchResult:
        with self.one_call_at_a_time():
            self.search_entered.set()
            if self.gate is not None:
                assert self.gate.wait(POLL_TIMEOUT_SECONDS), "the test never released the search"
            if self.search_error is not None:
                self.search_requests.append(request)
                raise self.search_error
            return super().search(request)

    def get_listing(self, listing_id: str) -> ListingDetail:
        with self.one_call_at_a_time():
            return super().get_listing(listing_id)

    @contextmanager
    def one_call_at_a_time(self) -> Iterator[None]:
        if self.in_call:
            self.overlaps += 1
            raise AssertionError("two Carousell calls overlapped")
        self.in_call = True
        try:
            yield
        finally:
            self.in_call = False


def open_ask_client(
    script: ScriptedModel, carousell: FakeCarousell | None = None, agent: AgentSettings = KEYED
) -> TestClient:
    return open_client(carousell or GatedCarousell(), agent=agent, agent_model=FunctionModel(script))


def post_ask(client: TestClient, prompt: str = ASK_PROMPT) -> Response:
    """As the page's HTMX form sends it."""
    return client.post("/ask", data={"prompt": prompt}, headers={"HX-Request": "true"})


def run_id_of(progress_html: str) -> str:
    match = re.search(r'hx-get="/ask/([A-Za-z0-9_-]+)"', progress_html)
    assert match, "no polling attribute in the progress partial"
    return match.group(1)


def poll_until(client: TestClient, run_id: str, done: Callable[[str], bool]) -> str:
    """GET /ask/{run_id} until `done(page)`, as the page's polling would, for at most POLL_TIMEOUT_SECONDS."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        page = client.get(f"/ask/{run_id}").text
        if done(page) or time.monotonic() > deadline:
            return page
        time.sleep(0.02)


def poll_to_the_end(client: TestClient, run_id: str) -> str:
    page = poll_until(client, run_id, lambda text: 'hx-trigger="every 1s"' not in text)
    assert 'hx-trigger="every 1s"' not in page, "the run did not finish in time"
    return page


def ask_card_html(page: str, listing_id: str) -> str:
    """The <article> of one kept listing in an Ask answer."""
    pattern = rf'<article class="card ask-card">((?:(?!</article>).)*?/listing/{listing_id}".*?)</article>'
    match = re.search(pattern, page, re.DOTALL)
    assert match, f"no kept card for listing {listing_id}"
    return match.group(1)


def services_of(client: TestClient) -> WebServices:
    return client.app.state.services


def test_ask_runs_in_the_background_and_answers_with_carousells_cards_dropped_notes_and_trace() -> None:
    carousell = GatedCarousell()
    script = ScriptedModel(
        calls(AGENT_SEARCH),
        final_answer("Checked one page only.", [SEEN_KEPT, UNSEEN_KEPT], DROPPED_ROWS),
    )
    with open_ask_client(script, carousell) as client:
        started = post_ask(client)
        run_id = run_id_of(started.text)
        page = poll_to_the_end(client, run_id)

    assert started.status_code == 200
    assert 'id="ask-progress"' in started.text
    assert 'hx-trigger="every 1s"' in started.text and 'hx-swap="outerHTML"' in started.text
    assert ASK_PROMPT in started.text

    assert "hx-get" not in page.replace('hx-get="/listing/', "")  # nothing polls any more
    kept = ask_card_html(page, "300")
    assert TEMPLATE_LISTING.thumbnail_url in kept
    assert '<span class="price">S$800</span>' in kept
    assert "iPhone 15 Pro 256GB Natural" in kept and "the model's own title" not in kept  # Carousell's title
    assert "One phone, 256GB, within budget" in kept
    assert "Not in any search result" not in kept
    unseen = ask_card_html(page, "999")
    assert "Not in any search result" in unseen and "A phone no search returned" in unseen and "S$650" in unseen

    assert "<summary>Dropped (2)</summary>" in page
    for dropped in DROPPED_ROWS:
        assert dropped["title"] in page and dropped["reason"] in page
    assert "<li>Checked one page only.</li>" in page
    assert "<summary>Tool calls (1)</summary>" in page
    assert re.search(r'trace-row trace-ok">\s*<span class="trace-status">ok</span>\s*'
                     r'<span class="trace-call"><code>search_listings</code>', page)
    assert "2 kept · 2 dropped · 1 tool call" in page
    assert [request.query for request in carousell.search_requests] == ["iphone 15 pro 256gb"]


def test_polling_shows_a_tool_call_while_it_runs_with_the_budget_used() -> None:
    carousell = GatedCarousell()
    carousell.gate = threading.Event()
    script = ScriptedModel(calls(AGENT_SEARCH), final_answer("Done."))
    with open_ask_client(script, carousell) as client:
        try:
            run_id = run_id_of(post_ask(client).text)
            while_blocked = poll_until(client, run_id, lambda text: "search_listings" in text)
            assert services_of(client).prompt_runs.active() is not None
        finally:
            carousell.gate.set()
        page = poll_to_the_end(client, run_id)

    assert 'hx-trigger="every 1s"' in while_blocked
    assert 'trace-row trace-running' in while_blocked and "<code>search_listings</code>" in while_blocked
    assert "calls 1 of 40 · photos 0 of 15 · listings 8 of 200" in while_blocked
    assert "Running ·" in while_blocked
    assert "<li>Done.</li>" in page and "trace-ok" in page


def test_a_budget_refusal_shows_as_refused_in_the_trace_and_the_notes() -> None:
    photo_call = ("listing_photo", {"url": TEMPLATE_LISTING.thumbnail_url})
    script = ScriptedModel(calls(photo_call), final_answer("No photos allowed."))
    agent = AgentSettings(anthropic_api_key="test-key", budget=AgentBudget(max_photos=0))
    carousell = GatedCarousell()
    with open_ask_client(script, carousell, agent) as client:
        page = poll_to_the_end(client, run_id_of(post_ask(client).text))

    assert re.search(r'trace-row trace-refused">\s*<span class="trace-status">refused</span>', page)
    assert "<li>Photo budget reached: 0 photos judged, 1 more refused.</li>" in page
    assert carousell.photo_requests == []


def test_without_the_provider_key_run_is_disabled_and_nothing_is_called() -> None:
    carousell = GatedCarousell()
    script = ScriptedModel(final_answer())
    with open_ask_client(script, carousell, agent=AgentSettings()) as client:
        page = client.get("/?mode=ask").text
        refused = post_ask(client)
        runs = services_of(client).prompt_runs

    assert re.search(r'<button id="ask-run-button"[^>]*disabled>Run</button>', page)
    assert "ANTHROPIC_API_KEY is not set" in page and "Add it to .env and restart <code>carousearch web</code>" in page
    assert refused.status_code == 409
    assert "ANTHROPIC_API_KEY is not set" in refused.text
    assert (script.requests, carousell.search_requests, runs.active()) == ([], [], None)


def test_a_filter_search_during_a_run_waits_and_never_overlaps_an_agent_tool_call() -> None:
    carousell = GatedCarousell()
    carousell.gate = threading.Event()
    script = ScriptedModel(calls(AGENT_SEARCH), final_answer("Done."))
    with open_ask_client(script, carousell) as client:
        try:
            run_id = run_id_of(post_ask(client).text)
            assert carousell.search_entered.wait(POLL_TIMEOUT_SECONDS)
            waiting = client.get(SEARCH)
            waiting_page = client.get("/?q=iphone+15+pro+256gb")
            waiting_detail = client.get("/listing/300")
            ask_page = client.get("/?mode=ask")
            searches_while_running = len(carousell.search_requests)
        finally:
            carousell.gate.set()
        poll_to_the_end(client, run_id)
        carousell.gate = None
        results = client.get(SEARCH, headers={"HX-Trigger": "ask-waiting-results"})

    assert searches_while_running == 0  # the agent's own search is still held at the gate
    assert waiting.status_code == 200
    assert "An Ask run is in progress; your search will run when it finishes." in waiting.text
    assert f'hx-get="{SEARCH}"' in waiting.text.replace("&amp;", "&")
    assert 'hx-trigger="every 2s"' in waiting.text and 'class="card"' not in waiting.text
    assert "An Ask run is in progress" in waiting_page.text and 'class="card"' not in waiting_page.text
    assert "these details will load when it finishes" in waiting_detail.text
    assert 'id="ask-progress"' in ask_page.text  # the page shows the run in flight
    assert results.text.count('class="card"') == 8 and "HX-Push-Url" not in results.headers
    assert len(carousell.search_requests) == 2
    assert carousell.overlaps == 0


def test_a_second_run_is_refused_while_one_is_running() -> None:
    carousell = GatedCarousell()
    carousell.gate = threading.Event()
    script = ScriptedModel(calls(AGENT_SEARCH), final_answer())
    with open_ask_client(script, carousell) as client:
        try:
            run_id = run_id_of(post_ask(client).text)
            assert carousell.search_entered.wait(POLL_TIMEOUT_SECONDS)
            second = post_ask(client, "Something else entirely")
            active = services_of(client).prompt_runs.active()
        finally:
            carousell.gate.set()
        poll_to_the_end(client, run_id)

    assert second.status_code == 409
    assert "An Ask run is already in progress" in second.text
    assert run_id_of(second.text) == run_id  # the first run's progress goes on showing
    assert active is not None and active.run_id == run_id
    assert len(script.requests) == 2  # one search turn and one answer: nothing for the second prompt


def test_no_get_request_starts_a_run() -> None:
    carousell = GatedCarousell()
    script = ScriptedModel(final_answer())
    with open_ask_client(script, carousell) as client:
        unknown = client.get("/ask/unknown")
        page = client.get("/?mode=ask&prompt=find+a+bike&q=bike")
        results = client.get("/results?mode=ask&prompt=find+a+bike")
        runs = services_of(client).prompt_runs

    assert unknown.status_code == 404 and "no longer here" in unknown.text
    assert page.status_code == 200 and results.status_code == 200
    assert (script.requests, carousell.search_requests, runs.active()) == ([], [], None)


@pytest.mark.parametrize(
    "error", [ModelHTTPError(status_code=401, model_name="claude-haiku-5-5", body="invalid x-api-key"),
              RuntimeError("a bug in the model adapter")],
)
def test_a_model_failure_ends_the_run_calmly_and_the_next_run_works(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    attempts = []

    def fails_once(messages, info):
        attempts.append(1)
        raise error

    script = ScriptedModel(fails_once, final_answer("Second time lucky."))
    with caplog.at_level(logging.WARNING, logger="carousearch.web.prompt_runs"), open_ask_client(script) as client:
        failed = poll_to_the_end(client, run_id_of(post_ask(client).text))
        retried = poll_to_the_end(client, run_id_of(post_ask(client).text))

    assert "The agent run failed. The terminal running carousearch web has the details." in failed
    assert "x-api-key" not in failed and "a bug" not in failed and "Traceback" not in failed
    assert any("failed" in record.getMessage() for record in caplog.records)
    assert "<li>Second time lucky.</li>" in retried


def test_a_carousell_block_during_a_run_fails_the_search_and_the_model_still_answers() -> None:
    carousell = GatedCarousell(search_error=CarousellBlockedError("rate limited by Carousell (HTTP 429)"))
    script = ScriptedModel(calls(AGENT_SEARCH), final_answer("Carousell is blocking requests."))
    with open_ask_client(script, carousell) as client:
        page = poll_to_the_end(client, run_id_of(post_ask(client).text))

    assert re.search(r'trace-row trace-failed">\s*<span class="trace-status">failed</span>', page)
    assert "<li>Carousell is blocking requests.</li>" in page
    assert "0 kept · 0 dropped · 1 tool call" in page


@pytest.mark.parametrize(("prompt", "expected"), [("   ", "Type what you are looking for"), ("x" * 2001, "2,000 characters")])
def test_an_empty_or_too_long_prompt_is_refused_without_a_run(prompt: str, expected: str) -> None:
    script = ScriptedModel(final_answer())
    with open_ask_client(script) as client:
        response = post_ask(client, prompt)
    assert response.status_code == 422 and expected in response.text
    assert script.requests == []


def test_a_post_not_sent_by_the_page_is_refused() -> None:
    script = ScriptedModel(final_answer())
    with open_ask_client(script) as client:
        response = client.post("/ask", data={"prompt": ASK_PROMPT})
    assert response.status_code == 403
    assert script.requests == []


def test_the_page_offers_both_modes_and_opens_the_one_the_url_asks_for() -> None:
    with open_ask_client(ScriptedModel(final_answer())) as client:
        filters = client.get("/").text
        ask = client.get("/?mode=ask").text

    for page in (filters, ask):
        assert 'id="search-form"' in page and 'id="ask-form"' in page
        assert 'hx-post="/ask" hx-target="#ask-run" hx-swap="innerHTML"' in page
        assert '<textarea id="prompt" class="ask-input" name="prompt"' in page
        assert "Model: anthropic:claude-haiku-5-5, from AGENT_MODEL" in page
        assert "test-key" not in page
        assert not re.search(r'<button id="ask-run-button"[^>]*disabled', page)
    assert re.search(r'id="mode-filters"[^>]*checked', filters) and not re.search(r'id="mode-ask"[^>]*checked', filters)
    assert re.search(r'id="mode-ask"[^>]*checked', ask)


def test_stopping_the_app_mid_run_stops_the_run_before_the_client_closes() -> None:
    carousell = GatedCarousell()
    carousell.gate = threading.Event()
    closed_with_a_call_in_flight = []
    script = ScriptedModel(calls(AGENT_SEARCH), final_answer())
    with open_ask_client(script, carousell) as client:
        services = services_of(client)
        services.close = lambda: closed_with_a_call_in_flight.append(carousell.in_call)
        run_id = run_id_of(post_ask(client).text)
        assert carousell.search_entered.wait(POLL_TIMEOUT_SECONDS)
        threading.Timer(0.2, carousell.gate.set).start()  # the tool call in its worker thread ends on its own

    run = services.prompt_runs.get(run_id)
    assert run is not None and run.status == "failed"
    assert run.error is not None and "shut down" in run.error.text
    assert closed_with_a_call_in_flight == [False]
