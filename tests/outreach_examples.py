"""Shared outreach test data: messages, drafts and shortlisted listings."""

from datetime import UTC, datetime

from carousearch.outreach.models import OutreachDraft, OutreachMessage, ShortlistedListing

START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def draft(listing_id: str, seller_id: str | None = None, run_id: str = "20261030-090000-run1", **changes) -> OutreachDraft:
    values = {
        "run_id": run_id,
        "listing_id": listing_id,
        "seller_id": seller_id if seller_id is not None else f"seller-{listing_id}",
        "seller_username": f"user{listing_id}",
        "listing_title": f"Listing {listing_id}",
        "listing_url": f"https://www.carousell.sg/p/{listing_id}/",
        "listing_price": 100.0,
        "offer_price": 75.0,
        "body": f"Hi, is $75 ok for listing {listing_id}?",
    }
    return OutreachDraft(**{**values, **changes})


def message(message_id: int, listing_id: str, seller_id: str | None = None, **changes) -> OutreachMessage:
    values = {"id": message_id, "status": "approved", "created_at": START, **changes}
    return OutreachMessage(**draft(listing_id, seller_id).model_dump(), **values)


def shortlisted(listing_id: str, seller_id: str | None = None, price: float | None = 120.0, **changes) -> ShortlistedListing:
    values = {
        "listing_id": listing_id,
        "title": f"Listing {listing_id} in walnut finish",
        "price": price,
        "url": f"https://www.carousell.sg/p/{listing_id}/",
        "seller_id": seller_id if seller_id is not None else f"seller-{listing_id}",
        "seller_username": f"user{listing_id}",
        "description": "Barely used, small scratch on the left side. Self collect at Tampines.",
    }
    return ShortlistedListing(**{**values, **changes})
