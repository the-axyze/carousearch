"""Plain-text rendering of listings for the terminal. Pure functions: models in, strings out."""

from datetime import datetime, timedelta, timezone

from carousearch.filters.schema import Listing, ListingDetail, SearchResult, SellerVerification
from carousearch.screening.models import PriceSource, ResolvedPrice, ScreenedListing, format_sgd

SINGAPORE_TIME = timezone(timedelta(hours=8), "SGT")

# (header, width) for each table column; width 0 means "no limit" (the URL is never cut).
_TABLE_COLUMNS = [("ID", 10), ("PRICE", 9), ("CONDITION", 12), ("LIKES", 5), ("TITLE", 44), ("SELLER", 18), ("URL", 0)]
_SCREENED_TABLE_COLUMNS = [
    ("ID", 10), ("PRICE", 9), ("YOUR PRICE", 12), ("FLAGS", 34), ("CONDITION", 12), ("LIKES", 5), ("TITLE", 40),
    ("SELLER", 18), ("URL", 0),
]


def render_listing_table(listings: list[Listing]) -> str:
    rows = [[header for header, _ in _TABLE_COLUMNS]]
    for listing in listings:
        rows.append(
            [
                listing.id,
                listing.price_display,
                listing.condition_label or "-",
                str(listing.like_count),
                listing.title,
                listing.seller_username or "-",
                listing.listing_url,
            ]
        )
    return "\n".join(_format_row(row, _TABLE_COLUMNS) for row in rows)


def render_screened_table(screened: list[ScreenedListing]) -> str:
    """The search table with the screening columns: YOUR PRICE and FLAGS (tag labels)."""
    rows = [[header for header, _ in _SCREENED_TABLE_COLUMNS]]
    for item in screened:
        listing = item.listing
        rows.append(
            [
                listing.id,
                listing.price_display,
                describe_resolved_price(item.resolved_price, listing.price_display),
                ", ".join(tag.label for tag in item.tags) or "-",
                listing.condition_label or "-",
                str(listing.like_count),
                listing.title,
                listing.seller_username or "-",
                listing.listing_url,
            ]
        )
    return "\n".join(_format_row(row, _SCREENED_TABLE_COLUMNS) for row in rows)


def describe_resolved_price(resolved: ResolvedPrice, price_display: str) -> str:
    """The YOUR PRICE cell: the listed price, a description price ("S$850 (desc)"), or "not stated"."""
    if resolved.source == PriceSource.DESCRIPTION and resolved.amount is not None:
        return f"{format_sgd(resolved.amount)} (desc)"
    if resolved.source == PriceSource.NOT_STATED:
        return "not stated"
    return price_display


def render_search_summary(result: SearchResult, shown_count: int) -> str:
    total = result.total_display or (str(result.total) if result.total is not None else "unknown")
    more = "more pages available (use --json to get next_page)" if result.next_page else "no more pages"
    return f"Showing {shown_count} of {total} results; {more}."


def render_listing_detail(detail: ListingDetail) -> str:
    verification = detail.seller_verification
    lines = [
        detail.title,
        f"{detail.price_display}  |  {detail.condition_label or '-'}  |  {detail.like_count} likes",
        detail.listing_url,
        "",
        _field("Seller", f"{detail.seller_username or '-'} (id {detail.seller_id or '-'})"),
        _field("Rating", _rating_text(detail)),
        _field("Joined", format_date(detail.seller_joined_at)),
        _field("Response rate", detail.seller_response_rate or "-"),
        _field("Verified", _verification_text(verification) if verification else "-"),
        _field("Created", format_datetime(detail.created_at)),
        _field("Last modified", format_datetime(detail.last_modified_at)),
        _field("Deal", f"meet-up {_yes_no(detail.meetup_available)}, mailing {_yes_no(detail.mailing_available)}"),
    ]
    for index, meetup in enumerate(detail.meetup_locations):
        place = f"{meetup.name}, {meetup.address}" if meetup.address else meetup.name
        lines.append(_field("Meet-up points" if index == 0 else "", place))
    lines.append(_field("Photos", str(len(detail.image_urls))))
    lines.extend(f"  {url}" for url in detail.image_urls)
    lines.extend(["", "Description:"])
    lines.extend(f"  {line}" for line in (detail.description or "-").splitlines())
    return "\n".join(lines)


def format_datetime(moment: datetime | None) -> str:
    return moment.astimezone(SINGAPORE_TIME).strftime("%Y-%m-%d %H:%M SGT") if moment else "-"


def format_date(moment: datetime | None) -> str:
    return moment.astimezone(SINGAPORE_TIME).strftime("%Y-%m-%d") if moment else "-"


def _format_row(cells: list[str], columns: list[tuple[str, int]]) -> str:
    formatted = []
    for cell, (_, width) in zip(cells, columns, strict=True):
        formatted.append(_fit(cell, width) if width else cell)
    return "  ".join(formatted)


def _fit(text: str, width: int) -> str:
    single_line = " ".join(text.split())
    if len(single_line) > width:
        single_line = single_line[: width - 1] + "…"
    return single_line.ljust(width)


def _field(label: str, value: str) -> str:
    return f"{(label + ':') if label else '':<16}{value}"


def _rating_text(detail: ListingDetail) -> str:
    if detail.seller_rating is None:
        return "-"
    return f"{detail.seller_rating:.2f} / 5 from {detail.seller_review_count or 0} reviews"


def _verification_text(verification: SellerVerification) -> str:
    checks = {
        "email": verification.email_verified,
        "mobile": verification.mobile_verified,
        "ID": verification.id_verified,
        "Google": verification.google_verified,
        "Facebook": verification.facebook_verified,
    }
    verified = [name for name, is_verified in checks.items() if is_verified]
    return ", ".join(verified) if verified else "none"


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")
