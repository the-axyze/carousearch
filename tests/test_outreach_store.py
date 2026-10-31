"""SqliteOutreachStore over a temporary database file."""

from datetime import timedelta
from pathlib import Path

import pytest

from carousearch.outreach.store import SqliteOutreachStore

from outreach_examples import START, draft


@pytest.fixture
def store(tmp_path: Path) -> SqliteOutreachStore:
    opened = SqliteOutreachStore.open(tmp_path / "outreach.db")
    yield opened
    opened.close()


def test_drafts_are_stored_with_ids_and_listed_per_run(store: SqliteOutreachStore) -> None:
    added = store.add_drafts([draft("1"), draft("2"), draft("3", run_id="other-run")], START)

    assert [message.status for message in added] == ["drafted"] * 3
    assert [message.listing_id for message in store.list_for_run("20261030-090000-run1")] == ["1", "2"]
    assert added[0].created_at == START and added[0].offer_price == 75.0
    assert [message.listing_id for message in store.list_recent()] == ["3", "2", "1"]


def test_approve_send_fail_and_skip_move_statuses(store: SqliteOutreachStore) -> None:
    first, second, third = store.add_drafts([draft("1"), draft("2"), draft("3")], START)

    store.approve([first.id, second.id], START)
    store.mark_sent(first.id, START + timedelta(minutes=1), "https://www.carousell.sg/inbox/1/")
    store.mark_failed(second.id, "CHAT_BUTTON not found")
    store.mark_skipped([third.id], "the daily cap on new conversations is reached")

    by_id = {message.id: message for message in store.list_for_run("20261030-090000-run1")}
    assert by_id[first.id].status == "sent" and by_id[first.id].chat_url.endswith("/inbox/1/")
    assert by_id[first.id].sent_at == START + timedelta(minutes=1)
    assert by_id[second.id].status == "failed" and by_id[second.id].error == "CHAT_BUTTON not found"
    assert by_id[third.id].status == "skipped" and "daily cap" in by_id[third.id].skip_reason
    assert [message.id for message in store.list_recent(statuses=("sent",))] == [first.id]


def test_a_sent_message_is_never_marked_skipped(store: SqliteOutreachStore) -> None:
    (sent,) = store.add_drafts([draft("1")], START)
    store.mark_sent(sent.id, START, None)

    store.mark_skipped([sent.id], "replaced")

    assert store.get_many([sent.id])[0].status == "sent"


def test_only_unsent_bodies_can_be_edited(store: SqliteOutreachStore) -> None:
    editable, sent = store.add_drafts([draft("1"), draft("2")], START)
    store.mark_sent(sent.id, START, None)

    store.update_body(editable.id, "New text, $75?")
    store.update_body(sent.id, "Too late")

    assert [message.body for message in store.get_many([editable.id, sent.id])] == [
        "New text, $75?", "Hi, is $75 ok for listing 2?"
    ]


def test_already_contacted_matches_a_sent_listing_or_seller_only(store: SqliteOutreachStore) -> None:
    sent, _never_sent = store.add_drafts([draft("1", seller_id="s1"), draft("2", seller_id="s2")], START)
    store.mark_sent(sent.id, START, None)

    assert store.already_contacted("1", "someone-else")
    assert store.already_contacted("99", "s1")
    assert store.already_contacted("99", None, "user1")  # by username when the seller id is unknown
    assert not store.already_contacted("2", "s2")  # drafted, never sent
    assert not store.already_contacted("99", None, None)


def test_sent_count_since_counts_sends_in_the_window(store: SqliteOutreachStore) -> None:
    messages = store.add_drafts([draft("1"), draft("2"), draft("3")], START)
    store.mark_sent(messages[0].id, START - timedelta(hours=30), None)
    store.mark_sent(messages[1].id, START - timedelta(hours=2), None)
    store.mark_sent(messages[2].id, START, None)

    assert store.sent_count_since(START - timedelta(hours=24)) == 2
    assert store.sent_count_since(START + timedelta(seconds=1)) == 0


def test_the_reply_columns_exist_for_the_later_phase(store: SqliteOutreachStore) -> None:
    columns = {row["name"] for row in store._connection.execute("PRAGMA table_info(outreach_messages)")}

    assert {"reply_text", "reply_at", "reply_class", "counter_price"} <= columns
