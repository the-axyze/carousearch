"""OutreachSender with a real store in tmp_path, the dry-run transport, a fake clock and a recording sleep."""

import random
from datetime import timedelta
from pathlib import Path

import pytest

from carousearch.outreach.browser import DryRunChatTransport
from carousearch.outreach.models import OutreachMessage, OutreachPolicy, SendOutcome, SessionStatus, SkipReason
from carousearch.outreach.sender import OutreachSender, ProgressEvent
from carousearch.outreach.store import SqliteOutreachStore

from outreach_examples import START, draft

POLICY = OutreachPolicy(daily_cap=3, min_gap_seconds=30, max_gap_seconds=60)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self):
        return self.now


class RecordingSleep:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> SqliteOutreachStore:
    opened = SqliteOutreachStore.open(tmp_path / "outreach.db")
    yield opened
    opened.close()


def approved(store: SqliteOutreachStore, *drafts) -> list[OutreachMessage]:
    added = store.add_drafts(list(drafts), START)
    store.approve([message.id for message in added], START)
    return store.get_many([message.id for message in added])


def make_sender(store, transport, clock=None):
    clock = clock or Clock()
    sleep = RecordingSleep(clock)
    return OutreachSender(store, transport, POLICY, clock, sleep, random.Random(3)), sleep


def test_a_batch_is_sent_in_order_with_a_gap_between_sends_and_recorded(store: SqliteOutreachStore) -> None:
    messages = approved(store, draft("1"), draft("2"), draft("3"))
    transport = DryRunChatTransport()
    sender, sleep = make_sender(store, transport)
    events: list[ProgressEvent] = []

    report = sender.send_batch(messages, events.append)

    assert [listing_id for listing_id, _ in transport.sends] == ["1", "2", "3"]
    assert transport.status_checks == 1
    assert len(sleep.waits) == 2 and all(30 <= wait <= 60 for wait in sleep.waits)
    assert report.sent == [message.id for message in messages] and report.cap_remaining == 0
    stored = store.get_many(report.sent)
    assert [message.status for message in stored] == ["sent"] * 3
    assert stored[0].chat_url == "dry-run://chat/1" and stored[0].sent_at == START
    assert stored[1].sent_at == START + timedelta(seconds=sleep.waits[0])  # sent after the first gap
    assert [event.kind for event in events] == [
        "sending", "sent", "waiting", "sending", "sent", "waiting", "sending", "sent", "finished"
    ]
    assert events[-1].text.startswith("Done: 3 sent, 0 skipped.")


def test_nothing_is_sent_when_not_logged_in(store: SqliteOutreachStore) -> None:
    messages = approved(store, draft("1"), draft("2"))
    transport = DryRunChatTransport(session=SessionStatus(logged_in=False, detail="Not logged in."))
    sender, sleep = make_sender(store, transport)
    events: list[ProgressEvent] = []

    report = sender.send_batch(messages, events.append)

    assert transport.sends == [] and sleep.waits == []
    assert report.aborted == "Not logged in." and report.not_attempted == [message.id for message in messages]
    assert [message.status for message in store.get_many(report.not_attempted)] == ["approved", "approved"]
    assert events[0].kind == "aborted" and "Not logged in." in events[0].text


def test_a_block_page_aborts_too(store: SqliteOutreachStore) -> None:
    transport = DryRunChatTransport(session=SessionStatus(logged_in=True, blocked=True, detail="Challenge."))
    sender, _ = make_sender(store, transport)

    report = sender.send_batch(approved(store, draft("1")))

    assert report.aborted == "Challenge." and transport.sends == []


def test_the_first_failure_stops_the_batch(store: SqliteOutreachStore) -> None:
    messages = approved(store, draft("1"), draft("2"), draft("3"))
    transport = DryRunChatTransport(fail_on_listing_ids=frozenset({"2"}))
    sender, _ = make_sender(store, transport)

    report = sender.send_batch(messages)

    first, second, third = (message.id for message in messages)
    assert [listing_id for listing_id, _ in transport.sends] == ["1", "2"]
    assert report.sent == [first] and report.failed == [(second, "dry-run failure")]
    assert report.not_attempted == [third] and "dry-run failure" in report.stopped
    assert [message.status for message in store.get_many([first, second, third])] == ["sent", "failed", "approved"]


class UnconfirmedTransport(DryRunChatTransport):
    def send(self, listing_id: str, body: str) -> SendOutcome:
        self.sends.append((listing_id, body))
        return SendOutcome("unconfirmed", chat_url="https://www.carousell.sg/inbox/9/", error="not seen")


def test_an_unconfirmed_send_counts_as_sent_and_stops_the_batch(store: SqliteOutreachStore) -> None:
    messages = approved(store, draft("1"), draft("2"))
    sender, _ = make_sender(store, UnconfirmedTransport())

    report = sender.send_batch(messages)

    (stored,) = store.get_many([messages[0].id])
    assert stored.status == "sent" and stored.error == "unconfirmed: not seen"
    assert report.sent == [messages[0].id] and report.not_attempted == [messages[1].id]
    assert store.already_contacted("1", None)


def test_skips_are_recorded_with_their_reasons_before_anything_is_sent(store: SqliteOutreachStore) -> None:
    earlier = approved(store, draft("old", seller_id="known"))
    store.mark_sent(earlier[0].id, START - timedelta(hours=1), None)
    messages = approved(
        store,
        draft("1", seller_id="known"),  # seller contacted before
        draft("2", seller_id="s"),
        draft("3", seller_id="s"),  # same seller as the one before
        draft("4"),
        draft("5"),  # beyond the cap: 1 sent in the last 24 hours, 2 more allowed
    )
    transport = DryRunChatTransport()
    sender, _ = make_sender(store, transport)
    events: list[ProgressEvent] = []

    report = sender.send_batch(messages, events.append)

    ids = [message.id for message in messages]
    assert [listing_id for listing_id, _ in transport.sends] == ["2", "4"]
    assert report.skipped == [
        (ids[0], SkipReason.LISTING_CONTACTED.value),
        (ids[2], SkipReason.SELLER_IN_BATCH.value),
        (ids[4], SkipReason.DAILY_CAP.value),
    ]
    stored = {message.id: message for message in store.get_many(ids)}
    assert stored[ids[4]].status == "skipped" and stored[ids[4]].skip_reason == SkipReason.DAILY_CAP.value
    assert [event.kind for event in events][:3] == ["skipped"] * 3
    assert report.cap_remaining == 0


def test_a_batch_with_nothing_sendable_never_opens_the_browser(store: SqliteOutreachStore) -> None:
    earlier = approved(store, draft("1"))
    store.mark_sent(earlier[0].id, START, None)
    transport = DryRunChatTransport()
    sender, _ = make_sender(store, transport)

    report = sender.send_batch(approved(store, draft("1")))

    assert transport.status_checks == 0 and report.sent == [] and len(report.skipped) == 1
