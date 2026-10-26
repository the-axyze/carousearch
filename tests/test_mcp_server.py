"""The MCP server, driven through the SDK's in-process client with a fake Carousell and an in-memory detail cache."""

import base64
import io
import json
from datetime import UTC, datetime

import anyio
from mcp import Client
from mcp.types import CallToolResult
from PIL import Image

from carousearch.carousell.errors import CarousellBlockedError
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.filters.schema import DealMethod, ListingDetail, SearchRequest, SearchResult, SortOrder
from carousearch.mcp_server import McpServices, build_server, load_usage_notes

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


def build_test_server(carousell: FakeCarousellSource):
    return build_server(McpServices(carousell=carousell, detail_cache=InMemoryDetailCache(), clock=lambda: NOW))


def call_tool(server, name: str, arguments: dict | None = None) -> CallToolResult:
    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(call)


def json_answer(result: CallToolResult) -> dict:
    assert result.is_error is False, result.content
    return json.loads(result.content[0].text)


def error_text(result: CallToolResult) -> str:
    assert result.is_error is True
    return result.content[0].text


def test_the_server_offers_exactly_the_five_tools_and_sends_its_usage_notes_as_instructions() -> None:
    async def connect() -> tuple[set[str], str | None]:
        async with Client(build_test_server(FakeCarousellSource())) as client:
            tools = await client.list_tools()
            return {tool.name for tool in tools.tools}, client.instructions

    tool_names, instructions = anyio.run(connect)

    assert tool_names == {"search_listings", "listing_details", "listing_photo", "screening_tags", "usage_notes"}
    assert instructions == load_usage_notes()


# --- search_listings ---------------------------------------------------------------------


def test_search_listings_passes_the_request_through_with_the_page_size_and_token() -> None:
    carousell = FakeCarousellSource()
    request = {"query": "road bike", "price_max": 800, "deal_methods": ["meetup"], "sort": "recent"}

    answer = json_answer(
        call_tool(build_test_server(carousell), "search_listings", {"request": request, "limit": 3, "page_token": "p2"})
    )

    assert carousell.search_requests == [
        SearchRequest(
            query="road bike",
            price_max=800,
            deal_methods=[DealMethod.MEETUP],
            sort=SortOrder.RECENT,
            page_size=3,
            page_cursor="p2",
        )
    ]
    assert set(answer) == {"listings", "total", "next_page"}
    assert [listing["id"] for listing in answer["listings"]] == ["1469923094", "1463757293", "1469915662"]
    assert answer["total"] == SEARCH_RESULT.total
    assert answer["next_page"] == SEARCH_RESULT.next_page


def test_search_listings_returns_listing_fields_without_carousells_raw_object() -> None:
    server = build_test_server(FakeCarousellSource())

    answer = json_answer(call_tool(server, "search_listings", {"request": {"query": "iphone 15"}}))

    first = answer["listings"][0]
    assert "raw" not in first
    assert first["title"] == "iPhone 15 256GB Blue"
    assert first["price"] == 579.0
    assert first["thumbnail_url"].startswith("https://media.karousell.com/")
    assert first["listing_url"] == "https://www.carousell.sg/p/1469923094/"


def test_search_listings_rejects_a_request_without_query_or_category_before_calling_carousell() -> None:
    carousell = FakeCarousellSource()

    result = call_tool(build_test_server(carousell), "search_listings", {"request": {"price_max": 800}})

    assert "a search needs a query, a category, or both" in error_text(result)
    assert carousell.search_requests == []


def test_a_blocked_carousell_is_reported_to_the_host_with_advice_to_stop() -> None:
    carousell = FakeCarousellSource(search_result=CarousellBlockedError("rate limited by Carousell (HTTP 429)"))

    result = call_tool(build_test_server(carousell), "search_listings", {"request": {"query": "bike"}})

    assert "blocking or rate-limiting" in error_text(result)
    assert "HTTP 429" in error_text(result)


# --- listing_details ---------------------------------------------------------------------


def test_listing_details_fetches_once_then_answers_from_the_cache() -> None:
    carousell = FakeCarousellSource()
    server = build_test_server(carousell)

    first = json_answer(call_tool(server, "listing_details", {"listing_id": DETAIL.id}))
    second = json_answer(call_tool(server, "listing_details", {"listing_id": DETAIL.id}))

    assert carousell.detail_requests == [DETAIL.id]
    assert first == second
    assert first["description"] == DETAIL.description
    assert len(first["image_urls"]) == 7
    assert "raw" not in first


def test_listing_details_rejects_an_id_that_is_not_numeric() -> None:
    carousell = FakeCarousellSource()

    result = call_tool(build_test_server(carousell), "listing_details", {"listing_id": "../search"})

    assert "listing_id" in error_text(result)
    assert carousell.detail_requests == []


# --- listing_photo -------------------------------------------------------------------------


def test_listing_photo_refuses_a_url_outside_carousells_media_host() -> None:
    carousell = FakeCarousellSource(photo=jpeg_bytes(10, 10))

    result = call_tool(build_test_server(carousell), "listing_photo", {"url": "https://example.com/photo.jpg"})

    assert "media.karousell.com" in error_text(result)
    assert carousell.photo_requests == []


def test_listing_photo_downsizes_a_1600_pixel_photo_to_800_and_returns_image_bytes() -> None:
    carousell = FakeCarousellSource(photo=jpeg_bytes(1600, 1200))

    result = call_tool(build_test_server(carousell), "listing_photo", {"url": PHOTO_URL})

    assert result.is_error is False
    (image,) = result.content
    assert (image.type, image.mime_type) == ("image", "image/jpeg")
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as photo:
        assert photo.size == (800, 600)
    assert carousell.photo_requests == [PHOTO_URL]


def test_listing_photo_honours_a_smaller_max_width() -> None:
    carousell = FakeCarousellSource(photo=jpeg_bytes(1600, 1200))

    result = call_tool(build_test_server(carousell), "listing_photo", {"url": PHOTO_URL, "max_width": 400})

    with Image.open(io.BytesIO(base64.b64decode(result.content[0].data))) as photo:
        assert photo.size == (400, 300)


def test_listing_photo_reports_bytes_that_are_not_an_image() -> None:
    carousell = FakeCarousellSource(photo=b"<html>gone</html>")

    result = call_tool(build_test_server(carousell), "listing_photo", {"url": PHOTO_URL})

    assert "could not be read" in error_text(result)


# --- screening_tags and usage_notes ---------------------------------------------------------


def test_screening_tags_returns_the_rule_tags_of_a_catalogue_post() -> None:
    catalogue = DETAIL.model_copy(update={"title": "iPhone 15 / 15 Pro / 15 Pro Max all storage"})
    carousell = FakeCarousellSource(detail=catalogue)

    answer = json_answer(call_tool(build_test_server(carousell), "screening_tags", {"listing_id": DETAIL.id}))

    assert answer["listing_id"] == DETAIL.id
    assert [tag["code"] for tag in answer["tags"]] == ["MULTI_ITEM_TITLE"]
    assert answer["tags"][0]["severity"] == "warn"
    assert "15 Pro Max" in answer["tags"][0]["reason"]


def test_screening_tags_of_a_single_item_listing_is_empty() -> None:
    server = build_test_server(FakeCarousellSource())

    answer = json_answer(call_tool(server, "screening_tags", {"listing_id": DETAIL.id}))

    assert answer == {"listing_id": DETAIL.id, "tags": []}


def test_usage_notes_returns_the_order_budget_and_shapes() -> None:
    result = call_tool(build_test_server(FakeCarousellSource()), "usage_notes")

    notes = result.content[0].text
    assert notes == load_usage_notes()
    assert "about 40 Carousell requests" in notes
    assert "## Recommended order" in notes and "## JSON shapes" in notes
