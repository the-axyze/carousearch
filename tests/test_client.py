import json

import httpx
import pytest

from carousearch.carousell.client import BROWSER_USER_AGENT, CarousellClient, is_carousell_media_url
from carousearch.carousell.errors import CarousellBlockedError, CarousellHTTPError, CarousellSchemaError
from carousearch.carousell.rate_limit import RateLimiter
from carousearch.filters.payload import build_search_payload
from carousearch.filters.schema import SearchRequest

from fake_carousell import BOOTSTRAP_PATH, SEARCH_PATH, FakeCarousell, no_wait_rate_limiter
from fixture_files import load_json_fixture

REQUEST = SearchRequest(query="iphone 15", price_max=900)


def forbidden() -> httpx.Response:
    return httpx.Response(403, json={"error": "forbidden"})


def cloudflare_challenge() -> httpx.Response:
    challenge = load_json_fixture("cloudflare_challenge.json")
    return httpx.Response(challenge["status_code"], headers=challenge["headers"], text=challenge["body"])


def test_first_call_bootstraps_then_sends_csrf_token_cookies_and_browser_user_agent() -> None:
    carousell = FakeCarousell()

    result = carousell.client().search(REQUEST)

    assert len(result.listings) > 0
    bootstrap_request, search_request = carousell.requests
    assert bootstrap_request.method == "GET" and bootstrap_request.url.path == BOOTSTRAP_PATH
    assert search_request.method == "POST" and search_request.url.path == SEARCH_PATH
    assert search_request.headers["csrf-token"] == "token-1"
    assert "_csrf=cookie-1" in search_request.headers["cookie"]
    assert search_request.url.params["_path"] == "/cf/4.0/search/"
    assert json.loads(search_request.content) == build_search_payload(REQUEST)
    for request in carousell.requests:
        assert request.headers["user-agent"] == BROWSER_USER_AGENT


def test_session_is_reused_across_calls() -> None:
    carousell = FakeCarousell()
    client = carousell.client()

    client.search(REQUEST)
    client.search(REQUEST)

    assert carousell.bootstrap_count == 1
    assert len(carousell.api_requests) == 2


def test_rejected_session_is_rebootstrapped_once_and_the_call_retried() -> None:
    carousell = FakeCarousell(api_responses=[forbidden()])

    result = carousell.client().search(REQUEST)

    assert len(result.listings) > 0
    assert carousell.bootstrap_count == 2
    first_try, retry = carousell.api_requests
    assert first_try.headers["csrf-token"] == "token-1"
    assert retry.headers["csrf-token"] == "token-2"
    assert "_csrf=cookie-2" in retry.headers["cookie"]
    assert "cookie-1" not in retry.headers["cookie"]


def test_missing_session_status_422_also_triggers_rebootstrap() -> None:
    carousell = FakeCarousell(api_responses=[httpx.Response(422, text="")])

    carousell.client().search(REQUEST)

    assert carousell.bootstrap_count == 2


def test_forbidden_twice_gives_up_with_blocked_error() -> None:
    carousell = FakeCarousell(api_responses=[forbidden(), forbidden()])

    with pytest.raises(CarousellBlockedError, match="even after re-bootstrapping"):
        carousell.client().search(REQUEST)

    assert carousell.bootstrap_count == 2
    assert len(carousell.api_requests) == 2


def test_cloudflare_challenge_on_api_raises_blocked_error() -> None:
    carousell = FakeCarousell(api_responses=[cloudflare_challenge(), cloudflare_challenge()])

    with pytest.raises(CarousellBlockedError, match="Cloudflare challenge"):
        carousell.client().search(REQUEST)


def test_challenged_bootstrap_raises_blocked_error_without_calling_the_api() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return cloudflare_challenge()

    client = CarousellClient(httpx.Client(transport=httpx.MockTransport(handle)), RateLimiter(0, 0))

    with pytest.raises(CarousellBlockedError):
        client.search(REQUEST)


def test_server_error_raises_http_error_without_rebootstrap() -> None:
    carousell = FakeCarousell(api_responses=[httpx.Response(500, text="oops")])

    with pytest.raises(CarousellHTTPError) as error_info:
        carousell.client().search(REQUEST)

    assert error_info.value.status_code == 500
    assert carousell.bootstrap_count == 1


def test_non_json_success_raises_schema_error() -> None:
    carousell = FakeCarousell(api_responses=[httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})])

    with pytest.raises(CarousellSchemaError, match="not JSON"):
        carousell.client().search(REQUEST)


def test_network_failure_raises_http_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = CarousellClient(httpx.Client(transport=httpx.MockTransport(handle)), RateLimiter(0, 0))

    with pytest.raises(CarousellHTTPError, match="network error"):
        client.search(REQUEST)


def test_get_listing_calls_detail_endpoint() -> None:
    carousell = FakeCarousell()

    detail = carousell.client().get_listing("1469923094")

    detail_request = carousell.api_requests[0]
    assert detail_request.url.path == "/ds/listing-detail/3.1/listings/1469923094/detail/"
    assert detail_request.url.params["country_code"] == "SG"
    assert detail_request.url.params["_path"] == "/3.1/listings/1469923094/detail/"
    assert detail_request.headers["y-build-no"] == "1"
    assert detail_request.headers["csrf-token"] == "token-1"
    assert detail.description is not None


def test_listing_id_must_be_numeric() -> None:
    with pytest.raises(ValueError, match="numeric"):
        FakeCarousell().client().get_listing("../search")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_every_request_waits_for_the_rate_limiter() -> None:
    clock = FakeClock()
    jitter_ranges = []

    def fixed_jitter(low: float, high: float) -> float:
        jitter_ranges.append((low, high))
        return 0.2

    limiter = RateLimiter(1.0, 0.3, clock=clock.time, sleep=clock.sleep, random_uniform=fixed_jitter)
    carousell = FakeCarousell()
    client = carousell.client(limiter)

    client.search(REQUEST)  # bootstrap + search
    client.get_listing("1469923094")

    assert len(carousell.requests) == 3
    assert clock.sleeps == pytest.approx([1.2, 1.2])
    assert jitter_ranges == [(-0.3, 0.3), (-0.3, 0.3)]


# --- photos ------------------------------------------------------------------------

PHOTO_URL = "https://media.karousell.com/media/photos/products/2026/10/2/iphone_15_pro_1790934810_9e7fb979_thumbnail.jpg"


def photo_host(response: httpx.Response) -> tuple[CarousellClient, list[httpx.Request]]:
    """A client whose every request is answered with `response`, and the list of requests it sent."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    return CarousellClient(httpx.Client(transport=httpx.MockTransport(handle)), no_wait_rate_limiter()), requests


def test_get_photo_downloads_the_photo_without_a_session_bootstrap() -> None:
    client, requests = photo_host(httpx.Response(200, content=b"jpeg bytes", headers={"content-type": "image/jpeg"}))

    assert client.get_photo(PHOTO_URL) == b"jpeg bytes"
    assert [str(request.url) for request in requests] == [PHOTO_URL]
    assert requests[0].headers["user-agent"] == BROWSER_USER_AGENT


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/photo.jpg",
        "http://media.karousell.com/media/photo.jpg",
        "https://media.karousell.com.evil.example/photo.jpg",
        "https://media.karousell.com@evil.example/photo.jpg",
        "https://www.carousell.sg/",
        "file:///etc/passwd",
        "https://[media.karousell.com/photo.jpg",
    ],
)
def test_only_https_urls_on_the_media_host_are_carousell_photos(url: str) -> None:
    assert is_carousell_media_url(url) is False


def test_get_photo_refuses_other_hosts_without_sending_a_request() -> None:
    client, requests = photo_host(httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"}))

    with pytest.raises(ValueError, match="media.karousell.com"):
        client.get_photo("https://evil.example/photo.jpg")
    assert requests == []


def test_get_photo_error_status_raises_http_error() -> None:
    client, _ = photo_host(httpx.Response(404, text="not found"))

    with pytest.raises(CarousellHTTPError, match="HTTP 404"):
        client.get_photo(PHOTO_URL)


def test_get_photo_that_is_not_an_image_raises_schema_error() -> None:
    client, _ = photo_host(httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}))

    with pytest.raises(CarousellSchemaError, match="not an image"):
        client.get_photo(PHOTO_URL)


def test_get_photo_rate_limited_by_carousell_raises_blocked_error() -> None:
    client, _ = photo_host(httpx.Response(429))

    with pytest.raises(CarousellBlockedError):
        client.get_photo(PHOTO_URL)
