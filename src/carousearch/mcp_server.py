"""A thin MCP server: lets an agent host (Claude Code, Codex CLI, the Claude desktop app, ChatGPT) search Carousell
through carousearch's client.

The host does all the reasoning: it filters and ranks the JSON these tools return, looks at the photos and plans its
own calls. This module only connects; each tool is one thin call into existing code:

- search_listings: CarousellClient.search
- listing_details: CarousellClient.get_listing through the 12-hour listing_detail_cache
- listing_photo: CarousellClient.get_photo, downsized by carousearch.photos
- screening_tags: the title and description rules of carousearch.screening.rules, for one listing
- usage_notes: the recommended order, the request budget and the JSON shapes (mcp_instructions.md)

`build_server(services)` wires the tools to injected services, so tests pass fakes and importing this module touches
nothing. The MCP SDK runs synchronous tools in worker threads; `McpServices.lock` keeps them to one Carousell or
database call at a time.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from carousearch.carousell.client import CAROUSELL_MEDIA_HOSTS, is_carousell_media_url
from carousearch.carousell.errors import CarousellBlockedError, CarousellError
from carousearch.config import DEFAULT_MCP_HTTP_PORT
from carousearch.filters.schema import MAX_PAGE_SIZE, ListingDetail, SearchRequest, SearchResult
from carousearch.photos import Photo, downsize_photo
from carousearch.screening.models import Tag
from carousearch.screening.rules import description_tags, title_lists_multiple_variants

SERVER_NAME = "carousearch"
USAGE_NOTES_FILE = Path(__file__).with_name("mcp_instructions.md")
McpTransport = Literal["stdio", "http"]
HTTP_HOST = "127.0.0.1"  # this machine only; a tunnel reaches it from outside (docs/MCP.md)
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_PHOTO_MAX_WIDTH = 800
# Every tool only reads, from a site outside this machine. Hosts such as ChatGPT skip their confirmation prompt
# for read-only tools.
READ_ONLY_TOOL = ToolAnnotations(read_only_hint=True, open_world_hint=True)

# Tool parameters. Their descriptions and limits are part of the schema the host model reads.
PageLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Listings to return, 1 to 48.")]
PageToken = Annotated[
    str | None,
    Field(description="The next_page of the previous search_listings result for the same request; omit for page 1."),
]
ListingId = Annotated[str, Field(pattern=r"^\d+$", description="A listing's numeric id: the id of a search result.")]
PhotoUrl = Annotated[
    str, Field(description="A listing's thumbnail_url, or an entry of image_urls, on https://media.karousell.com.")
]
PhotoMaxWidth = Annotated[int, Field(ge=100, le=2000, description="Photos wider than this many pixels are scaled down.")]
SearchKeywords = Annotated[
    str | None, Field(description="The search keywords, if any. They help the title rule tell variants apart.")
]


class CarousellSource(Protocol):
    """What the server needs from Carousell (CarousellClient satisfies it)."""

    def search(self, request: SearchRequest) -> SearchResult: ...

    def get_listing(self, listing_id: str) -> ListingDetail: ...

    def get_photo(self, url: str) -> bytes: ...


class ListingDetailCache(Protocol):
    """Where fetched listing details are kept (screening.store.SqliteScreeningStore satisfies it)."""

    def load_detail(self, listing_id: str, now: datetime) -> ListingDetail | None: ...

    def save_detail(self, detail: ListingDetail, fetched_at: datetime) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class McpServices:
    carousell: CarousellSource
    detail_cache: ListingDetailCache
    clock: Callable[[], datetime] = utc_now
    lock: threading.Lock = field(default_factory=threading.Lock)


def build_server(services: McpServices) -> MCPServer:
    """The MCP server with its five tools. The usage notes double as the instructions hosts read on connecting."""
    notes = load_usage_notes()
    server = MCPServer(SERVER_NAME, instructions=notes)

    @server.tool(annotations=READ_ONLY_TOOL)
    def search_listings(
        request: SearchRequest, limit: PageLimit = DEFAULT_SEARCH_LIMIT, page_token: PageToken = None
    ) -> dict:
        """Search Carousell Singapore. Call this first: it is cheap, one Carousell request per page.

        `request` takes Carousell's own server-side filters (the SearchRequest fields):
        - query: keywords, e.g. "road bike". A query, a category, or both, is required.
        - category: a numeric Carousell collection id, e.g. "5707" Mobile Phones, "1793" Laptops, "5755" Chairs.
        - price_min, price_max: in SGD.
        - conditions: any of brand_new, like_new, lightly_used, well_used, heavily_used; empty means any.
        - deal_methods: meetup and/or mailing; empty means either.
        - buyer_protection_only, free_delivery_only: true to require Buyer Protection or free delivery.
        - sort: best_match (default), recent, price_low_to_high or price_high_to_low.
        - category_fields: category facets by Carousell field name, e.g. {"mobile_storage": "MOBILE_STORAGE_256_GB"}.
        Paging comes from `limit` and `page_token`; request.page_size and request.page_cursor are ignored.

        Returns {"listings": [...], "total": ..., "next_page": ...}. Each listing has id, title, price (SGD),
        price_display, condition, condition_label, like_count, seller_username, seller_id, thumbnail_url,
        listing_url, posted_or_bumped_at and posted_or_bumped_kind. Carousell caps total (1000 means "1,000+").
        For the next page, call again with the same request and page_token set to next_page. Nothing is filtered
        or ranked here: match titles, prices and sellers yourself over this JSON.
        """
        return run_search(services, request, limit, page_token)

    @server.tool(annotations=READ_ONLY_TOOL)
    def listing_details(listing_id: ListingId) -> dict:
        """Everything about one listing: description, all photo URLs (image_urls), seller rating, review count,
        join date and response rate, verification, meet-up points, mailing, and created / last-modified times.

        One Carousell request unless the listing was fetched in the last 12 hours, so call it only on the listings
        you have shortlisted from search_listings, never on every result.
        """
        return detail_response(load_listing_detail(services, listing_id))

    @server.tool(annotations=READ_ONLY_TOOL)
    def listing_photo(url: PhotoUrl, max_width: PhotoMaxWidth = DEFAULT_PHOTO_MAX_WIDTH) -> Image:
        """Download one listing photo to look at, e.g. to judge photo quality or the item's visible condition.

        `url` must be a listing's thumbnail_url (from search_listings: the small cover photo buyers see in the
        results) or an entry of image_urls (from listing_details: full-size photos). Other domains are refused.
        Photos wider than max_width pixels are scaled down, keeping the aspect ratio. Each call is one Carousell
        request, so call it only on shortlisted listings, and usually only for the main photo.
        """
        photo = fetch_photo(services, url, max_width)
        return Image(data=photo.data, format=photo.format)

    @server.tool(annotations=READ_ONLY_TOOL)
    def screening_tags(listing_id: ListingId, query: SearchKeywords = None) -> dict:
        """Optional: carousearch's rule-based tags for one listing (plain rules, no AI). MULTI_ITEM_TITLE: the title
        names several models or storage sizes. PRICE_LIST_IN_DESCRIPTION: the description prices several variants.
        LISTED_PRICE_IS_FLOOR: the listed price is the cheapest of several, or a "from $X" price. Each tag has a
        reason with the evidence; severity "warn" means the listed price probably does not apply to one item.

        Uses the same cached detail as listing_details: one Carousell request unless already fetched. You can
        equally apply your own rules to listing_details.
        """
        return tags_response(listing_id, single_listing_tags(load_listing_detail(services, listing_id), query))

    @server.tool(annotations=READ_ONLY_TOOL)
    def usage_notes() -> str:
        """How to use these tools well: the recommended order, the Carousell request budget per task and the JSON
        shapes. Read it once at the start of a task.
        """
        return notes

    return server


def serve(server: MCPServer, transport: McpTransport, port: int = DEFAULT_MCP_HTTP_PORT) -> None:
    """Run until the host disconnects (stdio) or Ctrl+C (http: streamable HTTP at http://127.0.0.1:<port>/mcp)."""
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=HTTP_HOST, port=port)


def load_usage_notes() -> str:
    return USAGE_NOTES_FILE.read_text(encoding="utf-8")


# --- calls into carousearch (I/O through the injected services) ---------------------------------------------


def run_search(services: McpServices, request: SearchRequest, limit: int, page_token: str | None) -> dict:
    paged_request = request.model_copy(update={"page_size": limit, "page_cursor": page_token})
    with carousell_call(services):
        result = services.carousell.search(paged_request)
    return search_response(result, limit)


def load_listing_detail(services: McpServices, listing_id: str) -> ListingDetail:
    """The detail from the cache when it is fresh, otherwise from Carousell (and then cached)."""
    with carousell_call(services):
        now = services.clock()
        cached = services.detail_cache.load_detail(listing_id, now)
        if cached is not None:
            return cached
        detail = services.carousell.get_listing(listing_id)
        services.detail_cache.save_detail(detail, now)
        return detail


def fetch_photo(services: McpServices, url: str, max_width: int) -> Photo:
    if not is_carousell_media_url(url):
        allowed_hosts = ", ".join(sorted(CAROUSELL_MEDIA_HOSTS))
        raise ToolError(f"Only https photo URLs on {allowed_hosts} are allowed (a thumbnail_url or image_urls entry).")
    with carousell_call(services):
        image_bytes = services.carousell.get_photo(url)
    try:
        return downsize_photo(image_bytes, max_width)
    except ValueError as error:
        raise ToolError(f"Carousell sent a photo that could not be read: {error}") from error


@contextmanager
def carousell_call(services: McpServices) -> Iterator[None]:
    """One Carousell or cache call at a time; Carousell failures become ToolErrors, whose text the host sees."""
    with services.lock:
        try:
            yield
        except CarousellBlockedError as error:
            raise ToolError(
                f"Carousell is blocking or rate-limiting requests ({error}). Stop and tell the user; "
                "try again in a few minutes."
            ) from error
        except CarousellError as error:
            raise ToolError(f"Carousell request failed: {type(error).__name__}: {error}") from error


# --- response shaping (pure) -----------------------------------------------------------------------------------


def search_response(result: SearchResult, limit: int) -> dict:
    """At most `limit` listings without Carousell's raw objects, plus the total and the next-page cursor."""
    return {
        "listings": [listing.model_dump(mode="json", exclude={"raw"}) for listing in result.listings[:limit]],
        "total": result.total,
        "next_page": result.next_page,
    }


def detail_response(detail: ListingDetail) -> dict:
    return detail.model_dump(mode="json", exclude={"raw"})


def single_listing_tags(detail: ListingDetail, query: str | None) -> list[Tag]:
    """The rules that need only the listing itself: the title rule and the description rules (no peers, no AI)."""
    title_tag = title_lists_multiple_variants(detail.title, query)
    return [*([title_tag] if title_tag else []), *description_tags(detail.price, detail.description or "")]


def tags_response(listing_id: str, tags: list[Tag]) -> dict:
    return {"listing_id": listing_id, "tags": [tag.model_dump(mode="json") for tag in tags]}
