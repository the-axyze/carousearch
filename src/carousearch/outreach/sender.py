"""Sending one approved batch: apply the policy, check the session once, then send one message at a time with a
random gap between sends, recording every outcome. The first failure stops the batch; what was not attempted
stays approved for a later batch.

I/O only through what is injected: the store, the chat transport, the clock and the sleep function. Tests pass a
DryRunChatTransport, a fake clock, a recording sleep and a seeded random.Random.
"""

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from carousearch.outreach.browser import ChatTransport
from carousearch.outreach.models import OutreachMessage, OutreachPolicy, SendReport
from carousearch.outreach.policy import cap_remaining, next_gap_seconds, select_sendable

DAILY_WINDOW = timedelta(hours=24)  # the daily cap counts sends in the last 24 hours

ProgressKind = Literal["skipped", "aborted", "waiting", "sending", "sent", "failed", "finished"]


@dataclass(frozen=True)
class ProgressEvent:
    kind: ProgressKind
    text: str
    message_id: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


class OutreachRecorder(Protocol):
    """The store's reads and writes the sender uses (SqliteOutreachStore; a dry run passes a read-only wrapper)."""

    def already_contacted(self, listing_id: str, seller_id: str | None, seller_username: str | None = None) -> bool: ...

    def sent_count_since(self, since: datetime) -> int: ...

    def mark_sent(self, message_id: int, sent_at: datetime, chat_url: str | None, note: str | None = None) -> None: ...

    def mark_failed(self, message_id: int, error: str) -> None: ...

    def mark_skipped(self, message_ids: Sequence[int], reason: str) -> None: ...


@dataclass
class OutreachSender:
    store: OutreachRecorder
    transport: ChatTransport
    policy: OutreachPolicy
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    rng: random.Random = field(default_factory=random.Random)
    dry_run: bool = False  # only words the progress and the report as a rehearsal; the transport decides what is sent

    def send_batch(
        self, messages: Sequence[OutreachMessage], on_progress: ProgressCallback = lambda event: None
    ) -> SendReport:
        """Send `messages` (approved, in order) under the policy; see the module docstring."""
        sent_today = self.store.sent_count_since(self.clock() - DAILY_WINDOW)
        to_send, skipped = select_sendable(messages, self.store.already_contacted, sent_today, self.policy)
        report_skipped = []
        for message, reason in skipped:
            self.store.mark_skipped([message.id], reason.value)
            report_skipped.append((message.id, reason.value))
            on_progress(ProgressEvent("skipped", f"Skipped {message.listing_title}: {reason.value}", message.id))
        if not to_send:
            return self._finish(SendReport(skipped=report_skipped, dry_run=self.dry_run), sent_today, on_progress)

        session = self.transport.status()
        if not session.logged_in or session.blocked:
            on_progress(ProgressEvent("aborted", f"Nothing sent. {session.detail}"))
            report = SendReport(
                skipped=report_skipped, aborted=session.detail, not_attempted=[message.id for message in to_send],
                dry_run=self.dry_run,
            )
            return self._finish(report, sent_today, on_progress)
        return self._send_in_turn(to_send, report_skipped, sent_today, on_progress)

    def _send_in_turn(
        self,
        to_send: Sequence[OutreachMessage],
        report_skipped: list[tuple[int, str]],
        sent_today: int,
        on_progress: ProgressCallback,
    ) -> SendReport:
        sent: list[int] = []
        failed: list[tuple[int, str]] = []
        stopped = None
        for position, message in enumerate(to_send):
            if position > 0:
                gap = next_gap_seconds(self.policy, self.rng)
                verb = "Would wait" if self.dry_run else "Waiting"
                on_progress(ProgressEvent("waiting", f"{verb} {gap:.0f} s before the next message"))
                self.sleep(gap)
            verb = "Would send" if self.dry_run else "Sending"
            sending_line = f"{verb} to {seller_label(message)}: {message.listing_title}"
            on_progress(ProgressEvent("sending", sending_line, message.id))
            outcome = self.transport.send(message.listing_id, message.body)
            if outcome.result == "sent":
                self.store.mark_sent(message.id, self.clock(), outcome.chat_url)
                sent.append(message.id)
                sent_line = f"Dry run, not sent: {seller_label(message)}" if self.dry_run else f"Sent to {seller_label(message)}"
                on_progress(ProgressEvent("sent", sent_line, message.id))
                continue
            error = outcome.error or "the send failed"
            if outcome.result == "unconfirmed":  # it may have gone out: never send it again
                self.store.mark_sent(message.id, self.clock(), outcome.chat_url, note=f"unconfirmed: {error}")
                sent.append(message.id)
            else:
                self.store.mark_failed(message.id, error)
                failed.append((message.id, error))
            stopped = f"Stopped after {seller_label(message)}: {error}"
            on_progress(ProgressEvent("failed", stopped, message.id))
            report = SendReport(
                sent=sent, skipped=report_skipped, failed=failed, stopped=stopped,
                not_attempted=[later.id for later in to_send[position + 1:]], dry_run=self.dry_run,
            )
            return self._finish(report, sent_today + len(sent), on_progress)
        report = SendReport(sent=sent, skipped=report_skipped, failed=failed, dry_run=self.dry_run)
        return self._finish(report, sent_today + len(sent), on_progress)

    def _finish(self, report: SendReport, sent_today: int, on_progress: ProgressCallback) -> SendReport:
        finished = replace(report, cap_remaining=cap_remaining(self.policy, sent_today))
        on_progress(ProgressEvent("finished", finished_line(finished)))
        return finished


def seller_label(message: OutreachMessage) -> str:
    return message.seller_username or f"listing {message.listing_id}"


def finished_line(report: SendReport) -> str:
    parts = [f"{len(report.sent)} {'would be sent' if report.dry_run else 'sent'}", f"{len(report.skipped)} skipped"]
    if report.failed:
        parts.append(f"{len(report.failed)} failed")
    if report.not_attempted:
        parts.append(f"{len(report.not_attempted)} left approved for later")
    head = "Dry run done (nothing sent or recorded): " if report.dry_run else "Done: "
    return head + ", ".join(parts) + f". {report.cap_remaining} more new conversations allowed in the next 24 hours."
