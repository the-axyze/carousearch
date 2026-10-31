"""Outreach's HTTP routes: POST /outreach/runs/{run_id}/drafts (draft a run's messages), POST
/outreach/runs/{run_id}/send (the one approval: send the ticked drafts), GET /outreach/batches/{batch_id} (a
batch's progress, polled), POST /outreach/batches/{batch_id}/stop, and GET /outreach (every message). Thin: read,
call, render. `run_id` here is the run folder's name under data/runs.

Every POST needs the page's own HTMX header, so another site cannot make this app draft or send.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse

from carousearch.outreach.models import MessageStatus, OfferRule
from carousearch.outreach.run_shortlist import UnknownRunError
from carousearch.web.outreach_runs import start_send_batch
from carousearch.web.outreach_view_models import (
    BAD_OFFER_RULE_MESSAGE,
    BATCH_IN_PROGRESS_MESSAGE,
    DRAFTING_FAILED_MESSAGE,
    NOT_CONFIGURED_MESSAGE,
    NOTHING_CHOSEN_MESSAGE,
    UNKNOWN_BATCH_MESSAGE,
    UNKNOWN_OUTREACH_RUN_MESSAGE,
    batch_view,
    drafts_view,
    message_rows,
)
from carousearch.web.routes import TEMPLATES, Services, refuse_unless_from_the_page
from carousearch.web.services import WebServices
from carousearch.web.view_models import ErrorMessage

logger = logging.getLogger(__name__)

router = APIRouter()

RunFolder = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
BatchId = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
WEB_DRAFT_LIMIT = 20  # model calls per Draft press at most
MESSAGE_STATUSES: tuple[MessageStatus, ...] = ("drafted", "approved", "sent", "failed", "skipped")


@router.post("/outreach/runs/{run_id}/drafts", response_class=HTMLResponse)
async def draft_messages(
    request: Request,
    services: Services,
    run_id: RunFolder,
    percent_below: Annotated[int, Form()] = 0,
    round_to: Annotated[int, Form()] = 50,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Draft one message per kept listing (model calls only; nothing is sent) and show them for review."""
    from pydantic import ValidationError
    from pydantic_ai.exceptions import AgentRunError, UserError

    refusal = refuse_unless_from_the_page(request) or refuse_without_outreach(request, services)
    if refusal is not None:
        return refusal
    if services.outreach_missing_key:
        message = ErrorMessage(f"Drafting needs {services.outreach_missing_key} in .env; then restart", "carousearch web")
        return outreach_message(request, message, 409)
    try:
        rule = OfferRule(percent_below_asking=percent_below, round_to=round_to, note_to_drafter=note.strip())
    except ValidationError:
        return outreach_message(request, BAD_OFFER_RULE_MESSAGE, 422)
    try:
        batch = await services.outreach.draft_for_run(run_id, rule, WEB_DRAFT_LIMIT)
    except UnknownRunError:
        return outreach_message(request, UNKNOWN_OUTREACH_RUN_MESSAGE, 404)
    except (AgentRunError, UserError) as error:
        logger.warning("Outreach drafting for run %s failed: %s: %s", run_id, type(error).__name__, error)
        return outreach_message(request, DRAFTING_FAILED_MESSAGE, 409)
    logger.info("Outreach: %d drafts for run %s", len(batch.drafts), run_id)
    view = drafts_view(batch, dry_run_forced=services.outreach.force_dry_run)
    return TEMPLATES.TemplateResponse(request, "partials/outreach_drafts.html", {"drafts": view})


@router.post("/outreach/runs/{run_id}/send", response_class=HTMLResponse)
async def send_messages(request: Request, services: Services, run_id: RunFolder) -> HTMLResponse:
    """The batch's one approval: save the owner's edits, then send the ticked drafts in the background."""
    refusal = refuse_unless_from_the_page(request) or refuse_without_outreach(request, services)
    if refusal is not None:
        return refusal
    form = await request.form()
    message_ids = [int(value) for value in form.getlist("message_id") if str(value).isdigit()]
    if not message_ids:
        return outreach_message(request, NOTHING_CHOSEN_MESSAGE, 422)
    if services.outreach_batches.active() is not None:
        return outreach_message(request, BATCH_IN_PROGRESS_MESSAGE, 409)
    edits = {message_id: str(form.get(f"body_{message_id}") or "") for message_id in message_ids}
    services.outreach.update_bodies(edits)
    dry_run = form.get("dry_run") == "1" or services.outreach.force_dry_run  # the service enforces it too
    batch = start_send_batch(
        services.outreach_batches, services.outreach, run_id, message_ids, dry_run, services.screening.clock()
    )
    logger.info("Outreach batch %s: %d messages for run %s%s", batch.batch_id, len(message_ids), run_id,
                " (dry run)" if dry_run else "")
    return TEMPLATES.TemplateResponse(request, "partials/outreach_progress.html", {"batch": batch_view(batch)})


@router.get("/outreach/batches/{batch_id}", response_class=HTMLResponse)
async def batch_progress(request: Request, services: Services, batch_id: BatchId) -> HTMLResponse:
    """A batch's progress; the partial polls again until the batch is done."""
    batch = services.outreach_batches.get(batch_id)
    if batch is None:
        return outreach_message(request, UNKNOWN_BATCH_MESSAGE, 404)
    return TEMPLATES.TemplateResponse(request, "partials/outreach_progress.html", {"batch": batch_view(batch)})


@router.post("/outreach/batches/{batch_id}/stop", response_class=HTMLResponse)
async def stop_batch(request: Request, services: Services, batch_id: BatchId) -> HTMLResponse:
    """Stop a batch before its next message (the one being sent, if any, finishes)."""
    refusal = refuse_unless_from_the_page(request)
    if refusal is not None:
        return refusal
    batch = services.outreach_batches.get(batch_id)
    if batch is None:
        return outreach_message(request, UNKNOWN_BATCH_MESSAGE, 404)
    batch.stop_requested.set()
    logger.info("Outreach batch %s: stop requested", batch_id)
    return TEMPLATES.TemplateResponse(request, "partials/outreach_progress.html", {"batch": batch_view(batch)})


@router.get("/outreach", response_class=HTMLResponse)
async def outreach_page(request: Request, services: Services, status: str = "") -> HTMLResponse:
    """Every drafted and sent message, newest first."""
    statuses = (status,) if status in MESSAGE_STATUSES else ()
    messages = services.outreach.store.list_recent(statuses=statuses) if services.outreach else []
    context = {"rows": message_rows(messages), "status": status if statuses else "", "statuses": MESSAGE_STATUSES,
               "configured": services.outreach is not None, "public_demo": services.public_demo}
    return TEMPLATES.TemplateResponse(request, "outreach.html", context)


def refuse_without_outreach(request: Request, services: WebServices) -> HTMLResponse | None:
    return outreach_message(request, NOT_CONFIGURED_MESSAGE, 409) if services.outreach is None else None


def outreach_message(request: Request, error: ErrorMessage, status_code: int) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "partials/error.html", {"error": error}, status_code=status_code)
