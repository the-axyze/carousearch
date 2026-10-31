"""The sending policy: de-duplication, the daily cap, the order, the gaps. Pure."""

import random

from carousearch.outreach.models import OutreachPolicy, SkipReason
from carousearch.outreach.policy import (
    cap_remaining,
    choose_listings_to_draft,
    next_gap_seconds,
    select_sendable,
)

from outreach_examples import message, shortlisted

POLICY = OutreachPolicy(daily_cap=10, min_gap_seconds=30, max_gap_seconds=60)


def never_contacted(listing_id: str, seller_id: str | None, seller_username: str | None) -> bool:
    return False


def test_everything_is_sent_in_order_when_nothing_stands_in_the_way() -> None:
    messages = [message(3, "c"), message(1, "a"), message(2, "b")]

    to_send, skipped = select_sendable(messages, never_contacted, 0, POLICY)

    assert [item.id for item in to_send] == [3, 1, 2] and skipped == []


def test_a_listing_or_seller_contacted_before_is_skipped() -> None:
    contacted = {("a", "seller-a"), ("b", "old-seller")}

    def already(listing_id: str, seller_id: str | None, seller_username: str | None) -> bool:
        return (listing_id, seller_id) in contacted or seller_id == "old-seller"

    messages = [message(1, "a"), message(2, "x", seller_id="old-seller"), message(3, "c")]

    to_send, skipped = select_sendable(messages, already, 0, POLICY)

    assert [item.id for item in to_send] == [3]
    assert [(item.id, reason) for item, reason in skipped] == [
        (1, SkipReason.LISTING_CONTACTED), (2, SkipReason.LISTING_CONTACTED)
    ]


def test_within_a_batch_only_the_first_listing_of_a_seller_goes_out() -> None:
    messages = [message(1, "a", seller_id="s"), message(2, "b", seller_id="s"), message(3, "a", seller_id="t")]

    to_send, skipped = select_sendable(messages, never_contacted, 0, POLICY)

    assert [item.id for item in to_send] == [1]
    assert [(item.id, reason) for item, reason in skipped] == [
        (2, SkipReason.SELLER_IN_BATCH), (3, SkipReason.LISTING_IN_BATCH)
    ]


def test_sellers_without_an_id_are_matched_by_username() -> None:
    first = message(1, "a", seller_id="").model_copy(update={"seller_id": None, "seller_username": "same"})
    second = message(2, "b", seller_id="").model_copy(update={"seller_id": None, "seller_username": "same"})

    to_send, skipped = select_sendable([first, second], never_contacted, 0, POLICY)

    assert [item.id for item in to_send] == [1] and skipped[0][1] == SkipReason.SELLER_IN_BATCH


def test_the_daily_cap_counts_earlier_sends() -> None:
    messages = [message(index, str(index)) for index in range(1, 6)]

    to_send, skipped = select_sendable(messages, never_contacted, 7, POLICY)

    assert [item.id for item in to_send] == [1, 2, 3]
    assert [(item.id, reason) for item, reason in skipped] == [(4, SkipReason.DAILY_CAP), (5, SkipReason.DAILY_CAP)]
    assert select_sendable(messages, never_contacted, 12, POLICY)[0] == []


def test_cap_remaining_never_goes_below_zero() -> None:
    assert cap_remaining(POLICY, 4) == 6
    assert cap_remaining(POLICY, 15) == 0


def test_gaps_stay_within_the_bounds_and_follow_the_seed() -> None:
    gaps = [next_gap_seconds(POLICY, random.Random(7)) for _ in range(3)]
    many = [next_gap_seconds(POLICY, rng) for rng in [random.Random(1)] for _ in range(200)]

    assert gaps[0] == gaps[1] == gaps[2]
    assert all(30 <= gap <= 60 for gap in many)
    assert max(many) - min(many) > 20  # spread over the range, not pinned to one end


def test_a_zero_width_gap_is_exact() -> None:
    assert next_gap_seconds(OutreachPolicy(min_gap_seconds=5, max_gap_seconds=5), random.Random()) == 5


def test_drafting_passes_over_contacted_listings_and_repeat_sellers_and_stops_at_the_limit() -> None:
    listings = [
        shortlisted("1", seller_id="s1"),
        shortlisted("2", seller_id="contacted"),
        shortlisted("3", seller_id="s1"),
        shortlisted("1", seller_id="s1"),
        shortlisted("4"),
        shortlisted("5"),
        shortlisted("6"),
    ]

    def already(listing_id: str, seller_id: str | None, seller_username: str | None) -> bool:
        return seller_id == "contacted"

    to_draft, skipped = choose_listings_to_draft(listings, already, limit=3)

    assert [listing.listing_id for listing in to_draft] == ["1", "4", "5"]
    assert [(listing.listing_id, reason) for listing, reason in skipped] == [
        ("2", SkipReason.LISTING_CONTACTED), ("3", SkipReason.SELLER_IN_BATCH)
    ]
    assert len(choose_listings_to_draft(listings, already, limit=None)[0]) == 4
