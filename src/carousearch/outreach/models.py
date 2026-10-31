"""Outreach's data models, shared by the store, the drafter, the sender, the CLI and the web app.

Pure: Pydantic models and frozen dataclasses only.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

MessageStatus = Literal["drafted", "approved", "sent", "failed", "skipped"]


class OfferRule(BaseModel):
    """How the offer price follows from the asking price, and an optional hint for the drafter."""

    percent_below_asking: int = Field(ge=1, le=90)
    round_to: int = Field(default=50, ge=1)  # the offer is rounded down to a multiple of this, in SGD
    note_to_drafter: str = Field(default="", max_length=500)


class ShortlistedListing(BaseModel):
    """One listing of a run's shortlist, with what the run directory holds about it."""

    listing_id: str
    title: str
    price: float | None
    url: str | None = None
    seller_id: str | None = None
    seller_username: str | None = None
    condition: str | None = None
    description: str | None = None  # the full description when the run fetched details, else the card's snippet
    attributes: dict[str, Any] = Field(default_factory=dict)  # seller-entered fields
    extracted: dict[str, Any] = Field(default_factory=dict)  # values extract_fields read from the text
    kept_reason: str | None = None  # the agent's one line on why it kept the listing


class OutreachDraft(BaseModel):
    run_id: str  # the run directory's name, e.g. 20261027-092958-f1ec53
    listing_id: str
    seller_id: str | None
    seller_username: str | None
    listing_title: str
    listing_url: str | None = None
    listing_price: float | None
    offer_price: float | None  # None only for a row skipped before drafting
    body: str


class OutreachMessage(OutreachDraft):
    id: int
    status: MessageStatus
    created_at: datetime
    sent_at: datetime | None = None
    error: str | None = None
    skip_reason: str | None = None
    chat_url: str | None = None


class OutreachPolicy(BaseModel, frozen=True):
    """Limits that protect the owner's account: new conversations per day and the gap between two sends."""

    daily_cap: int = Field(default=10, ge=0)
    min_gap_seconds: float = Field(default=30, ge=0)
    max_gap_seconds: float = Field(default=60, ge=0)

    @model_validator(mode="after")
    def gap_bounds_in_order(self) -> "OutreachPolicy":
        if self.min_gap_seconds > self.max_gap_seconds:
            raise ValueError("the minimum gap between sends must not exceed the maximum")
        return self


class SkipReason(StrEnum):
    LISTING_CONTACTED = "this listing or its seller was messaged before"
    SELLER_IN_BATCH = "another listing from this seller is earlier in the batch"
    LISTING_IN_BATCH = "this listing is already earlier in the batch"
    DAILY_CAP = "the daily cap on new conversations is reached"
    NO_PRICE = "the listing has no plain price to offer below"
    DRAFT_FAILED = "the drafter gave no usable message"
    REPLACED = "replaced by a newer draft"


@dataclass(frozen=True)
class SessionStatus:
    """What the browser profile shows on carousell.sg."""

    logged_in: bool
    username: str | None = None
    blocked: bool = False  # a challenge or block page: stop everything
    detail: str = ""  # one plain line for the owner


SendResult = Literal["sent", "unconfirmed", "failed"]


@dataclass(frozen=True)
class SendOutcome:
    """One send's result. "unconfirmed": send was pressed but the message was not seen in the thread afterwards,
    so it may have gone out; it counts as sent for de-duplication and stops the batch."""

    result: SendResult
    chat_url: str | None = None
    error: str | None = None
    failed_selector: str | None = None  # the selector name that found nothing, when that was the failure

    @property
    def possibly_delivered(self) -> bool:
        return self.result in ("sent", "unconfirmed")


@dataclass(frozen=True)
class SendReport:
    """What one batch did."""

    sent: list[int] = field(default_factory=list)  # message ids
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (message id, reason)
    failed: list[tuple[int, str]] = field(default_factory=list)  # (message id, error)
    aborted: str | None = None  # why the batch stopped before sending anything, e.g. not logged in
    stopped: str | None = None  # why it stopped part-way, e.g. a send failed
    not_attempted: list[int] = field(default_factory=list)  # approved but left for a later batch
    cap_remaining: int = 0  # new conversations still allowed in the last 24 hours after this batch
    dry_run: bool = False
