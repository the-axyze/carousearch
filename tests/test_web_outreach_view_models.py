"""Outreach's web view models. Pure."""

from pathlib import Path

from carousearch.outreach.models import ShortlistedListing
from carousearch.outreach.sender import ProgressEvent
from carousearch.outreach.service import DraftBatch
from carousearch.web.outreach_runs import OutreachBatch
from carousearch.web.outreach_view_models import (
    batch_view,
    drafts_view,
    message_rows,
    outreach_panel_for,
)

from outreach_examples import START, message


def test_the_panel_needs_kept_listings_and_a_run_folder_in_the_data_folder(tmp_path: Path) -> None:
    folder = tmp_path / "runs" / "20261030-run"

    panel = outreach_panel_for(folder, tmp_path, 3, None)

    assert panel.draft_url == "/outreach/runs/20261030-run/drafts" and panel.missing_key is None
    assert outreach_panel_for(folder, tmp_path, 0, None) is None
    assert outreach_panel_for(tmp_path / "elsewhere" / "x", tmp_path, 3, None) is None
    assert outreach_panel_for(None, tmp_path, 3, None) is None
    assert "OPENAI_API_KEY" in outreach_panel_for(folder, tmp_path, 3, "OPENAI_API_KEY").missing_key.text


def test_drafts_view_unticks_drafts_a_send_would_skip() -> None:
    drafts = [message(1, "a", status="drafted"), message(2, "b", status="drafted")]
    passed = ShortlistedListing(listing_id="c", title="C", price=None, url="https://www.carousell.sg/p/c/")
    batch = DraftBatch(
        run_id="run", drafts=drafts, skipped=[(passed, "no plain price")], send_preview={2: "daily cap reached"},
        from_answer=False, cap_remaining=1,
    )

    view = drafts_view(batch)

    assert [(row.message_id, row.checked, row.skip_label) for row in view.rows] == [
        (1, True, None), (2, False, "daily cap reached")
    ]
    assert view.rows[0].offer == "S$75" and view.rows[0].asking == "S$100"
    assert view.send_label == "Send 1 message" and view.send_url == "/outreach/runs/run/send"
    assert view.passed_over[0].reason == "no plain price" and view.notice is not None
    assert view.cap_line == "1 new conversations allowed in the next 24 hours."


def test_drafts_view_says_when_sends_are_forced_to_dry_run() -> None:
    batch = DraftBatch(
        run_id="run", drafts=[message(1, "a", status="drafted")], skipped=[], send_preview={}, from_answer=True,
        cap_remaining=1,
    )

    assert not drafts_view(batch).dry_run_forced
    assert drafts_view(batch, dry_run_forced=True).dry_run_forced


def test_batch_view_polls_while_running_and_shows_the_summary_when_done() -> None:
    batch = OutreachBatch(batch_id="b1", run_id="run", dry_run=False, started_at=START)
    batch.record(ProgressEvent("sending", "Sending to user1"))

    running = batch_view(batch)
    batch.record(ProgressEvent("finished", "Done: 1 sent."))
    batch.report = object()  # any report ends the batch
    done = batch_view(batch)

    assert running.running and running.lines == ["Sending to user1"] and running.poll_url == "/outreach/batches/b1"
    assert not done.running and done.summary == "Done: 1 sent." and done.lines == ["Sending to user1"]


def test_message_rows_link_only_real_chat_addresses() -> None:
    rows = message_rows([
        message(1, "a", status="sent", sent_at=START, chat_url="https://www.carousell.sg/inbox/1/"),
        message(2, "b", status="sent", sent_at=START, chat_url="dry-run://chat/b"),
        message(3, "c", status="skipped", skip_reason="replaced by a newer draft"),
    ])

    assert [row.chat_url for row in rows] == ["https://www.carousell.sg/inbox/1/", None, None]
    assert rows[0].when == "2026-10-30 17:00 SGT" and rows[2].note == "replaced by a newer draft"
