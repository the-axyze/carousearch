"""Drafting: the offer price, the drafter's prompt, the body checks, and the model call through a FunctionModel."""

import asyncio

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.outreach.drafting import (
    MessageDrafter,
    body_problem,
    build_drafter_prompt,
    compute_offer_price,
    draft_messages,
    format_offer,
)
from carousearch.outreach.models import OfferRule, SkipReason
from carousearch.outreach.prompt import DRAFTER_INSTRUCTIONS

from outreach_examples import shortlisted

models.ALLOW_MODEL_REQUESTS = False


@pytest.mark.parametrize(
    ("asking", "percent", "round_to", "offer"),
    [
        (1000, 25, 50, 750.0),
        (830, 25, 50, 600.0),  # 622.50 rounded down to a multiple of 50
        (120, 25, 50, 90.0),  # 50 is too coarse for 120: steps of 10
        (45, 25, 50, 33.0),  # steps of 1
        (64, 20, 5, 50.0),  # 51.20 rounded down to a multiple of 5
        (40, 20, 5, 32.0),  # 5 is more than a tenth of 40: steps of 1
        (300, 30, 5, 210.0),
        (2, 50, 50, 1.0),
        (1.2, 50, 50, 0.6),  # below a dollar: the exact amount
    ],
)
def test_the_offer_is_the_percentage_below_rounded_down(asking: float, percent: int, round_to: int, offer: float) -> None:
    assert compute_offer_price(asking, OfferRule(percent_below_asking=percent, round_to=round_to)) == offer


@pytest.mark.parametrize("asking", [None, 0, -5])
def test_no_offer_without_a_positive_price(asking: float | None) -> None:
    assert compute_offer_price(asking, OfferRule(percent_below_asking=25)) is None


def test_offers_are_written_like_prices() -> None:
    assert [format_offer(600), format_offer(1050.0), format_offer(22.5)] == ["$600", "$1,050", "$22.50"]


def test_the_prompt_carries_the_listings_details_and_the_offer() -> None:
    listing = shortlisted(
        "1", condition="Lightly used", attributes={"colour": "brown", "empty": None}, extracted={"height_cm": 60},
        kept_reason="lowest price with a solid top",
    )

    prompt = build_drafter_prompt(listing, 90.0, "mention I can collect this weekend")

    assert "Listing title: Listing 1 in walnut finish" in prompt
    assert "Asking price: $120" in prompt and "Condition: Lightly used" in prompt
    assert "Seller-entered fields: colour: brown" in prompt and "empty" not in prompt
    assert "Values read from the description: height_cm: 60" in prompt
    assert "Why the buyer shortlisted it: lowest price with a solid top" in prompt
    assert "<<<\nBarely used, small scratch on the left side." in prompt
    assert "Offer price: $90" in prompt
    assert "Buyer's note for this batch: mention I can collect this weekend" in prompt


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("Hi! Like the walnut finish. Would you take $90?", None),
        ("Hi, would you do 1050 for it?", None),
        ("", "empty"),
        ("Hi, would you take $80?", "$90"),
        ("Hi, $90 ok? Call me at 9123 4567.", "contact number"),
        ("Hi, $90 ok? Mail me: me@example.com", "email"),
        ("Hi, $90 ok? See https://example.com", "link"),
        ("Hi, $90? " + "x" * 600, "longer"),
    ],
)
def test_body_checks(body: str, problem: str | None) -> None:
    offer = 1050.0 if "1050" in body else 90.0
    found = body_problem(body, offer)
    assert (found is None) if problem is None else (problem in found)


def message_call(body: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("message", {"body": body})])


class RecordingDrafter:
    """A FunctionModel function that answers with `bodies` in turn and keeps every prompt it saw."""

    def __init__(self, *bodies: str) -> None:
        self.bodies = list(bodies)
        self.prompts: list[str] = []
        self.instructions: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.prompts += [part.content for part in request.parts if isinstance(part, UserPromptPart)]
        self.instructions.append(request.instructions)
        return message_call(self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0])


def test_the_drafter_sends_the_listing_and_offer_and_returns_the_body() -> None:
    model = RecordingDrafter("  Hi! The walnut finish looks great. Would you take $90 for it?  ")

    body = asyncio.run(MessageDrafter(FunctionModel(model)).draft(shortlisted("1"), 90.0))

    assert body == "Hi! The walnut finish looks great. Would you take $90 for it?"
    assert "Listing 1 in walnut finish" in model.prompts[0] and "Offer price: $90" in model.prompts[0]
    assert model.instructions[0] == DRAFTER_INSTRUCTIONS.strip()


def test_a_body_that_breaks_the_rules_is_sent_back_once_and_fixed() -> None:
    model = RecordingDrafter("Hi, would you take $80?", "Hi, small scratch is fine by me. Is $90 ok?")

    body = asyncio.run(MessageDrafter(FunctionModel(model)).draft(shortlisted("1"), 90.0))

    assert body == "Hi, small scratch is fine by me. Is $90 ok?"


def first_user_prompt(messages: list[ModelMessage]) -> str:
    return next(
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    )


def test_draft_messages_drafts_priced_listings_and_skips_the_rest() -> None:
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = first_user_prompt(messages)
        offer = prompt.split("Offer price: ")[1].split("\n")[0]
        if "Listing 3" in prompt:
            return message_call("no price here")  # never fixed: drafting gives up on it
        return message_call(f"Hi, saw the scratch note. Would {offer} work?")

    listings = [shortlisted("1"), shortlisted("2", price=None), shortlisted("3"), shortlisted("4", price=1000)]
    rule = OfferRule(percent_below_asking=25)

    outcome = asyncio.run(draft_messages("run-1", listings, rule, MessageDrafter(FunctionModel(answer))))

    assert [(draft.listing_id, draft.offer_price) for draft in outcome.drafts] == [("1", 90.0), ("4", 750.0)]
    assert outcome.drafts[1].body == "Hi, saw the scratch note. Would $750 work?"
    assert outcome.drafts[0].run_id == "run-1" and outcome.drafts[0].seller_id == "seller-1"
    assert [(listing.listing_id, reason) for listing, reason in outcome.skipped] == [
        ("2", SkipReason.NO_PRICE), ("3", SkipReason.DRAFT_FAILED)
    ]
