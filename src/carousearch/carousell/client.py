"""HTTP access to carousell.sg's internal JSON endpoints. All Carousell network I/O lives here.

Session handling follows CAROUSELL_ACCESS_NOTES.md section 7.8: load one HTML page to get
cookies and the CSRF token, then send the token as the `csrf-token` header on every API call.
"""

from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from carousearch.carousell.errors import CarousellBlockedError, CarousellHTTPError, CarousellSchemaError
from carousearch.carousell.parse import (
    extract_csrf_token,
    parse_listing_detail_response,
    parse_search_response,
    raise_if_blocked,
)
from carousearch.carousell.rate_limit import RateLimiter
from carousearch.filters.payload import SINGAPORE_COUNTRY_CODE, build_search_payload
from carousearch.filters.schema import ListingDetail, SearchRequest, SearchResult

CAROUSELL_BASE_URL = "https://www.carousell.sg"
BOOTSTRAP_PATH = "/"  # the home page (~370 KB) carries the CSRF token and sets the session cookies
SEARCH_SERVICE = "/ds/search"
SEARCH_ENDPOINT = "/cf/4.0/search/"
LISTING_DETAIL_SERVICE = "/ds/listing-detail"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-SG,en;q=0.9"}
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
# No AVIF: the formats asked for are the ones model hosts accept as image input.
PHOTO_ACCEPT = "image/webp,image/jpeg,image/png,image/*;q=0.8"
# Listing photos (search-card thumbnails and the detail's image_urls) come from this host.
CAROUSELL_MEDIA_HOSTS = frozenset({"media.karousell.com"})

# 422 is what the API returns when cookies or the CSRF token are missing (access notes section 2).
SESSION_REJECTED_STATUS_CODES = frozenset({401, 403, 422})
DEFAULT_TIMEOUT_SECONDS = 20.0


class CarousellClient:
    def __init__(
        self,
        http_client: httpx.Client,
        rate_limiter: RateLimiter | None = None,
        base_url: str = CAROUSELL_BASE_URL,
    ) -> None:
        self._http_client = http_client
        self._rate_limiter = rate_limiter or RateLimiter()
        self._base_url = base_url.rstrip("/")
        self._csrf_token: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http_client.close()

    def bootstrap(self) -> str:
        """Start a fresh session: load the home page for cookies and the CSRF token. Returns the page HTML."""
        self._http_client.cookies.clear()
        self._csrf_token = None
        response = self._send("GET", self._base_url + BOOTSTRAP_PATH, headers={"Accept": HTML_ACCEPT})
        raise_if_blocked(response.status_code, response.headers, response.text)
        if response.status_code != 200:
            raise CarousellHTTPError(f"bootstrap page returned HTTP {response.status_code}", response.status_code)
        self._csrf_token = extract_csrf_token(response.text)
        return response.text

    def search(self, request: SearchRequest) -> SearchResult:
        # TODO(fallback): when this raises CarousellBlockedError or CarousellSchemaError, a later phase
        # should retry through the server-rendered HTML page (access notes section 5,
        # initialState.SearchListing.listingCards) and, failing that, through Playwright.
        return parse_search_response(self.search_raw(request))

    def search_raw(self, request: SearchRequest) -> Any:
        """The undecoded search response JSON (used to capture test fixtures)."""
        return self._call_api("POST", SEARCH_SERVICE, SEARCH_ENDPOINT, json_body=build_search_payload(request))

    def get_listing(self, listing_id: str) -> ListingDetail:
        return parse_listing_detail_response(self.get_listing_raw(listing_id))

    def get_listing_raw(self, listing_id: str) -> Any:
        """The undecoded listing-detail response JSON (used to capture test fixtures)."""
        if not listing_id.isdigit():
            raise ValueError(f"listing id must be numeric, got {listing_id!r}")
        return self._call_api(
            "GET",
            LISTING_DETAIL_SERVICE,
            f"/3.1/listings/{listing_id}/detail/",
            extra_params={"country_code": SINGAPORE_COUNTRY_CODE},
            extra_headers={"y-build-no": "1"},
        )

    def get_photo(self, url: str) -> bytes:
        """One listing photo as the bytes Carousell's media host sends. No session needed; rate-limited as usual."""
        if not is_carousell_media_url(url):
            allowed_hosts = ", ".join(sorted(CAROUSELL_MEDIA_HOSTS))
            raise ValueError(f"only https photo URLs on {allowed_hosts} can be downloaded, got {url!r}")
        response = self._send("GET", url, headers={"Accept": PHOTO_ACCEPT, "Referer": self._base_url + "/"})
        raise_if_blocked(response.status_code, response.headers, response.text)
        if response.status_code != 200:
            raise CarousellHTTPError(f"photo {url} returned HTTP {response.status_code}", response.status_code)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise CarousellSchemaError(f"photo {url} is not an image (content-type {content_type or 'missing'})")
        return response.content

    def _call_api(
        self,
        method: str,
        service: str,
        endpoint: str,
        json_body: dict | None = None,
        extra_params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Send one API request, re-bootstrapping the session once if Carousell rejects it."""
        if self._csrf_token is None:
            self.bootstrap()
        response = self._send_api_request(method, service, endpoint, json_body, extra_params, extra_headers)
        if response.status_code in SESSION_REJECTED_STATUS_CODES:
            self.bootstrap()
            response = self._send_api_request(method, service, endpoint, json_body, extra_params, extra_headers)
        return decode_api_response(response, endpoint)

    def _send_api_request(
        self,
        method: str,
        service: str,
        endpoint: str,
        json_body: dict | None,
        extra_params: dict[str, str] | None,
        extra_headers: dict[str, str] | None,
    ) -> httpx.Response:
        # The web client sends the endpoint path again as `_path` (access notes 4.1).
        params = {**(extra_params or {}), "_path": endpoint, "l": "en"}
        headers = {
            "Accept": "application/json",
            "Origin": self._base_url,
            "Referer": self._base_url + "/",
            "csrf-token": self._csrf_token or "",
            **(extra_headers or {}),
        }
        return self._send(method, self._base_url + service + endpoint, params=params, headers=headers, json=json_body)

    def _send(self, method: str, url: str, headers: dict[str, str], **request_options: Any) -> httpx.Response:
        self._rate_limiter.wait()
        try:
            return self._http_client.request(method, url, headers={**BROWSER_HEADERS, **headers}, **request_options)
        except httpx.HTTPError as error:
            raise CarousellHTTPError(f"network error calling {url}: {error}") from error


def decode_api_response(response: httpx.Response, endpoint: str) -> Any:
    """Turn a final (post-retry) API response into JSON or the matching Carousell exception."""
    raise_if_blocked(response.status_code, response.headers, response.text)
    status = response.status_code
    if status in (401, 403):
        raise CarousellBlockedError(f"HTTP {status} from {endpoint} even after re-bootstrapping the session")
    if status == 422:
        raise CarousellHTTPError(f"HTTP 422 from {endpoint}: session or CSRF token rejected after re-bootstrap", status)
    if status != 200:
        raise CarousellHTTPError(f"HTTP {status} from {endpoint}: {response.text[:200]!r}", status)
    try:
        return response.json()
    except ValueError as error:
        content_type = response.headers.get("content-type", "unknown")
        raise CarousellSchemaError(f"response from {endpoint} is not JSON (content-type {content_type})") from error


def is_carousell_media_url(url: str) -> bool:
    """True for an https URL on one of CAROUSELL_MEDIA_HOSTS; False for any other host or scheme."""
    try:
        parts = urlsplit(url)
    except ValueError:  # malformed, e.g. an unclosed IPv6 bracket
        return False
    return parts.scheme == "https" and parts.hostname in CAROUSELL_MEDIA_HOSTS


def create_carousell_client(
    rate_limiter: RateLimiter | None = None, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> CarousellClient:
    """A client for the real site with its own httpx.Client (closed by CarousellClient.close)."""
    http_client = httpx.Client(follow_redirects=True, timeout=timeout_seconds)
    return CarousellClient(http_client, rate_limiter or RateLimiter())
