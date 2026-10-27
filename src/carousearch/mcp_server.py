"""A thin MCP server: lets an agent host (Claude Code, Codex CLI, the Claude desktop app, ChatGPT) search Carousell
through carousearch's client.

The host does all the reasoning: it filters and ranks the JSON these tools return, looks at the photos and plans its
own calls. This module only connects; each tool is one thin call into existing code:

- search_listings: CarousellClient.search (optionally only some fields per listing)
- listing_details: CarousellClient.get_listing through the 12-hour listing_detail_cache
- listing_photo: CarousellClient.get_photo, downsized by carousearch.photos
- screening_tags: the title and description rules of carousearch.screening.rules, for one listing
- category_fields: the category tree snapshot (carousell.categories) and CarousellClient.get_category_filters
- sample_descriptions: one search page shown as short text (card snippets, cached details, a few fetched ones), so a
  host can learn what sellers in a category actually state before planning a search
- usage_notes: the recommended order, the request budget and the JSON shapes (mcp_instructions.md)

`build_server(services)` wires the tools to injected services, so tests pass fakes and importing this module touches
nothing. The MCP SDK runs synchronous tools in worker threads; `McpServices.lock` keeps them to one Carousell or
database call at a time.
"""

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from carousearch.carousell.categories import CategoryTree, load_category_tree
from carousearch.carousell.client import CAROUSELL_MEDIA_HOSTS, is_carousell_media_url
from carousearch.carousell.errors import CarousellBlockedError, CarousellError
from carousearch.config import DEFAULT_MCP_HTTP_PORT
from carousearch.filters.schema import (
    MAX_PAGE_SIZE,
    RESERVED_FILTER_NAMES,
    CategoryFilter,
    Listing,
    ListingDetail,
    SearchRequest,
    SearchResult,
)
from carousearch.photos import Photo, downsize_photo
from carousearch.screening.models import Tag
from carousearch.screening.rules import description_tags, title_lists_multiple_variants

SERVER_NAME = "carousearch"
USAGE_NOTES_FILE = Path(__file__).with_name("mcp_instructions.md")
McpTransport = Literal["stdio", "http"]
HTTP_HOST = "127.0.0.1"  # this machine only; a tunnel reaches it from outside (docs/MCP.md)
DEFAULT_SEARCH_LIMIT = 20
# Tool names, for code that treats one tool's calls or results specially (the agent's budget and listings.db).
SEARCH_TOOL = "search_listings"
DETAILS_TOOL = "listing_details"
PHOTO_TOOL = "listing_photo"
SCREENING_TOOL = "screening_tags"
CATEGORY_TOOL = "category_fields"
SAMPLE_TOOL = "sample_descriptions"
DEFAULT_SAMPLE_SIZE = 20
MAX_SAMPLE_DETAIL_FETCHES = 5
SAMPLE_EXCERPT_CHARACTERS = 300
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
# The fields of a search result listing (Listing without `raw`); a test keeps the two in step.
ListingField = Literal[
    "id",
    "title",
    "price",
    "currency",
    "price_display",
    "condition",
    "condition_label",
    "like_count",
    "seller_username",
    "seller_id",
    "thumbnail_url",
    "listing_url",
    "posted_or_bumped_at",
    "posted_or_bumped_kind",
    "description_snippet",
]
ListingFields = Annotated[
    list[ListingField] | None,
    Field(description="Only these fields for each listing (id is always included), to keep the result short; omit "
          "for all of them."),
]
SampleSize = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Listings to sample, 1 to 48.")]
SampleDetailFetches = Annotated[
    int,
    Field(
        ge=0,
        le=MAX_SAMPLE_DETAIL_FETCHES,
        description=f"Full descriptions to fetch for sampled listings that have neither a card snippet nor a cached "
        f"detail, 0 to {MAX_SAMPLE_DETAIL_FETCHES}; each is one Carousell request.",
    ),
]
CategoryIdArgument = Annotated[
    str | None,
    Field(pattern=r"^\d+$", description="A category's numeric id; omit to list the top-level categories."),
]
# Carousell filters that SearchRequest sets through its own fields, and the field that does.
CORE_FILTER_FIELDS = {
    "sort_by": "request.sort",
    "price_start": "request.price_min",
    "price_end": "request.price_max",
    "layered_condition": "request.conditions",
    "_deal_method": "request.deal_methods",
    "is_bp_enabled": "request.buyer_protection_only",
    "shipping_offer_free_shipping": "request.free_delivery_only",
}


class CarousellSource(Protocol):
    """What the server needs from Carousell (CarousellClient satisfies it)."""

    def search(self, request: SearchRequest) -> SearchResult: ...

    def get_listing(self, listing_id: str) -> ListingDetail: ...

    def get_photo(self, url: str) -> bytes: ...

    def get_category_filters(self, category_id: str) -> list[CategoryFilter]: ...


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
    categories: CategoryTree = field(default_factory=load_category_tree)


def build_server(services: McpServices) -> MCPServer:
    """The MCP server with its seven tools. The usage notes double as the instructions hosts read on connecting."""
    notes = load_usage_notes()
    server = MCPServer(SERVER_NAME, instructions=notes)

    @server.tool(annotations=READ_ONLY_TOOL)
    def search_listings(
        request: SearchRequest,
        limit: PageLimit = DEFAULT_SEARCH_LIMIT,
        page_token: PageToken = None,
        fields: ListingFields = None,
    ) -> dict:
        """Search Carousell Singapore. Call this first: it is cheap, one Carousell request per page.

        `request` takes Carousell's own server-side filters (the SearchRequest fields):
        - query: keywords. A query, a category, or both, is required.
        - category: a numeric Carousell collection id; category_fields lists the categories and their filters.
        - price_min, price_max: in SGD.
        - conditions: any of brand_new, like_new, lightly_used, well_used, heavily_used; empty means any.
        - deal_methods: meetup and/or mailing; empty means either.
        - buyer_protection_only, free_delivery_only: true to require Buyer Protection or free delivery.
        - sort: best_match (default), recent, price_low_to_high or price_high_to_low.
        - category_fields: category facets by Carousell field name, {"<field_name>": "<value>"}; the
          category_fields tool lists each category's field names and allowed values.
        Paging comes from `limit` and `page_token`; request.page_size and request.page_cursor are ignored.
        `fields` keeps only the listed fields of each listing, e.g. ["id", "title", "price"], for a shorter result.

        Returns {"listings": [...], "total": ..., "next_page": ...}. Each listing has id, title, price (SGD),
        price_display, condition, condition_label, like_count, seller_username, seller_id, thumbnail_url,
        listing_url, posted_or_bumped_at, posted_or_bumped_kind and description_snippet (the start of the
        description, about 300 characters, as the card shows it). Carousell caps total (1000 means "1,000+").
        For the next page, call again with the same request and page_token set to next_page. Nothing is filtered
        or ranked here: match titles, prices and sellers yourself over this JSON.
        """
        return project_search_response(run_search(services, request, limit, page_token), fields)

    @server.tool(annotations=READ_ONLY_TOOL)
    def listing_details(listing_id: ListingId) -> dict:
        """Everything about one listing: description, the seller-entered category fields (attributes), all photo
        URLs (image_urls), seller rating, review count, join date and response rate, verification, meet-up points,
        mailing, and created / last-modified times.

        One Carousell request unless the listing was fetched in the last 12 hours, so call it only on the listings
        you have shortlisted from search_listings, never on every result.
        """
        return detail_response(load_listing_detail(services, listing_id))

    @server.tool(annotations=READ_ONLY_TOOL)
    def listing_photo(url: PhotoUrl, max_width: PhotoMaxWidth = DEFAULT_PHOTO_MAX_WIDTH) -> Image:
        """Download one listing photo to look at, e.g. to judge photo quality or the visible condition.

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
    def category_fields(category: CategoryIdArgument = None) -> dict:
        """Carousell's categories and the filters each one offers, to search the right category with the right
        filters.

        Without `category`: the top-level categories, from a snapshot of Carousell's category tree (no request).
        With a category id: its path, its subcategories (same snapshot) and the filters Carousell offers there, read
        live (one Carousell request): each filter's field_name, label, type, allowed values, and `set_with`, the
        search_listings argument that applies it. Filters whose set_with is "request.category_fields" go into
        request.category_fields by field_name, {"<field_name>": "<value>"}. Some categories split by subcategory
        instead of by filter, so check subcategories first; a top-level category often reports only the generic
        filters, while its subcategories report their own.
        """
        if category is None:
            return top_level_categories_response(services.categories)
        filters = load_category_filters(services, category)
        return category_fields_response(services.categories, category, filters)

    @server.tool(annotations=READ_ONLY_TOOL)
    def sample_descriptions(
        request: SearchRequest, n: SampleSize = DEFAULT_SAMPLE_SIZE, max_detail_fetches: SampleDetailFetches = 0
    ) -> dict:
        """Learn what sellers in a search actually write, before deciding how to search it: one search page,
        shown as short text per listing. Use it to see which facts sellers state, how they phrase them, and which
        structured fields they fill in.

        `request` is the same as search_listings' (a query, a category, or both, plus any filters). Each sample has
        id, title, price, description_excerpt (the first ~300 characters: from a cached detail when there is one,
        else the search card's snippet), excerpt_source ("detail", "card" or "none") and structured_fields
        (condition, the category name and the seller-entered category fields when the detail is known).
        One Carousell request for the page; details come only from the 12-hour cache unless max_detail_fetches
        allows a few requests for listings that have no excerpt at all.
        """
        return load_sample(services, request, n, max_detail_fetches)

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


def load_sample(services: McpServices, request: SearchRequest, size: int, max_detail_fetches: int) -> dict:
    """One search page, each listing with its cached detail when there is one, plus up to `max_detail_fetches`
    details fetched for listings that would otherwise have no excerpt at all."""
    paged_request = request.model_copy(update={"page_size": size, "page_cursor": None})
    with carousell_call(services):
        result = services.carousell.search(paged_request)
    listings = result.listings[:size]
    details = {listing.id: detail for listing in listings if (detail := find_cached_detail(services, listing.id))}
    without_text = [
        listing.id for listing in listings if listing.id not in details and not listing.description_snippet
    ]
    for listing_id in without_text[:max_detail_fetches]:
        try:
            details[listing_id] = load_listing_detail(services, listing_id)
        except ToolError as error:  # one missing detail only leaves its excerpt empty; a block still stops the call
            if isinstance(error.__cause__, CarousellBlockedError):
                raise
    return sample_response(listings, details, result.total)


def find_cached_detail(services: McpServices, listing_id: str) -> ListingDetail | None:
    with services.lock:
        return services.detail_cache.load_detail(listing_id, services.clock())


def load_category_filters(services: McpServices, category_id: str) -> list[CategoryFilter]:
    with carousell_call(services):
        return services.carousell.get_category_filters(category_id)


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


def project_search_response(response: dict, fields: Sequence[str] | None) -> dict:
    """The search response with only `fields` (plus id) in each listing; unchanged when `fields` is empty."""
    if not fields:
        return response
    return {**response, "listings": [project_listing(listing, fields) for listing in response["listings"]]}


def project_listing(listing: dict[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    wanted = {"id", *fields}
    return {name: value for name, value in listing.items() if name in wanted}


def detail_response(detail: ListingDetail) -> dict:
    return detail.model_dump(mode="json", exclude={"raw"})


def sample_response(listings: Sequence[Listing], details: Mapping[str, ListingDetail], total: int | None) -> dict:
    samples = [sample_entry(listing, details.get(listing.id)) for listing in listings]
    return {
        "samples": samples,
        "total": total,
        "with_full_description": sum(1 for sample in samples if sample["excerpt_source"] == "detail"),
    }


def sample_entry(listing: Listing, detail: ListingDetail | None) -> dict:
    """One listing as compact text: the description's start (detail first, else the card's snippet) and the
    structured fields known for it."""
    if detail is not None and detail.description:
        excerpt, source = detail.description, "detail"
    elif listing.description_snippet:
        excerpt, source = listing.description_snippet, "card"
    else:
        excerpt, source = "", "none"
    structured: dict[str, str] = {"condition": listing.condition_label} if listing.condition_label else {}
    if detail is not None:
        if detail.category_name:
            structured["category_name"] = detail.category_name
        structured.update(detail.attributes)
    return {
        "id": listing.id,
        "title": listing.title,
        "price": listing.price,
        "description_excerpt": cut_text(excerpt, SAMPLE_EXCERPT_CHARACTERS),
        "excerpt_source": source,
        "structured_fields": structured,
    }


def cut_text(text: str, max_characters: int) -> str:
    """Whitespace collapsed, and cut with an ellipsis past `max_characters`."""
    flat = " ".join(text.split())
    return flat if len(flat) <= max_characters else flat[: max_characters - 1] + "…"


def single_listing_tags(detail: ListingDetail, query: str | None) -> list[Tag]:
    """The rules that need only the listing itself: the title rule and the description rules (no peers, no AI)."""
    title_tag = title_lists_multiple_variants(detail.title, query)
    return [*([title_tag] if title_tag else []), *description_tags(detail.price, detail.description or "")]


def tags_response(listing_id: str, tags: list[Tag]) -> dict:
    return {"listing_id": listing_id, "tags": [tag.model_dump(mode="json") for tag in tags]}


def top_level_categories_response(tree: CategoryTree) -> dict:
    return {
        "categories": [{"id": category.id, "name": category.name} for category in tree.top_level()],
        "tree_captured_on": tree.captured_on,
    }


def category_fields_response(tree: CategoryTree, category_id: str, filters: list[CategoryFilter]) -> dict:
    """The category's place in the tree snapshot and its live filters, each with the argument that sets it."""
    path = tree.path(category_id)
    return {
        "category": {
            "id": category_id,
            "name": path[-1].name if path else None,
            "path": " > ".join(category.name for category in path) or "not in the category snapshot",
        },
        "subcategories": [{"id": child.id, "name": child.name} for child in tree.children(category_id)],
        "filters": [filter_response(category_filter) for category_filter in filters],
        "tree_captured_on": tree.captured_on,
    }


def filter_response(category_filter: CategoryFilter) -> dict:
    return {
        "field_name": category_filter.field_name,
        "label": category_filter.label,
        "type": category_filter.filter_type,
        "values": [{"value": option.value, "label": option.label} for option in category_filter.options],
        "set_with": filter_setter(category_filter),
    }


def filter_setter(category_filter: CategoryFilter) -> str:
    """The search_listings argument that applies a filter, or why none does."""
    if category_filter.field_name in CORE_FILTER_FIELDS:
        return CORE_FILTER_FIELDS[category_filter.field_name]
    if category_filter.filter_type == "id_or_keyword" and category_filter.field_name not in RESERVED_FILTER_NAMES:
        return "request.category_fields"
    return "not settable through search_listings"
