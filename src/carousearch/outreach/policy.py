"""The sending policy: which listings are worth drafting for, which approved messages may go out now, and how
long to wait between two sends.

Pure: no I/O. The caller passes the store's de-duplication check, the count of recent sends and a seeded
random.Random, so every decision here is testable.
"""

import random
from collections.abc import Callable, Sequence

from carousearch.outreach.models import OutreachMessage, OutreachPolicy, ShortlistedListing, SkipReason

# (listing id, seller id, seller username) -> whether any earlier batch messaged that listing or seller.
AlreadyContacted = Callable[[str, str | None, str | None], bool]


def select_sendable(
    messages: Sequence[OutreachMessage],
    already_contacted: AlreadyContacted,
    sent_today: int,
    policy: OutreachPolicy,
) -> tuple[list[OutreachMessage], list[tuple[OutreachMessage, SkipReason]]]:
    """The messages to send, in the order given, and the others with the reason each is skipped: a listing or
    seller contacted before, a second listing or message for the same seller in this batch, or the daily cap."""
    to_send: list[OutreachMessage] = []
    skipped: list[tuple[OutreachMessage, SkipReason]] = []
    listings_in_batch: set[str] = set()
    sellers_in_batch: set[str] = set()
    allowance = cap_remaining(policy, sent_today)
    for message in messages:
        seller = seller_key(message)
        if already_contacted(message.listing_id, message.seller_id, message.seller_username):
            skipped.append((message, SkipReason.LISTING_CONTACTED))
        elif message.listing_id in listings_in_batch:
            skipped.append((message, SkipReason.LISTING_IN_BATCH))
        elif seller is not None and seller in sellers_in_batch:
            skipped.append((message, SkipReason.SELLER_IN_BATCH))
        elif len(to_send) >= allowance:
            skipped.append((message, SkipReason.DAILY_CAP))
        else:
            to_send.append(message)
            listings_in_batch.add(message.listing_id)
            if seller is not None:
                sellers_in_batch.add(seller)
    return to_send, skipped


def seller_key(message: OutreachMessage) -> str | None:
    """The seller's id, else "username:<name>", else None (then only the listing id de-duplicates)."""
    if message.seller_id:
        return message.seller_id
    return f"username:{message.seller_username}" if message.seller_username else None


def next_gap_seconds(policy: OutreachPolicy, rng: random.Random) -> float:
    """Seconds to wait before the next send, uniform between the policy's bounds."""
    return rng.uniform(policy.min_gap_seconds, policy.max_gap_seconds)


def cap_remaining(policy: OutreachPolicy, sent_today: int) -> int:
    return max(policy.daily_cap - sent_today, 0)


def choose_listings_to_draft(
    listings: Sequence[ShortlistedListing], already_contacted: AlreadyContacted, limit: int | None
) -> tuple[list[ShortlistedListing], list[tuple[ShortlistedListing, SkipReason]]]:
    """Up to `limit` listings to draft for, in order, and the ones passed over on the way with their reason: a
    listing or seller contacted before, or a seller already in this batch. No model call is spent on those."""
    to_draft: list[ShortlistedListing] = []
    skipped: list[tuple[ShortlistedListing, SkipReason]] = []
    listing_ids: set[str] = set()
    sellers: set[str] = set()
    for listing in listings:
        if limit is not None and len(to_draft) >= limit:
            break
        seller = listing.seller_id or (f"username:{listing.seller_username}" if listing.seller_username else None)
        if listing.listing_id in listing_ids:
            continue
        if already_contacted(listing.listing_id, listing.seller_id, listing.seller_username):
            skipped.append((listing, SkipReason.LISTING_CONTACTED))
        elif seller is not None and seller in sellers:
            skipped.append((listing, SkipReason.SELLER_IN_BATCH))
        else:
            to_draft.append(listing)
            listing_ids.add(listing.listing_id)
            if seller is not None:
                sellers.add(seller)
    return to_draft, skipped
