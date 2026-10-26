"""Turn Carousell responses (already-decoded JSON, HTML text, status and headers) into models.

Pure functions only: no network, no files. Shapes are documented in
CAROUSELL_ACCESS_NOTES.md sections 4.3 (search) and 4.5 (listing detail).
"""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.filters.payload import condition_from_code
from carousearch.filters.schema import (
    Listing,
    ListingDetail,
    MeetupLocation,
    PostedOrBumpedKind,
    SearchResult,
    SellerVerification,
)

LISTING_URL_TEMPLATE = "https://www.carousell.sg/p/{listing_id}/"

_INITIAL_STATE_SCRIPT = re.compile(r'<script type="application/json">(.*?)</script>', re.DOTALL)
_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_BLOCK_PAGE_TITLE_MARKERS = ("just a moment", "attention required", "captcha", "access denied")
_DISPLAY_PRICE = re.compile(r"^(?P<currency>S\$)?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)$")

# Labels for the `condition` code on search cards. 3 to 7 are the five-level conditions. 2 is the
# legacy "Used" seen on old listings (e.g. from 2021); Carousell's like-new or lightly-used
# filters still return those. Legacy 1 is presumably "New" but was never observed, so it is left out.
CARD_CONDITION_LABELS = {
    "3": "Brand new",
    "4": "Like new",
    "7": "Lightly used",
    "5": "Well used",
    "6": "Heavily used",
    "2": "Used",
}


# ---------------------------------------------------------------------------
# Bootstrap page and block detection
# ---------------------------------------------------------------------------


def extract_initial_state(page_html: str) -> dict | None:
    """The app state JSON every carousell.sg page embeds (`window.initialState`), or None."""
    for script_body in _INITIAL_STATE_SCRIPT.findall(page_html):
        try:
            initial_state = json.loads(script_body)
        except json.JSONDecodeError:
            continue
        if isinstance(initial_state, dict) and "Application" in initial_state:
            return initial_state
    return None


def extract_csrf_token(page_html: str) -> str:
    """Read `initialState.Application.csrfToken` from a carousell.sg HTML page."""
    initial_state = extract_initial_state(page_html) or {}
    application = initial_state.get("Application")
    token = application.get("csrfToken") if isinstance(application, dict) else None
    if not isinstance(token, str) or not token:
        raise CarousellSchemaError("missing key 'initialState.Application.csrfToken' in bootstrap page")
    return token


def raise_if_blocked(status_code: int, headers: Mapping[str, str], body_text: str) -> None:
    """Raise CarousellBlockedError if the response is a Cloudflare challenge, captcha or rate limit."""
    lowercase_headers = {name.lower(): value for name, value in headers.items()}
    if lowercase_headers.get("cf-mitigated", "").lower() == "challenge":
        raise CarousellBlockedError(f"Cloudflare challenge (HTTP {status_code}, cf-mitigated: challenge)")
    if status_code == 429:
        raise CarousellBlockedError("rate limited by Carousell (HTTP 429)")
    if "text/html" not in lowercase_headers.get("content-type", ""):
        return
    title = _html_title(body_text).lower()
    for marker in _BLOCK_PAGE_TITLE_MARKERS:
        if marker in title:
            raise CarousellBlockedError(f"block page (HTTP {status_code}, title {_html_title(body_text)!r})")


def _html_title(page_html: str) -> str:
    match = _HTML_TITLE.search(page_html[:20_000])
    return match.group(1).strip() if match else ""


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def parse_search_response(response_json: Any) -> SearchResult:
    data = require_key(response_json, "data", "", dict)
    listings = [
        parse_listing_card(require_key(item, "listingCard", f"data.results[{index}]", dict))
        for index, item in enumerate(_read_results(data))
        if isinstance(item, dict) and "listingCard" in item  # skip banners and other promos
    ]
    return SearchResult(
        listings=listings,
        total=_read_total(data),
        total_display=data.get("formattedTotal"),
        next_page=_read_next_page(data),
    )


def _read_results(data: dict) -> list:
    # A search with no matches omits `results` (protobuf-style JSON drops empty lists) and sends a zero `total`.
    # Only that pairing means no listings; a missing `results` with a missing or nonzero `total` is a schema change.
    if "results" not in data and _read_total(data) == 0:
        return []
    return require_key(data, "results", "data", list)


def _read_total(data: dict) -> int | None:
    total = data.get("total")
    if not isinstance(total, dict):
        return None
    # Zero is omitted like any protobuf default: a search with no matches sends "total": {}.
    return long_to_int(total.get("value", 0))


def _read_next_page(data: dict) -> str | None:
    if data.get("hasMore") is not True:
        return None
    return require_key(data, "session", "data", str)


def parse_listing_card(card: dict) -> Listing:
    listing_id = str(require_key(card, "id", "listingCard"))
    location = f"listingCard[{listing_id}]"
    price_display = require_key(card, "price", location, str)
    price, currency = parse_display_price(price_display)
    seller = card.get("seller") if isinstance(card.get("seller"), dict) else {}
    photo_urls = card.get("photoUrls") if isinstance(card.get("photoUrls"), list) else []
    posted_or_bumped_kind, posted_or_bumped_at = parse_card_timestamp(card.get("aboveFold"))
    return _build_model(
        Listing,
        location,
        id=listing_id,
        title=require_key(card, "title", location, str),
        price=price,
        currency=currency,
        price_display=price_display,
        condition=condition_from_code(card.get("condition")),
        condition_label=CARD_CONDITION_LABELS.get(str(card.get("condition"))),
        # The search API omits zero values (protobuf-style JSON); no card ever has "likesCount": 0.
        like_count=card.get("likesCount", 0),
        seller_username=seller.get("username"),
        seller_id=_optional_string(seller.get("id")),
        thumbnail_url=photo_urls[0] if photo_urls else None,
        listing_url=LISTING_URL_TEMPLATE.format(listing_id=listing_id),
        posted_or_bumped_at=posted_or_bumped_at,
        posted_or_bumped_kind=posted_or_bumped_kind,
        raw=card,
    )


def parse_card_timestamp(above_fold: Any) -> tuple[PostedOrBumpedKind | None, datetime | None]:
    """Read the creation or bump time from a card's `aboveFold` components."""
    if not isinstance(above_fold, list):
        return None, None
    known_kinds = {kind.value: kind for kind in PostedOrBumpedKind}
    for component in above_fold:
        if not isinstance(component, dict) or component.get("component") not in known_kinds:
            continue
        timestamp_content = component.get("timestampContent")
        seconds = long_to_int(timestamp_content.get("seconds")) if isinstance(timestamp_content, dict) else None
        if seconds is not None:
            return known_kinds[component["component"]], datetime.fromtimestamp(seconds, UTC)
    return None, None


def parse_display_price(price_display: str) -> tuple[float | None, str | None]:
    """'S$1,400' -> (1400.0, 'SGD'). Returns (None, None) if the text is not a plain price."""
    match = _DISPLAY_PRICE.match(price_display.strip())
    if not match:
        return None, None
    amount = float(match.group("amount").replace(",", ""))
    currency = "SGD" if match.group("currency") else None
    return amount, currency


# ---------------------------------------------------------------------------
# Listing detail
# ---------------------------------------------------------------------------


def parse_listing_detail_response(response_json: Any) -> ListingDetail:
    data = require_key(response_json, "data", "", dict)
    screens = require_key(data, "screens", "data", list)
    for screen in screens:
        meta = screen.get("meta") if isinstance(screen, dict) else None
        listing_json = meta.get("default_value") if isinstance(meta, dict) else None
        if isinstance(listing_json, dict) and "id" in listing_json:
            return parse_listing_detail(listing_json)
    raise CarousellSchemaError("missing key 'data.screens[].meta.default_value' holding the listing")


def parse_listing_detail(listing_json: dict) -> ListingDetail:
    """Parse the listing object found at `data.screens[0].meta.default_value`."""
    listing_id = str(require_key(listing_json, "id", "listing"))
    location = f"listing[{listing_id}]"
    price_text = str(require_key(listing_json, "price", location))
    seller = require_key(listing_json, "seller", location, dict)
    photos = listing_json.get("photos") if isinstance(listing_json.get("photos"), list) else []
    meetups = listing_json.get("meetups") if isinstance(listing_json.get("meetups"), list) else []
    return _build_model(
        ListingDetail,
        location,
        id=listing_id,
        title=require_key(listing_json, "title", location, str),
        price=_optional_float(price_text),
        currency=listing_json.get("currency_code"),
        price_display=f"{listing_json.get('currency_symbol') or ''}{listing_json.get('price_formatted') or price_text}",
        condition=condition_from_code(listing_json.get("layered_condition")),
        condition_label=_detail_condition_label(listing_json),
        like_count=listing_json.get("likes_count") or 0,
        seller_username=seller.get("username"),
        seller_id=_optional_string(seller.get("id")),
        thumbnail_url=None,
        listing_url=LISTING_URL_TEMPLATE.format(listing_id=listing_id),
        posted_or_bumped_at=None,
        posted_or_bumped_kind=None,
        raw=listing_json,
        description=listing_json.get("description"),
        image_urls=[photo["image_url"] for photo in photos if isinstance(photo, dict) and photo.get("image_url")],
        created_at=parse_iso_timestamp(listing_json.get("time_created")),
        last_modified_at=parse_iso_timestamp(listing_json.get("last_modified")),
        seller_rating=seller.get("feedback_score"),
        seller_review_count=seller.get("feedback_count"),
        seller_joined_at=parse_iso_timestamp(seller.get("date_joined")),
        seller_response_rate=seller.get("response_rate") or None,
        seller_verification=parse_seller_verification(seller.get("profile")),
        meetup_available=listing_json.get("meetup"),
        mailing_available=listing_json.get("mailing"),
        meetup_locations=[parse_meetup_location(meetup) for meetup in meetups if isinstance(meetup, dict)],
    )


def _detail_condition_label(listing_json: dict) -> str | None:
    attributes = listing_json.get("listing_attributes")
    label = attributes.get("condition") if isinstance(attributes, dict) else None
    if isinstance(label, str) and label:
        return label
    return CARD_CONDITION_LABELS.get(str(listing_json.get("layered_condition")))


def parse_seller_verification(profile: Any) -> SellerVerification | None:
    if not isinstance(profile, dict):
        return None
    return SellerVerification(
        email_verified=profile.get("is_email_verified"),
        mobile_verified=profile.get("is_mobile_verified"),
        id_verified=profile.get("is_id_verified"),
        google_verified=profile.get("is_gplus_verified"),
        facebook_verified=profile.get("is_facebook_verified"),
    )


def parse_meetup_location(meetup: dict) -> MeetupLocation:
    return MeetupLocation(
        name=meetup.get("name") or "",
        address=meetup.get("address") or None,
        latitude=meetup.get("latitude"),
        longitude=meetup.get("longitude"),
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def require_key(container: Any, key: str, parent_path: str, expected_type: type | None = None) -> Any:
    """Return container[key] or raise CarousellSchemaError naming the missing key's full path."""
    path = f"{parent_path}.{key}" if parent_path else key
    if not isinstance(container, dict) or container.get(key) is None:
        raise CarousellSchemaError(f"missing key '{path}'")
    value = container[key]
    if expected_type is not None and not isinstance(value, expected_type):
        raise CarousellSchemaError(
            f"key '{path}' should be {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


def long_to_int(value: Any) -> int | None:
    """Read a 64-bit number sent as an int, a digit string, or a protobuf Long {low, high, unsigned}."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if isinstance(value, dict) and isinstance(value.get("low"), int):
        low = value["low"] & 0xFFFFFFFF
        high = value.get("high", 0)
        if value.get("unsigned"):
            high &= 0xFFFFFFFF
        return high * 2**32 + low
    return None


def parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _optional_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _build_model[ModelType: Listing](model_class: type[ModelType], location: str, **fields: Any) -> ModelType:
    try:
        return model_class(**fields)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}" for problem in error.errors()
        )
        raise CarousellSchemaError(f"{location} has unexpected values: {problems}") from error
