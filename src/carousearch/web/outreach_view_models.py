"""What the outreach templates show: the "Message sellers" panel under an Ask answer, the drafts to review, a
batch's progress, and the /outreach page. Pure: models in, frozen dataclasses out."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from carousearch.agent.run_directory import RUNS_DIRECTORY_NAME
from carousearch.outreach.models import OutreachMessage
from carousearch.screening.models import format_sgd
from carousearch.text_render import format_datetime
from carousearch.web.view_models import ErrorMessage

if TYPE_CHECKING:
    from carousearch.outreach.service import DraftBatch
    from carousearch.web.outreach_runs import OutreachBatch

DEFAULT_PERCENT_BELOW = 25
DEFAULT_ROUND_TO = 50
OUTREACH_PAGE_URL = "/outreach"

NOT_CONFIGURED_MESSAGE = ErrorMessage("Outreach is not set up in this web app.")
BATCH_IN_PROGRESS_MESSAGE = ErrorMessage("A batch of messages is already being sent. Wait for it to finish.")
NOTHING_CHOSEN_MESSAGE = ErrorMessage("Tick at least one message to send.")
UNKNOWN_BATCH_MESSAGE = ErrorMessage("That batch is no longer here (the web app keeps the last few until it restarts).")
UNKNOWN_OUTREACH_RUN_MESSAGE = ErrorMessage("That run's folder is not in the data folder, so there is nothing to draft from.")
DRAFTING_FAILED_MESSAGE = ErrorMessage("Drafting failed. The terminal running carousearch web has the details.")
BAD_OFFER_RULE_MESSAGE = ErrorMessage("Give a percentage from 1 to 90 and a rounding step of 1 or more.")


@dataclass(frozen=True)
class OutreachPanelView:
    run_id: str  # the run folder's name
    draft_url: str
    kept_count: int
    percent_below: int
    round_to: int
    missing_key: ErrorMessage | None  # shown instead of the form when the drafting model has no key


def outreach_panel_view(run_id: str, kept_count: int, missing_key_variable: str | None) -> OutreachPanelView:
    missing = None
    if missing_key_variable:
        missing = ErrorMessage(f"Drafting needs {missing_key_variable} in .env; then restart", "carousearch web")
    return OutreachPanelView(
        run_id=run_id, draft_url=f"/outreach/runs/{run_id}/drafts", kept_count=kept_count,
        percent_below=DEFAULT_PERCENT_BELOW, round_to=DEFAULT_ROUND_TO, missing_key=missing,
    )


def outreach_panel_for(
    run_directory: Path | None, runs_root: Path | None, kept_count: int, missing_key_variable: str | None
) -> OutreachPanelView | None:
    """The panel for a run that kept listings and whose folder is in the data folder's runs/, else None."""
    if run_directory is None or runs_root is None or kept_count == 0:
        return None
    if run_directory.parent != runs_root / RUNS_DIRECTORY_NAME:
        return None
    return outreach_panel_view(run_directory.name, kept_count, missing_key_variable)


@dataclass(frozen=True)
class DraftRowView:
    message_id: int
    title: str
    listing_url: str | None
    seller: str
    asking: str
    offer: str
    body: str
    checked: bool
    skip_label: str | None  # why a send now would skip it


@dataclass(frozen=True)
class PassedOverView:
    title: str
    listing_url: str | None
    reason: str


@dataclass(frozen=True)
class DraftsView:
    run_id: str
    send_url: str
    rows: list[DraftRowView]
    passed_over: list[PassedOverView]
    notice: str | None
    cap_line: str
    send_count: int
    dry_run_forced: bool = False  # public-demo mode: the Send button only rehearses

    @property
    def send_label(self) -> str:
        return f"Send {self.send_count} message{'' if self.send_count == 1 else 's'}"


def drafts_view(batch: "DraftBatch", dry_run_forced: bool = False) -> DraftsView:
    rows = [
        DraftRowView(
            message_id=draft.id,
            title=draft.listing_title,
            listing_url=draft.listing_url,
            seller=draft.seller_username or "-",
            asking=money(draft.listing_price),
            offer=money(draft.offer_price),
            body=draft.body,
            checked=draft.id not in batch.send_preview,
            skip_label=batch.send_preview.get(draft.id),
        )
        for draft in batch.drafts
    ]
    notice = None
    if not batch.from_answer:
        notice = "This run kept no answer.json, so every listing it saw stands in for the shortlist."
    return DraftsView(
        run_id=batch.run_id,
        send_url=f"/outreach/runs/{batch.run_id}/send",
        rows=rows,
        passed_over=[PassedOverView(listing.title, listing.url, reason) for listing, reason in batch.skipped],
        notice=notice,
        cap_line=f"{batch.cap_remaining} new conversations allowed in the next 24 hours.",
        send_count=sum(row.checked for row in rows),
        dry_run_forced=dry_run_forced,
    )


@dataclass(frozen=True)
class BatchView:
    batch_id: str
    poll_url: str
    stop_url: str
    running: bool
    dry_run: bool
    lines: list[str]
    summary: str | None
    error: str | None


def batch_view(batch: "OutreachBatch") -> BatchView:
    lines = [event.text for event in batch.events if event.kind != "finished"]
    finished = next((event.text for event in reversed(batch.events) if event.kind == "finished"), None)
    return BatchView(
        batch_id=batch.batch_id,
        poll_url=f"/outreach/batches/{batch.batch_id}",
        stop_url=f"/outreach/batches/{batch.batch_id}/stop",
        running=batch.running,
        dry_run=batch.dry_run,
        lines=lines,
        summary=finished,
        error=batch.error,
    )


@dataclass(frozen=True)
class MessageRowView:
    message_id: int
    status: str
    when: str
    run_id: str
    title: str
    listing_url: str | None
    seller: str
    offer: str
    body: str
    chat_url: str | None
    note: str  # the skip reason or the error


def message_rows(messages: Sequence[OutreachMessage]) -> list[MessageRowView]:
    return [
        MessageRowView(
            message_id=message.id,
            status=message.status,
            when=format_datetime(message.sent_at if message.status == "sent" else message.created_at),
            run_id=message.run_id,
            title=message.listing_title,
            listing_url=message.listing_url,
            seller=message.seller_username or "-",
            offer=money(message.offer_price),
            body=message.body,
            chat_url=message.chat_url if (message.chat_url or "").startswith("https://") else None,
            note=message.skip_reason or message.error or "",
        )
        for message in messages
    ]


def money(amount: float | None) -> str:
    return "-" if amount is None else format_sgd(amount)
