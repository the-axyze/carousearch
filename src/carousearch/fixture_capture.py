"""Developer tool: re-fetch live Carousell responses and save them as test fixtures.

Run `carousearch capture-fixtures` when Carousell changes shape, then re-run `pytest` to see
which parsers need updating. About 5 requests: bootstrap, 2 searches, 1 detail, 1 challenge sample.
"""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from carousearch.carousell.client import CAROUSELL_BASE_URL, CarousellClient
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.carousell.parse import extract_initial_state, raise_if_blocked
from carousearch.carousell.rate_limit import RateLimiter
from carousearch.filters.payload import build_search_payload
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

FIXTURE_SEARCHES: dict[str, SearchRequest] = {
    "search_price_condition": SearchRequest(
        query="iphone 15",
        price_min=300,
        price_max=900,
        conditions=[Condition.LIKE_NEW, Condition.LIGHTLY_USED],
        sort=SortOrder.RECENT,
        page_size=5,
    ),
    "search_category_fields": SearchRequest(
        query="iphone 15 pro",
        category="5707",  # Mobile Phones
        category_fields={"mobile_storage": "MOBILE_STORAGE_256_GB"},
        deal_methods=[DealMethod.MEETUP],
        page_size=5,
    ),
}
DETAIL_SOURCE_SEARCH = "search_price_condition"  # the detail fixture is its first listing
LISTING_DETAIL_FILE = "listing_detail.json"
BOOTSTRAP_PAGE_FILE = "bootstrap_page.html"
CHALLENGE_FILE = "cloudflare_challenge.json"
# Cloudflare challenges obvious bot User-Agents (access notes section 2); httpx's default is one.
BOT_USER_AGENT = f"python-httpx/{httpx.__version__}"
_KEPT_CHALLENGE_HEADERS = ("content-type", "cf-mitigated", "server")


@dataclass
class CaptureReport:
    written_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def capture_fixtures(
    client: CarousellClient,
    bot_http_client: httpx.Client,
    rate_limiter: RateLimiter,
    output_dir: Path,
) -> CaptureReport:
    """Fetch every fixture and write it into `output_dir`.

    `bot_http_client` is a separate cookie-less client used to provoke a Cloudflare challenge;
    `rate_limiter` must be the one `client` uses so all requests share the same pacing.
    """
    report = CaptureReport()
    bootstrap_html = client.bootstrap()
    _write_text(output_dir / BOOTSTRAP_PAGE_FILE, trim_bootstrap_page(bootstrap_html), report)

    search_responses = {}
    for name, request in FIXTURE_SEARCHES.items():
        search_responses[name] = client.search_raw(request)
        _write_json(output_dir / f"{name}.json", search_responses[name], report)
        _write_json(output_dir / f"{name}.request.json", describe_search_request(request), report)

    first_listing_id = first_listing_id_in(search_responses[DETAIL_SOURCE_SEARCH])
    detail_response = client.get_listing_raw(first_listing_id)
    _write_json(output_dir / LISTING_DETAIL_FILE, trim_listing_detail_response(detail_response), report)

    rate_limiter.wait()
    challenge = bot_http_client.get(CAROUSELL_BASE_URL + "/search/iphone", headers={"User-Agent": BOT_USER_AGENT})
    challenge_sample = describe_response(challenge)
    if is_blocked_response(challenge_sample):
        _write_json(output_dir / CHALLENGE_FILE, challenge_sample, report)
    else:
        report.warnings.append(
            f"bot User-Agent was not challenged (HTTP {challenge.status_code}); kept the old {CHALLENGE_FILE}"
        )
    return report


def describe_search_request(request: SearchRequest) -> dict:
    """What was sent, saved next to each search fixture so tests can check the translation."""
    return {"search_request": request.model_dump(mode="json"), "payload": build_search_payload(request)}


def first_listing_id_in(search_response: Any) -> str:
    for item in search_response["data"]["results"]:
        if "listingCard" in item:
            return str(item["listingCard"]["id"])
    raise ValueError("search response has no listing cards")


def trim_bootstrap_page(page_html: str) -> str:
    """Keep only the initialState `Application` slice (holds csrfToken); the full page is ~370 KB."""
    initial_state = extract_initial_state(page_html)
    if initial_state is None:
        raise ValueError("bootstrap page has no initialState JSON")
    application_only = json.dumps({"Application": initial_state["Application"]})
    return (
        "<!DOCTYPE html><html><head><title>Carousell trimmed bootstrap page</title></head><body>"
        f'<script type="application/json">{application_only}</script>'
        "<script>window.initialState = JSON.parse(document.currentScript.previousSibling.textContent);</script>"
        "</body></html>\n"
    )


def trim_listing_detail_response(response_json: Any) -> Any:
    """Drop the screens' UI layout `groups` (~34 KB); the listing data is in `meta.default_value`."""
    trimmed = copy.deepcopy(response_json)
    for screen in trimmed.get("data", {}).get("screens", []):
        if isinstance(screen, dict) and "groups" in screen:
            screen["groups"] = []
    return trimmed


def describe_response(response: httpx.Response) -> dict:
    return {
        "status_code": response.status_code,
        "headers": {name: response.headers[name] for name in _KEPT_CHALLENGE_HEADERS if name in response.headers},
        "body": response.text,
    }


def is_blocked_response(sample: dict) -> bool:
    try:
        raise_if_blocked(sample["status_code"], sample["headers"], sample["body"])
    except CarousellBlockedError:
        return True
    return False


def _write_json(path: Path, content: Any, report: CaptureReport) -> None:
    _write_text(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n", report)


def _write_text(path: Path, content: str, report: CaptureReport) -> None:
    path.write_text(content, encoding="utf-8")
    report.written_files.append(path)
