"""HTTP routes: the page (Filters or Ask mode), the results partial, the listing-detail partial, and Ask mode's
POST /ask (plan a run), GET /ask/{run_id} (its progress, its plan, then its answer, with outreach's "Message
sellers" panel), and the plan's buttons: POST /ask/{run_id}/execute, /revise (one round) and /cancel, plus POST
/ask/{run_id}/knowledge (save the answer's suggested note). Outreach's own routes are in outreach_routes.py.
Thin: read, call, render.

Only the Ask POSTs start model calls, and only POST .../execute spends Carousell requests beyond planning's. While an
Ask run plans or runs, the filter-search and detail routes answer at once with a "waiting" partial that asks again
every 2 s, instead of queueing behind the run for minutes.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import SearchRequest
from carousearch.web.outreach_view_models import OutreachPanelView, outreach_panel_for
from carousearch.web.prompt_runs import (
    PromptRun,
    save_suggested_note,
    start_execution,
    start_planning_run,
    start_revision,
)
from carousearch.web.prompt_view_models import (
    NO_KNOWLEDGE_FOLDER_MESSAGE,
    NO_MORE_REVISIONS_MESSAGE,
    NO_NOTE_MESSAGE,
    NOT_FROM_THE_PAGE_MESSAGE,
    NOT_PLANNED_MESSAGE,
    RUN_IN_PROGRESS_MESSAGE,
    UNKNOWN_RUN_MESSAGE,
    WAITING_RESULTS_ELEMENT_ID,
    PlanView,
    ProgressView,
    answer_view,
    ask_form_view,
    cancelled_view,
    change_problem,
    failed_view,
    missing_key_message,
    plan_view,
    progress_view,
    prompt_problem,
    waiting_view,
)
from carousearch.web.query_params import (
    PageMode,
    ResultsTally,
    hide_multi_item_from_params,
    mode_from_params,
    page_url,
    search_request_from_params,
    tally_from_params,
)
from carousearch.web.services import WebServices, load_listing, search_and_screen
from carousearch.web.view_models import (
    GENERIC_ERROR_MESSAGE,
    ErrorMessage,
    detail_view,
    error_message_for,
    results_view,
    search_form_view,
)

logger = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


class RequestAbandoned(Exception):
    """The browser gave up on the request before its turn came, so its work is skipped (web.app answers 204)."""


def get_services(request: Request) -> WebServices:
    return request.app.state.services


Services = Annotated[WebServices, Depends(get_services)]
RunId = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{1,32}$")]


@router.get("/", response_class=HTMLResponse)
async def search_page(request: Request, services: Services) -> HTMLResponse:
    """The whole page. In Filters mode with search parameters (a bookmark, a reload) it includes the first page of
    results; in Ask mode it shows the form and the run in progress, if any, and never searches or runs anything."""
    params = read_query_params(request)
    mode = mode_from_params(params)
    search_request = search_request_from_params(params)
    hide_multi_item = hide_multi_item_from_params(params)
    active_run = services.prompt_runs.active()
    waiting_plan = plan_waiting_for_a_decision(services) if active_run is None else None
    context: dict[str, Any] = {
        "form": search_form_view(search_request, hide_multi_item),
        "mode": mode.value,
        "ask": ask_form_view(services.agent),
        "progress": None if active_run is None else run_progress(active_run, services),
        "plan": None if waiting_plan is None else run_plan(waiting_plan, services),
        "public_demo": services.public_demo,
    }
    if mode == PageMode.FILTERS and search_request is not None:
        if active_run is not None:
            context["waiting"] = waiting_view(f"/results?{request.url.query}")
        else:
            context |= await results_context(request, services, search_request, hide_multi_item, ResultsTally())
    return TEMPLATES.TemplateResponse(request, "index.html", context)


@router.get("/results", response_class=HTMLResponse)
async def results_partial(request: Request, services: Services) -> HTMLResponse:
    """The results region for the form's values. With `cursor`: the next page's cards, appended by "Load more"."""
    params = read_query_params(request)
    search_request = search_request_from_params(params) if mode_from_params(params) == PageMode.FILTERS else None
    hide_multi_item = hide_multi_item_from_params(params)
    waiting = search_request is not None and services.prompt_runs.active() is not None
    # The address bar shows the page URL (/?q=...), not this partial's, so a reload or bookmark gets the full page.
    # A waiting partial asking again pushes nothing: the first answer already did.
    pushes_url = not search_request or not search_request.page_cursor
    asking_again = request.headers.get("HX-Trigger") == WAITING_RESULTS_ELEMENT_ID
    headers = {"HX-Push-Url": page_url(search_request, hide_multi_item)} if pushes_url and not asking_again else {}
    if waiting:
        context = {"waiting": waiting_view(f"{request.url.path}?{request.url.query}")}
        return TEMPLATES.TemplateResponse(request, "partials/ask_waiting.html", context, headers=headers)
    if search_request is not None and search_request.page_cursor:
        context = await results_context(request, services, search_request, hide_multi_item, tally_from_params(params))
        return TEMPLATES.TemplateResponse(request, "partials/more_results.html", context)
    context: dict[str, Any] = {}
    if search_request is not None:
        context |= await results_context(request, services, search_request, hide_multi_item, ResultsTally())
    return TEMPLATES.TemplateResponse(request, "partials/results.html", context, headers=headers)


@router.get("/listing/{listing_id}", response_class=HTMLResponse)
async def listing_detail_partial(
    request: Request, services: Services, listing_id: Annotated[str, PathParameter(pattern=r"^[0-9]+$")]
) -> HTMLResponse:
    if services.prompt_runs.active() is not None:
        context = {"waiting": waiting_view(request.url.path, for_detail=True)}
        return TEMPLATES.TemplateResponse(request, "partials/ask_waiting.html", context)
    outcome = await call_in_turn(request, services, load_listing, listing_id, services)
    if isinstance(outcome, ErrorMessage):
        return TEMPLATES.TemplateResponse(request, "partials/error.html", {"error": outcome})
    context = {"detail": detail_view(outcome.detail, outcome.screened)}
    return TEMPLATES.TemplateResponse(request, "partials/listing_detail.html", context)


# --- Ask mode -----------------------------------------------------------------------------------------------------


@router.post("/ask", response_class=HTMLResponse)
async def start_ask_run(request: Request, services: Services, prompt: Annotated[str, Form()] = "") -> HTMLResponse:
    """Start planning a run in the background and answer with its progress, which polls GET /ask/{run_id} until the
    plan is ready. Refused, with nothing started: not sent by the page's own HTMX (403), no provider key (409), an
    empty or too long prompt (422), a run in progress (409)."""
    refusal = refuse_unless_from_the_page(request) or refuse_without_a_key(request, services)
    if refusal is not None:
        return refusal
    problem = prompt_problem(prompt)
    if problem is not None:
        return ask_message(request, problem, status_code=422)
    busy = refuse_while_busy(request, services)
    if busy is not None:
        return busy
    run = start_planning_run(prompt.strip(), services)
    logger.info("Ask run %s planning", run.run_id)
    return TEMPLATES.TemplateResponse(request, "partials/ask_progress.html", {"progress": run_progress(run, services)})


@router.get("/ask/{run_id}", response_class=HTMLResponse)
async def ask_run_partial(request: Request, services: Services, run_id: RunId) -> HTMLResponse:
    """A run's progress while it plans or runs (the partial polls again), its plan while it waits for a decision,
    then its answer, its failure or its cancellation (polling stops)."""
    run = services.prompt_runs.get(run_id)
    if run is None:
        return ask_message(request, UNKNOWN_RUN_MESSAGE, status_code=404)
    return render_run(request, run, services)


@router.post("/ask/{run_id}/execute", response_class=HTMLResponse)
async def execute_ask_plan(request: Request, services: Services, run_id: RunId) -> HTMLResponse:
    """Execute a planned run's plan (the first Carousell requests beyond planning), answering with its progress."""
    run, refusal = planned_run_or_refusal(request, services, run_id)
    if refusal is not None:
        return refusal
    start_execution(run, services)
    logger.info("Ask run %s executing", run.run_id)
    return TEMPLATES.TemplateResponse(request, "partials/ask_progress.html", {"progress": run_progress(run, services)})


@router.post("/ask/{run_id}/revise", response_class=HTMLResponse)
async def revise_ask_plan(
    request: Request, services: Services, run_id: RunId, change: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Send the plan back to the planner with the owner's change, once; answers with the planning progress."""
    run, refusal = planned_run_or_refusal(request, services, run_id)
    if refusal is not None:
        return refusal
    if not run.can_revise:
        return ask_message(request, NO_MORE_REVISIONS_MESSAGE, status_code=409, plan=run_plan(run, services))
    problem = change_problem(change)
    if problem is not None:
        return ask_message(request, problem, status_code=422, plan=run_plan(run, services))
    start_revision(run, change.strip(), services)
    logger.info("Ask run %s revising its plan", run.run_id)
    return TEMPLATES.TemplateResponse(request, "partials/ask_progress.html", {"progress": run_progress(run, services)})


@router.post("/ask/{run_id}/cancel", response_class=HTMLResponse)
async def cancel_ask_plan(request: Request, services: Services, run_id: RunId) -> HTMLResponse:
    """Drop a planned run without executing it."""
    refusal = refuse_unless_from_the_page(request)
    if refusal is not None:
        return refusal
    run = services.prompt_runs.get(run_id)
    if run is None:
        return ask_message(request, UNKNOWN_RUN_MESSAGE, status_code=404)
    if run.status != "planned":
        return ask_message(request, NOT_PLANNED_MESSAGE, status_code=409)
    run.cancel(services.screening.clock())
    logger.info("Ask run %s cancelled before execution", run.run_id)
    return render_run(request, run, services)


@router.post("/ask/{run_id}/knowledge", response_class=HTMLResponse)
async def save_ask_note(request: Request, services: Services, run_id: RunId) -> HTMLResponse:
    """Save a finished run's suggested note to knowledge/ (never overwriting a file) and say where."""
    refusal = refuse_unless_from_the_page(request)
    if refusal is not None:
        return refusal
    run = services.prompt_runs.get(run_id)
    if run is None:
        return ask_message(request, UNKNOWN_RUN_MESSAGE, status_code=404)
    if run.answer is None or not run.answer.suggested_knowledge:
        return ask_message(request, NO_NOTE_MESSAGE, status_code=409)
    if services.knowledge_dir is None:
        return ask_message(request, NO_KNOWLEDGE_FOLDER_MESSAGE, status_code=409)
    file_name = await run_in_threadpool(save_suggested_note, run, services.knowledge_dir)
    logger.info("Ask run %s: suggested note saved to knowledge/%s", run.run_id, file_name)
    context = {"saved_to": f"knowledge/{file_name}"}
    return TEMPLATES.TemplateResponse(request, "partials/ask_note_saved.html", context)


def plan_waiting_for_a_decision(services: WebServices) -> PromptRun | None:
    """The newest run when it is planned and waiting for Execute, Edit or Cancel (a reload shows it again)."""
    newest_run = services.prompt_runs.newest()
    return newest_run if newest_run is not None and newest_run.status == "planned" else None


def render_run(request: Request, run: PromptRun, services: WebServices) -> HTMLResponse:
    """The partial for a run's current state."""
    if run.busy:
        context = {"progress": run_progress(run, services)}
        return TEMPLATES.TemplateResponse(request, "partials/ask_progress.html", context)
    if run.status == "planned":
        return TEMPLATES.TemplateResponse(request, "partials/ask_plan.html", {"plan": run_plan(run, services)})
    if run.status == "cancelled":
        return TEMPLATES.TemplateResponse(request, "partials/ask_cancelled.html", {"cancelled": cancelled_view(run)})
    elapsed = elapsed_seconds(run, services)
    if run.answer is not None:
        context = {
            "answer": answer_view(run, run.answer, services.agent.budget, elapsed),
            "outreach": outreach_panel(run, services),
        }
        return TEMPLATES.TemplateResponse(request, "partials/ask_answer.html", context)
    error = run.error or GENERIC_ERROR_MESSAGE
    context = {"failed": failed_view(run, error, services.agent.budget)}
    return TEMPLATES.TemplateResponse(request, "partials/ask_failed.html", context)


def outreach_panel(run: PromptRun, services: WebServices) -> OutreachPanelView | None:
    """The "Message sellers" panel under a finished answer, when outreach is set up."""
    if services.outreach is None or run.answer is None:
        return None
    return outreach_panel_for(
        run.run_directory, services.runs_root, len(run.answer.kept), services.outreach_missing_key
    )


def planned_run_or_refusal(
    request: Request, services: WebServices, run_id: str
) -> tuple[PromptRun, None] | tuple[None, HTMLResponse]:
    """The run behind a plan button, or the refusal to send instead: not from the page (403), no key (409), an
    unknown run (404), a run not waiting for a decision (409) or another run in progress (409)."""
    refusal = refuse_unless_from_the_page(request) or refuse_without_a_key(request, services)
    if refusal is not None:
        return None, refusal
    run = services.prompt_runs.get(run_id)
    if run is None:
        return None, ask_message(request, UNKNOWN_RUN_MESSAGE, status_code=404)
    if run.status != "planned":
        return None, ask_message(request, NOT_PLANNED_MESSAGE, status_code=409)
    busy = refuse_while_busy(request, services)
    if busy is not None:
        return None, busy
    return run, None


def refuse_unless_from_the_page(request: Request) -> HTMLResponse | None:
    """Another site's page can post a form to localhost but cannot add this header, so it cannot spend money here."""
    if request.headers.get("HX-Request") != "true":
        return ask_message(request, NOT_FROM_THE_PAGE_MESSAGE, status_code=403)
    return None


def refuse_without_a_key(request: Request, services: WebServices) -> HTMLResponse | None:
    missing_key = missing_key_message(services.agent)
    return ask_message(request, missing_key, status_code=409) if missing_key is not None else None


def refuse_while_busy(request: Request, services: WebServices) -> HTMLResponse | None:
    active_run = services.prompt_runs.active()
    if active_run is None:
        return None
    progress = run_progress(active_run, services)
    return ask_message(request, RUN_IN_PROGRESS_MESSAGE, status_code=409, progress=progress)


def ask_message(
    request: Request,
    error: ErrorMessage,
    status_code: int,
    progress: ProgressView | None = None,
    plan: PlanView | None = None,
) -> HTMLResponse:
    """A calm refusal in the Ask run region; with `progress` the run in flight, or with `plan` the plan waiting for
    a decision, goes on showing below it."""
    context = {"error": error, "progress": progress, "plan": plan}
    return TEMPLATES.TemplateResponse(request, "partials/ask_message.html", context, status_code=status_code)


def run_progress(run: PromptRun, services: WebServices) -> ProgressView:
    return progress_view(
        run, services.agent.budget, elapsed_seconds(run, services), services.agent.planning_budget
    )


def run_plan(run: PromptRun, services: WebServices) -> PlanView:
    planned_at = run.planned_at or services.screening.clock()
    return plan_view(run, services.agent.budget, (planned_at - run.started_at).total_seconds())


def elapsed_seconds(run: PromptRun, services: WebServices) -> float:
    end = run.finished_at or services.screening.clock()
    return (end - run.started_at).total_seconds()


async def results_context(
    request: Request,
    services: WebServices,
    search_request: SearchRequest,
    hide_multi_item: bool,
    earlier_pages: ResultsTally,
) -> dict[str, Any]:
    """The template context for a search: {"results": ResultsView}, or {"error": ErrorMessage} if it failed."""
    outcome = await call_in_turn(request, services, search_and_screen, search_request, services)
    if isinstance(outcome, ErrorMessage):
        return {"error": outcome}
    view = results_view(outcome, search_request, hide_multi_item, earlier_pages, services.price_lookup_enabled)
    return {"results": view}


async def call_in_turn[Result](
    request: Request, services: WebServices, work: Callable[..., Result], *arguments: Any
) -> Result | ErrorMessage:
    """Run blocking `work` in a worker thread once no other call is running. A failure becomes a calm message."""
    async with services.turn:
        if await request.is_disconnected():
            raise RequestAbandoned
        try:
            return await run_in_threadpool(work, *arguments)
        except CarousellError as error:
            logger.warning("Carousell request failed: %s: %s", type(error).__name__, error)
            return error_message_for(error)
        except Exception:  # never a traceback in the page; the log has it
            logger.exception("%s failed", request.url.path)
            return GENERIC_ERROR_MESSAGE


def read_query_params(request: Request) -> dict[str, list[str]]:
    return {name: request.query_params.getlist(name) for name in request.query_params}
