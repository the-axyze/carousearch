"""What the templates show, worked out from search requests and screening results. Pure functions, no I/O.

Templates only print these fields and pick CSS classes from them. Every decision (which price to show, which
badges, what the results line says, which posts "Hide multi-item posts" hid and why, how a date reads) is made here,
so it can be tested without HTML.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from carousearch.bot.keyboard import SORT_CHOICES
from carousearch.bot.messages import COMMON_CATEGORIES, CONDITION_LABELS, DEAL_METHOD_LABELS
from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.filters.payload import format_price
from carousearch.filters.schema import Listing, ListingDetail, SearchRequest, SortOrder
from carousearch.screening.models import PriceSource, ScreenedListing, Severity, Tag, TagCode, format_sgd
from carousearch.web.query_params import ResultsTally, load_more_url

SINGAPORE = ZoneInfo("Asia/Singapore")

SORT_LABELS: dict[SortOrder, str] = {
    SortOrder.BEST_MATCH: "Best match",
    SortOrder.RECENT: "Recent",
    SortOrder.PRICE_LOW_TO_HIGH: "Price ↑",
    SortOrder.PRICE_HIGH_TO_LOW: "Price ↓",
}
RESPONSE_RATE_LABELS = {"H": "High", "M": "Medium", "L": "Low"}

# A description longer than this starts clamped, with a "Show more" button.
LONG_DESCRIPTION_LINES = 12
LONG_DESCRIPTION_CHARACTERS = 700

RESOLVED_CAPTION = "from description"
NOT_STATED_CAPTION = "price for your item not stated"


# --- the search form ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class SearchFormView:
    query: str
    price_min: str
    price_max: str
    conditions: list[Choice]
    deliveries: list[Choice]
    sorts: list[Choice]
    categories: list[Choice]
    hide_multi_item: bool


def search_form_view(request: SearchRequest | None, hide_multi_item: bool) -> SearchFormView:
    """The controls as they should be drawn for this search; all at their defaults when there is none."""
    # No search: draw an empty request's form (built unvalidated, as a real one needs keywords or a category).
    shown = request or SearchRequest.model_construct()
    return SearchFormView(
        query=shown.query or "",
        price_min=format_optional_price(shown.price_min),
        price_max=format_optional_price(shown.price_max),
        conditions=[
            Choice(condition.value, label, condition in shown.conditions)
            for condition, label in CONDITION_LABELS.items()
        ],
        deliveries=[
            Choice(method.value, label, method in shown.deal_methods) for method, label in DEAL_METHOD_LABELS.items()
        ],
        sorts=[Choice(sort.value, SORT_LABELS[sort], sort == shown.sort) for sort in SORT_CHOICES],
        categories=category_choices(shown.category),
        hide_multi_item=hide_multi_item,
    )


def category_choices(selected: str | None) -> list[Choice]:
    """Any, the common categories, and the selected id itself when it is not a common one (from a typed URL)."""
    choices = [Choice("", "Any category", selected is None)]
    choices += [Choice(category_id, label, category_id == selected) for category_id, label in COMMON_CATEGORIES.items()]
    if selected is not None and selected not in COMMON_CATEGORIES:
        choices.append(Choice(selected, f"Collection {selected}", True))
    return choices


def format_optional_price(price: float | None) -> str:
    return "" if price is None else format_price(price)


# --- cards --------------------------------------------------------------------------------------------------------


class PriceKind(StrEnum):
    LISTED = "listed"  # the card price, nothing better known
    RESOLVED = "resolved"  # the description gives a different price for the item searched for
    NOT_STATED = "not-stated"  # the description was read and gives no price for the item searched for


@dataclass(frozen=True)
class PriceDisplay:
    kind: PriceKind
    price: str  # the price to read first
    struck_price: str | None  # the listed price, struck through beside a resolved one
    caption: str | None


class BadgeTone(StrEnum):
    AMBER = "amber"
    GREY = "grey"


@dataclass(frozen=True)
class Badge:
    label: str
    tone: BadgeTone
    reasons: str  # the tooltip: one "Label: reason" line per tag behind the badge


@dataclass(frozen=True)
class CardView:
    listing_id: str
    title: str
    url: str
    thumbnail_url: str | None
    price: PriceDisplay
    badges: list[Badge]
    meta: str


def card_view(screened: ScreenedListing) -> CardView:
    listing = screened.listing
    return CardView(
        listing_id=listing.id,
        title=listing.title,
        url=listing.listing_url,
        thumbnail_url=listing.thumbnail_url,
        price=price_display(screened),
        badges=badges_for(screened),
        meta=meta_line(listing),
    )


def price_display(screened: ScreenedListing) -> PriceDisplay:
    listing = screened.listing
    resolved = screened.resolved_price
    if resolved.source == PriceSource.DESCRIPTION and resolved.amount is not None and resolved.amount != listing.price:
        return PriceDisplay(PriceKind.RESOLVED, format_sgd(resolved.amount), listing.price_display, RESOLVED_CAPTION)
    if resolved.source == PriceSource.NOT_STATED:
        return PriceDisplay(PriceKind.NOT_STATED, listing.price_display, None, NOT_STATED_CAPTION)
    return PriceDisplay(PriceKind.LISTED, listing.price_display, None, None)


def badges_for(screened: ScreenedListing) -> list[Badge]:
    """Multi-item (amber) when any warn tag is present, then Low price (grey) for the below-market tag."""
    badges = []
    if screened.is_multi_item:
        badges.append(Badge("Multi-item", BadgeTone.AMBER, describe_tags(warning_tags(screened))))
    below_market = [tag for tag in screened.tags if tag.code == TagCode.PRICE_BELOW_MARKET]
    if below_market:
        badges.append(Badge("Low price", BadgeTone.GREY, describe_tags(below_market)))
    return badges


def warning_tags(screened: ScreenedListing) -> list[Tag]:
    """The warn-severity tags: the ones that make a listing multi-item."""
    return [tag for tag in screened.tags if tag.severity == Severity.WARN]


def describe_tags(tags: Sequence[Tag]) -> str:
    return "\n".join(describe_tag(tag) for tag in tags)


def describe_tag(tag: Tag) -> str:
    """ "Multi-item title: title names 3 variants"."""
    return f"{tag.label}: {tag.reason}"


def meta_line(listing: Listing) -> str:
    """ "Lightly used · 3 likes · grandstone", leaving out whatever the listing does not have."""
    likes = plural(listing.like_count, "like") if listing.like_count else None
    return " · ".join(part for part in (listing.condition_label, likes, listing.seller_username) if part)


# --- the results region -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenedPage:
    """One page of screened search results, as web.services returns it."""

    listings: list[ScreenedListing]
    total_display: str | None  # Carousell's count, e.g. "1,000+"
    next_page: str | None


@dataclass(frozen=True)
class HiddenRowView:
    """A post "Hide multi-item posts" took out of the grid, with the reasons it was taken out."""

    listing_id: str
    title: str
    url: str
    price: str  # the listed price, as Carousell shows it
    reasons: list[str]  # one "Label: reason" per warn tag, the lines of the card's Multi-item tooltip


@dataclass(frozen=True)
class ResultsView:
    cards: list[CardView]
    summary: str
    load_more_url: str | None
    nothing_found: bool  # Carousell returned no listings at all for this search
    hide_multi_item: bool  # draw the "Hidden (n)" section, even when nothing was hidden
    hidden: list[HiddenRowView]  # this page's hidden posts
    hidden_summary: str  # "Hidden (n)", n counted over every page shown so far


def results_view(
    page: ScreenedPage,
    request: SearchRequest,
    hide_multi_item: bool,
    earlier_pages: ResultsTally,
    price_lookup_enabled: bool,
) -> ResultsView:
    tally = earlier_pages.plus(tally_page(page.listings, hide_multi_item))
    more_url = load_more_url(request, page.next_page, hide_multi_item, tally) if page.next_page else None
    return ResultsView(
        cards=[card_view(item) for item in visible_listings(page.listings, hide_multi_item)],
        summary=results_summary(tally, page.total_display, page.next_page is not None, price_lookup_enabled),
        load_more_url=more_url,
        nothing_found=tally.shown + tally.hidden == 0,
        hide_multi_item=hide_multi_item,
        hidden=hidden_rows(page.listings, hide_multi_item),
        hidden_summary=f"Hidden ({tally.hidden:,})",
    )


def visible_listings(screened: Sequence[ScreenedListing], hide_multi_item: bool) -> list[ScreenedListing]:
    return [item for item in screened if not (hide_multi_item and item.is_multi_item)]


def hidden_rows(screened: Sequence[ScreenedListing], hide_multi_item: bool) -> list[HiddenRowView]:
    """The posts visible_listings leaves out, each with its warn-tag reasons; none when the switch is off."""
    if not hide_multi_item:
        return []
    return [hidden_row(item) for item in screened if item.is_multi_item]


def hidden_row(screened: ScreenedListing) -> HiddenRowView:
    listing = screened.listing
    return HiddenRowView(
        listing_id=listing.id,
        title=listing.title,
        url=listing.listing_url,
        price=listing.price_display,
        reasons=[describe_tag(tag) for tag in warning_tags(screened)],
    )


def tally_page(screened: Sequence[ScreenedListing], hide_multi_item: bool) -> ResultsTally:
    multi_item = sum(item.is_multi_item for item in screened)
    hidden = multi_item if hide_multi_item else 0
    return ResultsTally(
        shown=len(screened) - hidden,
        multi_item=multi_item,
        hidden=hidden,
        screened=sum(item.detail is not None for item in screened),
    )


def results_summary(
    tally: ResultsTally, total_display: str | None, more_pages: bool, price_lookup_enabled: bool
) -> str:
    """ "20 of 1,000+ results · 3 multi-item hidden · screened 6 descriptions"."""
    if more_pages and total_display:
        parts = [f"{tally.shown} of {total_display} results"]
    else:
        parts = [plural(tally.shown, "result")]
    if tally.hidden:
        parts.append(f"{tally.hidden} multi-item hidden")
    elif tally.multi_item:
        parts.append(f"{tally.multi_item} multi-item")
    if tally.screened:
        parts.append(f"screened {plural(tally.screened, 'description')}")
    if tally.multi_item and not price_lookup_enabled:
        parts.append("price lookup off")
    return " · ".join(parts)


# --- the detail panel ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    label: str
    lines: list[str]


@dataclass(frozen=True)
class TagView:
    label: str
    reason: str
    tone: BadgeTone


@dataclass(frozen=True)
class DetailView:
    listing_id: str
    title: str
    url: str
    description: str
    description_is_long: bool
    photo_urls: list[str]
    facts: list[Fact]
    tags: list[TagView]
    screened: bool  # False when the listing's screening is not known (it was not in a recent search)


def detail_view(detail: ListingDetail, screened: ScreenedListing | None) -> DetailView:
    """`screened` is the listing as the search screened it, when known; its tags are listed in full."""
    description = (detail.description or "").strip()
    tags = screened.tags if screened is not None else []
    return DetailView(
        listing_id=detail.id,
        title=detail.title,
        url=detail.listing_url,
        description=description,
        description_is_long=is_long(description),
        photo_urls=detail.image_urls,
        facts=detail_facts(detail, screened),
        tags=[TagView(tag.label, tag.reason, tone_for(tag)) for tag in tags],
        screened=screened is not None,
    )


def detail_facts(detail: ListingDetail, screened: ScreenedListing | None) -> list[Fact]:
    """Seller and listing facts, leaving out the ones Carousell did not give."""
    response_rate = detail.seller_response_rate
    candidates = [
        ("Seller", [detail.seller_username]),
        ("Rating", [describe_rating(detail.seller_rating, detail.seller_review_count)]),
        ("Joined", [format_month(detail.seller_joined_at)]),
        ("Response rate", [RESPONSE_RATE_LABELS.get(response_rate, response_rate) if response_rate else None]),
        ("Meet-up", [location.name for location in detail.meetup_locations]),
        ("Posted", [format_day(detail.created_at)]),
        ("Price check", [price_check_note(screened)]),
    ]
    facts = [Fact(label, [line for line in lines if line]) for label, lines in candidates]
    return [fact for fact in facts if fact.lines]


def describe_rating(rating: float | None, review_count: int | None) -> str | None:
    """ "4.95 ★ · 88 reviews"; None when there is no rating."""
    if rating is None:
        return None
    stars = f"{rating:.2f}".rstrip("0").rstrip(".") + " ★"
    return stars if review_count is None else f"{stars} · {plural(review_count, 'review')}"


def price_check_note(screened: ScreenedListing | None) -> str | None:
    """What the price lookup read in the description, when it read it."""
    if screened is None or screened.resolved_price.source == PriceSource.LISTED:
        return None
    return screened.resolved_price.note


def tone_for(tag: Tag) -> BadgeTone:
    return BadgeTone.AMBER if tag.severity == Severity.WARN else BadgeTone.GREY


def is_long(description: str) -> bool:
    return description.count("\n") + 1 > LONG_DESCRIPTION_LINES or len(description) > LONG_DESCRIPTION_CHARACTERS


# --- errors -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorMessage:
    text: str
    command: str | None = None  # printed as code after the text


RATE_LIMITED_MESSAGE = ErrorMessage("Carousell is rate-limiting us, try again in a minute.")
SITE_CHANGED_MESSAGE = ErrorMessage("Carousell changed its response; run", "carousearch canary")
GENERIC_ERROR_MESSAGE = ErrorMessage("Something went wrong. Try again in a moment.")


def error_message_for(error: Exception) -> ErrorMessage:
    if isinstance(error, CarousellBlockedError):
        return RATE_LIMITED_MESSAGE
    if isinstance(error, CarousellSchemaError):
        return SITE_CHANGED_MESSAGE
    return GENERIC_ERROR_MESSAGE


# --- formatting ---------------------------------------------------------------------------------------------------


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def format_month(moment: datetime | None) -> str | None:
    """ "May 2019", in Singapore time."""
    return None if moment is None else moment.astimezone(SINGAPORE).strftime("%b %Y")


def format_day(moment: datetime | None) -> str | None:
    """ "23 Oct 2026", in Singapore time."""
    if moment is None:
        return None
    local = moment.astimezone(SINGAPORE)
    return f"{local.day} {local:%b %Y}"
