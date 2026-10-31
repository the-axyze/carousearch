"""OutreachService over a run folder in tmp_path, a FunctionModel drafter and the dry-run transport."""

import asyncio
import json
import random
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.agent.listings_db import RunListingsStore
from carousearch.config import OutreachSettings
from carousearch.outreach.browser import DryRunChatTransport
from carousearch.outreach.drafting import MessageDrafter
from carousearch.outreach.models import OfferRule, OutreachPolicy, SessionStatus, SkipReason
from carousearch.outreach.service import OutreachService, build_outreach_service, wait_for_login
from carousearch.outreach.store import SqliteOutreachStore

from outreach_examples import START

models.ALLOW_MODEL_REQUESTS = False

RUN_ID = "20261030-090000-abc123"
RULE = OfferRule(percent_below_asking=25)


def card(listing_id: str, seller_id: str, price: float | None = 200.0) -> dict:
    return {
        "id": listing_id, "title": f"Listing {listing_id}", "price": price, "seller_id": seller_id,
        "seller_username": f"user{seller_id}", "listing_url": f"https://www.carousell.sg/p/{listing_id}/",
        "description_snippet": f"Detail of {listing_id}",
    }


def make_run(data_dir: Path, cards: list[dict], kept_ids: list[str] | None = None) -> None:
    directory = data_dir / "runs" / RUN_ID
    store = RunListingsStore(directory)
    store.record_search_page(cards, None)
    store.close()
    if kept_ids is not None:
        kept = [{"listing_id": listing_id, "reason": "fits"} for listing_id in kept_ids]
        (directory / "answer.json").write_text(json.dumps({"kept": kept}), encoding="utf-8")


def drafting_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    prompt = next(
        part.content for message in messages if isinstance(message, ModelRequest)
        for part in message.parts if isinstance(part, UserPromptPart)
    )
    title = prompt.split("Listing title: ")[1].split("\n")[0]
    offer = prompt.split("Offer price: ")[1].split("\n")[0]
    return ModelResponse(parts=[ToolCallPart("message", {"body": f"Hi, saw {title}. Would {offer} be ok?"})])


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def service(tmp_path: Path) -> OutreachService:
    store = SqliteOutreachStore.open(tmp_path / "outreach.db")
    built = OutreachService(
        store=store,
        policy=OutreachPolicy(daily_cap=2, min_gap_seconds=30, max_gap_seconds=60),
        runs_parent=tmp_path,
        drafter_factory=lambda: MessageDrafter(FunctionModel(drafting_model)),
        transport_factory=DryRunChatTransport,
        clock=Clock(),
        sleep=lambda seconds: None,
        rng=random.Random(1),
    )
    yield built
    store.close()


def test_drafting_a_run_stores_a_draft_per_kept_listing(service: OutreachService, tmp_path: Path) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b"), card("3", "c", price=None)], kept_ids=["2", "3", "1"])

    batch = asyncio.run(service.draft_for_run(RUN_ID, RULE))

    assert batch.from_answer
    assert [(draft.listing_id, draft.offer_price) for draft in batch.drafts] == [("2", 150.0), ("1", 150.0)]
    assert batch.drafts[0].body == "Hi, saw Listing 2. Would $150 be ok?"
    assert [(listing.listing_id, reason) for listing, reason in batch.skipped] == [("3", SkipReason.NO_PRICE.value)]
    assert batch.send_preview == {} and batch.cap_remaining == 2
    assert [message.status for message in service.store.list_for_run(RUN_ID)] == ["drafted", "drafted"]


def test_drafting_again_replaces_the_unsent_drafts_and_passes_over_contacted_sellers(
    service: OutreachService, tmp_path: Path
) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b"), card("3", "b")])
    first = asyncio.run(service.draft_for_run(RUN_ID, RULE, limit=1))
    service.store.approve([first.drafts[0].id], START)
    service.store.mark_sent(first.drafts[0].id, START, None)

    second = asyncio.run(service.draft_for_run(RUN_ID, RULE))

    assert not second.from_answer
    assert [draft.listing_id for draft in second.drafts] == ["2"]
    assert [(listing.listing_id, reason) for listing, reason in second.skipped] == [
        ("1", SkipReason.LISTING_CONTACTED.value), ("3", SkipReason.SELLER_IN_BATCH.value)
    ]
    third = asyncio.run(service.draft_for_run(RUN_ID, RULE))
    statuses = [(message.listing_id, message.status) for message in service.store.list_for_run(RUN_ID)]
    assert statuses == [("1", "sent"), ("2", "skipped"), ("2", "drafted")]
    assert service.store.list_for_run(RUN_ID, ("skipped",))[0].skip_reason == SkipReason.REPLACED.value
    assert third.drafts[0].listing_id == "2"


def test_the_preview_marks_drafts_beyond_the_daily_cap(service: OutreachService, tmp_path: Path) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b"), card("3", "c")])

    batch = asyncio.run(service.draft_for_run(RUN_ID, RULE))

    assert batch.send_preview == {batch.drafts[2].id: SkipReason.DAILY_CAP.value}


def test_a_dry_run_sends_nothing_and_changes_nothing(service: OutreachService, tmp_path: Path) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b"), card("3", "c")])
    batch = asyncio.run(service.draft_for_run(RUN_ID, RULE))
    events = []

    report = service.approve_and_send(RUN_ID, None, dry_run=True, on_progress=events.append)

    assert report.dry_run and len(report.sent) == 2 and report.skipped == [(batch.drafts[2].id, SkipReason.DAILY_CAP)]
    assert [message.status for message in service.store.list_for_run(RUN_ID)] == ["drafted"] * 3
    assert service.store.sent_count_since(START - timedelta(days=1)) == 0
    assert any(event.kind == "waiting" for event in events)


def test_a_service_forced_to_dry_run_never_opens_the_browser_or_records_a_send(
    service: OutreachService, tmp_path: Path
) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b")])
    asyncio.run(service.draft_for_run(RUN_ID, RULE))
    service.force_dry_run = True
    service.transport_factory = lambda: pytest.fail("public-demo mode must never open the real browser")

    report = service.approve_and_send(RUN_ID, None, dry_run=False)

    assert report.dry_run and len(report.sent) == 2
    assert [message.status for message in service.store.list_for_run(RUN_ID)] == ["drafted"] * 2


def test_the_real_service_can_be_built_forced_to_dry_run(tmp_path: Path) -> None:
    built = build_outreach_service(OutreachSettings(), tmp_path, lambda: START, force_dry_run=True)
    try:
        assert built.force_dry_run
    finally:
        built.store.close()


def test_a_send_approves_the_chosen_drafts_and_records_outcomes(service: OutreachService, tmp_path: Path) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b"), card("3", "c")])
    transports: list[DryRunChatTransport] = []

    def build_transport() -> DryRunChatTransport:
        transports.append(DryRunChatTransport())
        return transports[-1]

    service.transport_factory = build_transport
    batch = asyncio.run(service.draft_for_run(RUN_ID, RULE))
    chosen = [batch.drafts[0].id, batch.drafts[2].id]

    report = service.approve_and_send(RUN_ID, chosen, dry_run=False)

    assert report.sent == chosen and not report.dry_run and report.cap_remaining == 0
    assert [listing_id for listing_id, _ in transports[0].sends] == ["1", "3"]
    statuses = [message.status for message in service.store.list_for_run(RUN_ID)]
    assert statuses == ["sent", "drafted", "sent"]


def test_owner_edits_replace_changed_bodies_only(service: OutreachService, tmp_path: Path) -> None:
    make_run(tmp_path, [card("1", "a"), card("2", "b")])
    first, second = asyncio.run(service.draft_for_run(RUN_ID, RULE)).drafts

    service.update_bodies({first.id: "  Hi, would $150 do?  ", second.id: "   "})

    bodies = [message.body for message in service.store.get_many([first.id, second.id])]
    assert bodies == ["Hi, would $150 do?", second.body]


def test_the_real_service_keeps_its_database_in_the_data_folder(tmp_path: Path) -> None:
    service = build_outreach_service(OutreachSettings(daily_cap=4), tmp_path, Clock())

    assert (tmp_path / "outreach.db").is_file() and service.policy.daily_cap == 4
    service.store.close()


class FakeLoginBrowser(DryRunChatTransport):
    def __init__(self, statuses: list[SessionStatus]) -> None:
        super().__init__()
        self.statuses = statuses
        self.opened = 0

    def open_login_page(self) -> None:
        self.opened += 1

    def current_status(self) -> SessionStatus:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def test_waiting_for_login_watches_until_logged_in() -> None:
    browser = FakeLoginBrowser([SessionStatus(False), SessionStatus(False), SessionStatus(True, "me")])
    sleeps: list[float] = []

    status = wait_for_login(browser, monotonic=lambda: 0.0, sleep=sleeps.append, poll_seconds=3)

    assert status.username == "me" and browser.opened == 1 and sleeps == [3, 3]


def test_waiting_for_login_gives_up_at_the_deadline() -> None:
    browser = FakeLoginBrowser([SessionStatus(False)])
    clock = iter(range(0, 1000, 100))

    status = wait_for_login(browser, monotonic=lambda: next(clock), sleep=lambda seconds: None, timeout_seconds=300)

    assert not status.logged_in
