"""Outreach's wiring: the store, the policy, the drafter and the browser built from settings, and the three things
the CLI and the web app do with them: draft for a run, preview what a send would skip, approve and send.

I/O: reads the run directory, writes data/outreach.db, calls the drafting model, and drives the browser (or the
dry-run transport). Pydantic AI and Playwright are imported only when drafting or a real send starts.
"""

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from carousearch.config import OutreachSettings
from carousearch.outreach.browser import ChatTransport, DryRunChatTransport, PlaywrightChatTransport
from carousearch.outreach.models import (
    OfferRule,
    OutreachMessage,
    OutreachPolicy,
    SendReport,
    SessionStatus,
    ShortlistedListing,
    SkipReason,
)
from carousearch.outreach.policy import cap_remaining, choose_listings_to_draft, select_sendable
from carousearch.outreach.run_shortlist import load_run_shortlist
from carousearch.outreach.sender import DAILY_WINDOW, OutreachSender, ProgressCallback
from carousearch.outreach.store import OUTREACH_DATABASE_FILE_NAME, SqliteOutreachStore

if TYPE_CHECKING:
    from carousearch.outreach.drafting import MessageDrafter

LOGIN_TIMEOUT_SECONDS = 300
LOGIN_POLL_SECONDS = 3


@dataclass(frozen=True)
class DraftBatch:
    run_id: str
    drafts: list[OutreachMessage]  # stored, status "drafted"
    skipped: list[tuple[ShortlistedListing, str]]  # passed over before or during drafting, with the reason
    send_preview: dict[int, str]  # draft id -> why a send now would skip it (contacted since, the daily cap)
    from_answer: bool  # False: the run had no answer.json and its listings stood in for the shortlist
    cap_remaining: int


class ReadOnlyRecorder:
    """The store for a dry run: reads pass through, writes are dropped, so a dry run never marks anything sent."""

    def __init__(self, store: SqliteOutreachStore) -> None:
        self._store = store

    def already_contacted(self, listing_id: str, seller_id: str | None, seller_username: str | None = None) -> bool:
        return self._store.already_contacted(listing_id, seller_id, seller_username)

    def sent_count_since(self, since: datetime) -> int:
        return self._store.sent_count_since(since)

    def mark_sent(self, message_id: int, sent_at: datetime, chat_url: str | None, note: str | None = None) -> None:
        pass

    def mark_failed(self, message_id: int, error: str) -> None:
        pass

    def mark_skipped(self, message_ids: Sequence[int], reason: str) -> None:
        pass


@dataclass
class OutreachService:
    store: SqliteOutreachStore
    policy: OutreachPolicy
    runs_parent: Path  # the data folder; runs live in <runs_parent>/runs/<run id>/
    drafter_factory: "Callable[[], MessageDrafter]"
    transport_factory: Callable[[], ChatTransport]  # the real browser; a dry run uses DryRunChatTransport
    clock: Callable[[], datetime]
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    async def draft_for_run(self, run_id: str, rule: OfferRule, limit: int | None = None) -> DraftBatch:
        """Draft one message per shortlisted listing (up to `limit`), replacing the run's earlier unsent drafts."""
        from carousearch.outreach.drafting import draft_messages

        shortlist = load_run_shortlist(self.runs_parent, run_id)
        to_draft, passed_over = choose_listings_to_draft(shortlist.listings, self.store.already_contacted, limit)
        outcome = await draft_messages(run_id, to_draft, rule, self.drafter_factory())
        earlier = self.store.list_for_run(run_id, ("drafted",))
        self.store.mark_skipped([message.id for message in earlier], SkipReason.REPLACED.value)
        drafts = self.store.add_drafts(outcome.drafts, self.clock())
        skipped = [(listing, reason.value) for listing, reason in [*passed_over, *outcome.skipped]]
        return DraftBatch(
            run_id=run_id, drafts=drafts, skipped=skipped, send_preview=self.send_preview(drafts),
            from_answer=shortlist.from_answer, cap_remaining=self.cap_remaining(),
        )

    def send_preview(self, messages: Sequence[OutreachMessage]) -> dict[int, str]:
        """Why sending `messages` now would skip each one that would be skipped."""
        _, skipped = select_sendable(messages, self.store.already_contacted, self.sent_today(), self.policy)
        return {message.id: reason.value for message, reason in skipped}

    def approve_and_send(
        self,
        run_id: str,
        message_ids: Sequence[int] | None,
        dry_run: bool,
        on_progress: ProgressCallback = lambda event: None,
        sleep: Callable[[float], None] | None = None,
    ) -> SendReport:
        """Approve the chosen drafts (all of the run's unsent ones when `message_ids` is None) and send them. A dry
        run changes nothing in the store, waits no time and sends nothing. `sleep` replaces the service's own for
        this batch (the web app passes one that a shutdown or the Stop button interrupts)."""
        candidates = self.store.list_for_run(run_id, ("drafted", "approved"))
        if message_ids is not None:
            chosen = set(message_ids)
            candidates = [message for message in candidates if message.id in chosen]
        if dry_run:
            sender = OutreachSender(
                ReadOnlyRecorder(self.store), DryRunChatTransport(), self.policy, self.clock, lambda seconds: None,
                self.rng, dry_run=True,
            )
            return sender.send_batch(candidates, on_progress)
        self.store.approve([message.id for message in candidates if message.status == "drafted"], self.clock())
        approved = self.store.get_many([message.id for message in candidates])
        transport = self.transport_factory()
        try:
            sender = OutreachSender(self.store, transport, self.policy, self.clock, sleep or self.sleep, self.rng)
            return sender.send_batch(approved, on_progress)
        finally:
            transport.close()

    def update_bodies(self, bodies: dict[int, str]) -> None:
        """The owner's edits from the review table; unchanged or empty bodies are left alone."""
        current = {message.id: message.body for message in self.store.get_many(list(bodies))}
        for message_id, body in bodies.items():
            if body.strip() and body.strip() != current.get(message_id):
                self.store.update_body(message_id, body.strip())

    def sent_today(self) -> int:
        return self.store.sent_count_since(self.clock() - DAILY_WINDOW)

    def cap_remaining(self) -> int:
        return cap_remaining(self.policy, self.sent_today())


def build_outreach_service(
    settings: OutreachSettings, data_dir: Path, clock: Callable[[], datetime]
) -> OutreachService:
    """The real service: data/outreach.db, the drafting model, and the browser profile."""
    policy = OutreachPolicy(
        daily_cap=settings.daily_cap, min_gap_seconds=settings.min_gap_seconds, max_gap_seconds=settings.max_gap_seconds
    )

    def build_drafter() -> "MessageDrafter":
        from carousearch.language_models import build_model
        from carousearch.outreach.drafting import MessageDrafter

        return MessageDrafter(build_model(settings.draft_model, settings.keys))

    return OutreachService(
        store=SqliteOutreachStore.open(data_dir / OUTREACH_DATABASE_FILE_NAME),
        policy=policy,
        runs_parent=data_dir,
        drafter_factory=build_drafter,
        transport_factory=lambda: build_browser(settings, data_dir),
        clock=clock,
    )


def build_browser(settings: OutreachSettings, data_dir: Path, headed: bool | None = None) -> PlaywrightChatTransport:
    return PlaywrightChatTransport(settings.profile_dir_in(data_dir), headed=settings.headed if headed is None else headed)


# --- the one-time login -------------------------------------------------------------------------------------------


class LoginBrowser(ChatTransport, Protocol):
    """What the login wait needs beyond a transport (PlaywrightChatTransport has both)."""

    def open_login_page(self) -> None: ...

    def current_status(self) -> SessionStatus: ...


def wait_for_login(
    browser: LoginBrowser,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
    poll_seconds: float = LOGIN_POLL_SECONDS,
) -> SessionStatus:
    """Show the login page and watch it, without navigating, until the owner is logged in or the time runs out.
    The owner types everything and answers any check Carousell shows; this only looks."""
    browser.open_login_page()
    deadline = monotonic() + timeout_seconds
    status = browser.current_status()
    while not status.logged_in and monotonic() < deadline:
        sleep(poll_seconds)
        status = browser.current_status()
    return status
