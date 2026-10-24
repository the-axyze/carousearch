"""Search filters and listing models: the single source of truth for every part of carousearch.

The CLI builds a `SearchRequest` today. The LLM prompt parser and the Telegram filter keyboard
will build the same model later, and the Carousell client turns it into an API payload
(see `carousearch.filters.payload`). Enum values are semantic names, not Carousell codes, so an
LLM can fill them without knowing Carousell's internals.

Not supported as a filter: "posted within N days". Carousell has no server-side filter for
listing age (CAROUSELL_ACCESS_NOTES.md section 4.4). A later phase should sort by RECENT and
cut off on `Listing.posted_or_bumped_at` client-side.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The web client's default page size is 48; 20 and 40 were also tested (access notes 4.2).
MAX_PAGE_SIZE = 48

# Filter names that SearchRequest sets through its own fields. Passing them again through
# `category_fields` would send two conflicting filters.
RESERVED_FILTER_NAMES = frozenset(
    {
        "collections",
        "price",
        "price_start",
        "price_end",
        "layered_condition",
        "_deal_method",
        "is_bp_enabled",
        "shipping_offer_free_shipping",
        "sort_by",
    }
)


class Condition(StrEnum):
    BRAND_NEW = "brand_new"
    LIKE_NEW = "like_new"
    LIGHTLY_USED = "lightly_used"
    WELL_USED = "well_used"
    HEAVILY_USED = "heavily_used"


class SortOrder(StrEnum):
    BEST_MATCH = "best_match"
    RECENT = "recent"
    PRICE_LOW_TO_HIGH = "price_low_to_high"
    PRICE_HIGH_TO_LOW = "price_high_to_low"
    # Carousell sorts by distance from a chosen location. Phase 1 has no location filter,
    # so what Carousell does with NEARBY on its own is untested.
    NEARBY = "nearby"


class DealMethod(StrEnum):
    MEETUP = "meetup"
    MAILING = "mailing"


class PostedOrBumpedKind(StrEnum):
    """What the timestamp on a search card means (card `aboveFold[0].component`)."""

    CREATED = "time_created"
    BUMPED = "active_bump"
    BUMP_EXPIRED = "expired_bump"


class SearchRequest(BaseModel):
    """One Carousell search. Every field maps to a server-side Carousell filter."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    # Carousell collection `id` as a numeric string, e.g. "5707" for Mobile Phones.
    # This is the collection id, not the `cc_id` and not the URL slug (access notes 4.8, 7.4).
    category: str | None = None
    price_min: float | None = Field(default=None, ge=0)  # SGD
    price_max: float | None = Field(default=None, ge=0)  # SGD
    conditions: list[Condition] = Field(default_factory=list)  # empty means any condition
    deal_methods: list[DealMethod] = Field(default_factory=list)  # empty means any deal method
    buyer_protection_only: bool = False
    free_delivery_only: bool = False
    sort: SortOrder = SortOrder.BEST_MATCH
    # Category-specific facets by Carousell field name, e.g. {"mobile_storage": "MOBILE_STORAGE_256_GB"}.
    # Valid names and values depend on the category (access notes 4.4).
    category_fields: dict[str, str | list[str]] = Field(default_factory=dict)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    # The `next_page` value of a previous SearchResult for the same request.
    page_cursor: str | None = None

    @field_validator("query")
    @classmethod
    def blank_query_becomes_none(cls, query: str | None) -> str | None:
        if query is None or not query.strip():
            return None
        return query.strip()

    @field_validator("category")
    @classmethod
    def category_must_be_collection_id(cls, category: str | None) -> str | None:
        if category is not None and not category.isdigit():
            raise ValueError("category must be a numeric Carousell collection id, e.g. '5707'")
        return category

    @field_validator("category_fields")
    @classmethod
    def category_fields_must_not_override_core_filters(
        cls, category_fields: dict[str, str | list[str]]
    ) -> dict[str, str | list[str]]:
        for field_name, value in category_fields.items():
            if not field_name:
                raise ValueError("category field names must not be empty")
            if field_name in RESERVED_FILTER_NAMES:
                raise ValueError(f"'{field_name}' is set through its own SearchRequest field")
            if isinstance(value, list) and not value:
                raise ValueError(f"category field '{field_name}' has an empty value list")
        return category_fields

    @model_validator(mode="after")
    def check_search_is_meaningful(self) -> "SearchRequest":
        if self.query is None and self.category is None:
            raise ValueError("a search needs a query, a category, or both")
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("price_min must not be greater than price_max")
        return self


class Listing(BaseModel):
    """A listing as shown on a search results card."""

    id: str
    title: str
    price: float | None  # None when the display price is not a plain number
    currency: str | None
    price_display: str  # exactly as Carousell shows it, e.g. "S$1,400"
    # None for old listings that predate Carousell's five-level conditions; see condition_label.
    condition: Condition | None
    condition_label: str | None  # Carousell's wording, e.g. "Like new", or "Used" on old listings
    like_count: int
    seller_username: str | None
    seller_id: str | None
    thumbnail_url: str | None
    listing_url: str
    posted_or_bumped_at: datetime | None
    posted_or_bumped_kind: PostedOrBumpedKind | None
    raw: dict = Field(default_factory=dict, repr=False)


class MeetupLocation(BaseModel):
    name: str
    address: str | None
    latitude: float | None
    longitude: float | None


class SellerVerification(BaseModel):
    email_verified: bool | None
    mobile_verified: bool | None
    id_verified: bool | None
    google_verified: bool | None
    facebook_verified: bool | None


class ListingDetail(Listing):
    """A listing as returned by the listing-detail endpoint.

    Fields the endpoint does not provide are None. The detail endpoint has no bump time and
    no thumbnail, so `posted_or_bumped_at` and `thumbnail_url` are None here; use
    `created_at` and `image_urls` instead.
    """

    description: str | None
    image_urls: list[str]
    created_at: datetime | None
    last_modified_at: datetime | None
    seller_rating: float | None  # feedback score out of 5
    seller_review_count: int | None
    seller_joined_at: datetime | None
    seller_response_rate: str | None  # Carousell code, seen: "H" (high); presumably also "M" and "L"
    seller_verification: SellerVerification | None
    meetup_available: bool | None
    mailing_available: bool | None
    meetup_locations: list[MeetupLocation]


class SearchResult(BaseModel):
    listings: list[Listing]
    total: int | None  # Carousell caps this, e.g. 1000 when it shows "1,000+"
    total_display: str | None  # e.g. "1,000+"
    next_page: str | None  # pass back as SearchRequest.page_cursor; None when there are no more pages
