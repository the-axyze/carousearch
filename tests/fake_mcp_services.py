"""Fakes behind the MCP server's McpServices: a Carousell source answering from the saved fixtures, and an
in-memory detail cache. Shared by the MCP server tests and the agent tests."""

import io
from datetime import UTC, datetime

from PIL import Image

from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.filters.schema import ListingDetail, SearchRequest, SearchResult
from carousearch.mcp_server import McpServices

from fixture_files import load_json_fixture

SEARCH_RESULT = parse_search_response(load_json_fixture("search_price_condition.json"))  # 5 listings
DETAIL = parse_listing_detail_response(load_json_fixture("listing_detail.json"))
PHOTO_URL = "https://media.karousell.com/media/photos/products/2026/10/2/iphone_15_pro_1790934810_9e7fb979.jpg"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color="white").save(buffer, format="JPEG")
    return buffer.getvalue()


class FakeCarousellSource:
    """Answers with the saved fixtures (or what the test passes) and records every call."""

    def __init__(
        self,
        search_result: SearchResult | Exception = SEARCH_RESULT,
        detail: ListingDetail = DETAIL,
        photo: bytes = b"",
    ) -> None:
        self.search_result = search_result
        self.detail = detail
        self.photo = photo
        self.search_requests: list[SearchRequest] = []
        self.detail_requests: list[str] = []
        self.photo_requests: list[str] = []

    def search(self, request: SearchRequest) -> SearchResult:
        self.search_requests.append(request)
        if isinstance(self.search_result, Exception):
            raise self.search_result
        return self.search_result

    def get_listing(self, listing_id: str) -> ListingDetail:
        self.detail_requests.append(listing_id)
        return self.detail

    def get_photo(self, url: str) -> bytes:
        self.photo_requests.append(url)
        return self.photo


class InMemoryDetailCache:
    def __init__(self) -> None:
        self.details: dict[str, ListingDetail] = {}

    def load_detail(self, listing_id: str, now: datetime) -> ListingDetail | None:
        return self.details.get(listing_id)

    def save_detail(self, detail: ListingDetail, fetched_at: datetime) -> None:
        self.details[detail.id] = detail


def fake_services(carousell: FakeCarousellSource) -> McpServices:
    return McpServices(carousell=carousell, detail_cache=InMemoryDetailCache(), clock=lambda: NOW)
