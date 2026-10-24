"""A scripted stand-in for carousell.sg, served through httpx.MockTransport."""

import json

import httpx

from carousearch.carousell.client import CarousellClient
from carousearch.carousell.rate_limit import RateLimiter

from fixture_files import load_json_fixture

BOOTSTRAP_PATH = "/"
SEARCH_PATH = "/ds/search/cf/4.0/search/"


def bootstrap_page(csrf_token: str) -> str:
    return (
        "<html><head><title>Carousell Singapore</title></head><body>"
        f'<script type="application/json">{{"Application":{{"csrfToken":"{csrf_token}"}}}}</script>'
        "</body></html>"
    )


class FakeCarousell:
    """Serves a bootstrap page with a new CSRF token each time, then queued API responses.

    When the queue is empty, a search is answered from `page_responses` by the `session` cursor the
    client sent (None for a first page), and otherwise with the saved search fixture; a listing detail
    is answered with the saved listing-detail fixture.
    """

    def __init__(
        self,
        api_responses: list[httpx.Response] | None = None,
        page_responses: dict[str | None, httpx.Response] | None = None,
    ) -> None:
        self.api_responses = list(api_responses or [])
        self.page_responses = dict(page_responses or {})
        self.requests: list[httpx.Request] = []
        self.bootstrap_count = 0

    @property
    def api_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path != BOOTSTRAP_PATH]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == BOOTSTRAP_PATH:
            self.bootstrap_count += 1
            return httpx.Response(
                200,
                text=bootstrap_page(f"token-{self.bootstrap_count}"),
                headers={"content-type": "text/html", "set-cookie": f"_csrf=cookie-{self.bootstrap_count}; Path=/"},
            )
        if self.api_responses:
            return self.api_responses.pop(0)
        if request.url.path == SEARCH_PATH:
            cursor = json.loads(request.content or b"{}").get("session")
            if cursor in self.page_responses:
                return self.page_responses[cursor]
            return httpx.Response(200, json=load_json_fixture("search_price_condition.json"))
        if request.url.path.startswith("/ds/listing-detail/"):
            return httpx.Response(200, json=load_json_fixture("listing_detail.json"))
        return httpx.Response(404, text="not found")

    def client(self, rate_limiter: RateLimiter | None = None) -> CarousellClient:
        http_client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return CarousellClient(http_client, rate_limiter or no_wait_rate_limiter())


def no_wait_rate_limiter() -> RateLimiter:
    return RateLimiter(min_interval_seconds=0, jitter_seconds=0)
