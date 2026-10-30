"""Builds the web app: its settings, the services every request shares, the routes and the static files."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from carousearch import storage
from carousearch.agent.judge_cache import SqliteJudgementCache
from carousearch.agent.knowledge import DEFAULT_KNOWLEDGE_DIR
from carousearch.agent.settings import AgentSettings
from carousearch.carousell.client import CarousellClient, create_carousell_client
from carousearch.config import DATABASE_FILE_NAME, OutreachSettings, ScreeningSettings
from carousearch.outreach.service import build_outreach_service
from carousearch.screening.pipeline import DEFAULT_MAX_DETAIL_FETCHES, ScreeningServices
from carousearch.screening.price_resolver import NoopPriceResolver, build_price_resolver
from carousearch.screening.store import SqliteScreeningStore
from carousearch.web.outreach_routes import router as outreach_router
from carousearch.web.routes import RequestAbandoned, router
from carousearch.web.services import WebServices

DEFAULT_WEB_HOST = "127.0.0.1"  # this machine only; WSL2 forwards it to the Windows browser's localhost
DEFAULT_WEB_PORT = 8765
STATIC_DIR = Path(__file__).parent / "static"

ServicesFactory = Callable[[], WebServices]


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    screening: ScreeningSettings
    max_detail_fetches: int = DEFAULT_MAX_DETAIL_FETCHES
    agent: AgentSettings = field(default_factory=AgentSettings)  # Ask mode's model, budgets and provider keys
    knowledge_dir: Path | None = DEFAULT_KNOWLEDGE_DIR  # the owner's notes the Ask planner reads and saves to
    outreach: OutreachSettings = field(default_factory=OutreachSettings)  # drafting model, caps, browser profile


def create_app(settings: WebSettings, services_factory: ServicesFactory | None = None) -> FastAPI:
    """The app. Its services are built once when it starts and closed when it stops.

    By default they come from `settings` with a real Carousell client; tests pass a factory that returns fakes.
    """
    build_services = services_factory or (lambda: build_web_services(settings, create_carousell_client()))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = build_services()
        app.state.services = services
        try:
            yield
        finally:
            await services.prompt_runs.stop()  # an Ask run in flight must not outlive the client it uses
            await services.outreach_batches.stop()  # a batch stops at its next wait; the rest stay approved
            services.close()

    app = FastAPI(title="Carousearch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.include_router(outreach_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_exception_handler(RequestAbandoned, answer_no_one)
    return app


async def answer_no_one(request: Request, error: Exception) -> Response:
    """The browser dropped the request (a newer search replaced it) before its turn came: nothing to send."""
    return Response(status_code=204)


def build_web_services(settings: WebSettings, carousell_client: CarousellClient) -> WebServices:
    """The real services: the given client, the SQLite caches in the data folder, and the configured resolver."""
    database = storage.open_database(settings.data_dir / DATABASE_FILE_NAME, check_same_thread=False)
    resolver = build_price_resolver(settings.screening)
    screening = ScreeningServices(
        client=carousell_client,
        store=SqliteScreeningStore(database),
        resolver=resolver,
        clock=utc_now,
        max_detail_fetches=settings.max_detail_fetches,
    )

    outreach = build_outreach_service(settings.outreach, settings.data_dir, utc_now)

    def close() -> None:
        carousell_client.close()
        database.close()
        outreach.store.close()

    return WebServices(
        carousell=carousell_client,
        screening=screening,
        price_lookup_enabled=not isinstance(resolver, NoopPriceResolver),
        close=close,
        agent=settings.agent,
        judgement_cache=SqliteJudgementCache(database),
        runs_root=settings.data_dir,
        knowledge_dir=settings.knowledge_dir,
        outreach=outreach,
        outreach_missing_key=settings.outreach.missing_key_variable(),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)
