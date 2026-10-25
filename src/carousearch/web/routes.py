"""HTTP routes: the page, the results partial and the listing-detail partial. Thin: read, call, render."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from carousearch.carousell.errors import CarousellError
from carousearch.filters.schema import SearchRequest
from carousearch.web.query_params import (
    ResultsTally,
    hide_multi_item_from_params,
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


@router.get("/", response_class=HTMLResponse)
async def search_page(request: Request, services: Services) -> HTMLResponse:
    """The whole page; with search parameters (a bookmark, a reload) it includes the first page of results."""
    params = read_query_params(request)
    search_request = search_request_from_params(params)
    hide_multi_item = hide_multi_item_from_params(params)
    context: dict[str, Any] = {"form": search_form_view(search_request, hide_multi_item)}
    if search_request is not None:
        context |= await results_context(request, services, search_request, hide_multi_item, ResultsTally())
    return TEMPLATES.TemplateResponse(request, "index.html", context)


@router.get("/results", response_class=HTMLResponse)
async def results_partial(request: Request, services: Services) -> HTMLResponse:
    """The results region for the form's values. With `cursor`: the next page's cards, appended by "Load more"."""
    params = read_query_params(request)
    search_request = search_request_from_params(params)
    hide_multi_item = hide_multi_item_from_params(params)
    if search_request is not None and search_request.page_cursor:
        context = await results_context(request, services, search_request, hide_multi_item, tally_from_params(params))
        return TEMPLATES.TemplateResponse(request, "partials/more_results.html", context)
    context: dict[str, Any] = {}
    if search_request is not None:
        context |= await results_context(request, services, search_request, hide_multi_item, ResultsTally())
    # The address bar shows the page URL (/?q=...), not this partial's, so a reload or bookmark gets the full page.
    headers = {"HX-Push-Url": page_url(search_request, hide_multi_item)}
    return TEMPLATES.TemplateResponse(request, "partials/results.html", context, headers=headers)


@router.get("/listing/{listing_id}", response_class=HTMLResponse)
async def listing_detail_partial(
    request: Request, services: Services, listing_id: Annotated[str, PathParameter(pattern=r"^[0-9]+$")]
) -> HTMLResponse:
    outcome = await call_in_turn(request, services, load_listing, listing_id, services)
    if isinstance(outcome, ErrorMessage):
        return TEMPLATES.TemplateResponse(request, "partials/error.html", {"error": outcome})
    context = {"detail": detail_view(outcome.detail, outcome.screened)}
    return TEMPLATES.TemplateResponse(request, "partials/listing_detail.html", context)


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
