"""Drafting: one short offer message per shortlisted listing, written by the worker model.

Pure: compute_offer_price, format_offer, build_drafter_prompt and body_problem. I/O: MessageDrafter calls the model
provider through the injected Pydantic AI Model (tests pass a FunctionModel), and draft_messages awaits it for each
listing.
"""

import asyncio
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput, UsageLimits
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models import Model

from carousearch.outreach.models import OfferRule, OutreachDraft, ShortlistedListing, SkipReason
from carousearch.outreach.prompt import DRAFTER_INSTRUCTIONS

# Smaller rounding steps, tried when the owner's step is coarse for a small price.
FALLBACK_ROUNDING_STEPS = (10, 5, 1)
# The rounding step may cut at most this share of the asking price beyond the owner's percentage.
MAX_ROUNDING_SHARE = 0.10
MAX_DESCRIPTION_CHARACTERS = 1500
MAX_BODY_CHARACTERS = 500
DRAFTER_RETRIES = 2
DRAFTER_REQUEST_LIMIT = 4

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?65[ -]?)?[689]\d{3}[ -]?\d{4}(?!\d)")
LINK_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


class DraftedMessage(BaseModel):
    body: str = Field(description="The chat message to the seller, 2 to 4 sentences, nothing else.")


# --- the offer price (pure) ---------------------------------------------------------------------------------------


def compute_offer_price(asking_price: float | None, rule: OfferRule) -> float | None:
    """The asking price less the rule's percentage, rounded down to a multiple of `round_to`. When that step is
    more than a tenth of the asking price, the largest of 10, 5 or 1 that is not is used instead, so a small price
    is not cut far deeper than asked. None when there is no positive asking price."""
    if asking_price is None or asking_price <= 0:
        return None
    target = asking_price * (100 - rule.percent_below_asking) / 100
    step = rounding_step(asking_price, rule.round_to)
    offer = math.floor(target / step) * step
    return float(offer) if offer > 0 else round(target, 2)


def rounding_step(asking_price: float, round_to: int) -> int:
    allowed = asking_price * MAX_ROUNDING_SHARE
    for step in (round_to, *FALLBACK_ROUNDING_STEPS):
        if step <= allowed and step <= round_to:
            return step
    return 1


def format_offer(amount: float) -> str:
    """'$600', '$1,050', '$22.50'."""
    return f"${amount:,.0f}" if float(amount).is_integer() else f"${amount:,.2f}"


# --- the prompt (pure) --------------------------------------------------------------------------------------------


def build_drafter_prompt(listing: ShortlistedListing, offer_price: float, note_to_drafter: str = "") -> str:
    lines = [f"Listing title: {listing.title}"]
    if listing.price is not None:
        lines.append(f"Asking price: {format_offer(listing.price)}")
    if listing.condition:
        lines.append(f"Condition: {listing.condition}")
    lines += field_lines("Seller-entered fields", listing.attributes)
    lines += field_lines("Values read from the description (1 means yes, 0 means no)", listing.extracted)
    if listing.kept_reason:
        lines.append(f"Why the buyer shortlisted it: {listing.kept_reason}")
    if listing.description:
        description = listing.description.strip()[:MAX_DESCRIPTION_CHARACTERS]
        lines.append(f"Description (the seller's text):\n<<<\n{description}\n>>>")
    lines.append(f"\nOffer price: {format_offer(offer_price)}")
    if note_to_drafter.strip():
        lines.append(f"Buyer's note for this batch: {note_to_drafter.strip()}")
    return "\n".join(lines)


def field_lines(heading: str, values: dict[str, object]) -> list[str]:
    known = [f"{name}: {value}" for name, value in values.items() if value not in (None, "", [], {})]
    return [f"{heading}: " + "; ".join(known)] if known else []


def body_problem(body: str, offer_price: float) -> str | None:
    """Why a drafted body cannot be sent as it is, or None when it is fine."""
    text = body.strip()
    if not text:
        return "The message is empty."
    if len(text) > MAX_BODY_CHARACTERS:
        return f"The message is longer than {MAX_BODY_CHARACTERS} characters; keep it to 2 to 4 sentences."
    if EMAIL_PATTERN.search(text) or PHONE_PATTERN.search(text) or LINK_PATTERN.search(text):
        return "The message must not contain an email address, contact number or link."
    if not mentions_amount(text, offer_price):
        return f"The message must state the offer price {format_offer(offer_price)}."
    return None


def mentions_amount(text: str, amount: float) -> bool:
    plain = format_offer(amount).removeprefix("$")
    return plain in text or plain.replace(",", "") in text


# --- the model call (I/O) -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftContext:
    offer_price: float


class MessageDrafter:
    """One structured call per listing; an answer that breaks the rules in body_problem is sent back to the model."""

    def __init__(self, model: Model) -> None:
        self._agent: Agent[DraftContext, DraftedMessage] = Agent(
            model,
            deps_type=DraftContext,
            output_type=ToolOutput(DraftedMessage, name="message"),
            instructions=DRAFTER_INSTRUCTIONS,
            retries=DRAFTER_RETRIES,
        )
        self._agent.output_validator(check_body)

    async def draft(self, listing: ShortlistedListing, offer_price: float, note_to_drafter: str = "") -> str:
        result = await self._agent.run(
            build_drafter_prompt(listing, offer_price, note_to_drafter),
            deps=DraftContext(offer_price),
            usage_limits=UsageLimits(request_limit=DRAFTER_REQUEST_LIMIT),
        )
        return result.output.body.strip()


def check_body(context: RunContext[DraftContext], output: DraftedMessage) -> DraftedMessage:
    problem = body_problem(output.body, context.deps.offer_price)
    if problem is not None:
        raise ModelRetry(problem)
    return output


@dataclass(frozen=True)
class DraftingOutcome:
    drafts: list[OutreachDraft]
    skipped: list[tuple[ShortlistedListing, SkipReason]]


async def draft_messages(
    run_id: str, listings: Sequence[ShortlistedListing], rule: OfferRule, drafter: MessageDrafter
) -> DraftingOutcome:
    """A draft for each listing with a plain price, drafted concurrently, in the listings' order."""
    priced = [(listing, compute_offer_price(listing.price, rule)) for listing in listings]
    skipped = [(listing, SkipReason.NO_PRICE) for listing, offer in priced if offer is None]
    to_draft = [(listing, offer) for listing, offer in priced if offer is not None]
    bodies = await asyncio.gather(
        *(draft_one(drafter, listing, offer, rule.note_to_drafter) for listing, offer in to_draft)
    )
    drafts = []
    for (listing, offer), body in zip(to_draft, bodies, strict=True):
        if body is None:
            skipped.append((listing, SkipReason.DRAFT_FAILED))
        else:
            drafts.append(draft_for(run_id, listing, offer, body))
    return DraftingOutcome(drafts, skipped)


async def draft_one(drafter: MessageDrafter, listing: ShortlistedListing, offer: float, note: str) -> str | None:
    try:
        return await drafter.draft(listing, offer, note)
    except (UnexpectedModelBehavior, UsageLimitExceeded):  # the model kept breaking the rules
        return None


def draft_for(run_id: str, listing: ShortlistedListing, offer_price: float | None, body: str) -> OutreachDraft:
    return OutreachDraft(
        run_id=run_id,
        listing_id=listing.listing_id,
        seller_id=listing.seller_id,
        seller_username=listing.seller_username,
        listing_title=listing.title,
        listing_url=listing.url,
        listing_price=listing.price,
        offer_price=offer_price,
        body=body,
    )
