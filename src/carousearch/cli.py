"""The `carousearch` command line: search, listing, harvest, analyze, canary, capture-fixtures, bot, whoami and web."""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import httpx
import uvicorn
from pydantic import ValidationError
from telegram.error import TelegramError

from carousearch import storage
from carousearch.bot.app import create_bot_services, fetch_recent_senders, run_polling
from carousearch.canary import (
    DEFAULT_MINIMUM_LISTINGS,
    CanaryOutcome,
    append_canary_log,
    format_canary_log_line,
    run_canary,
)
from carousearch.carousell.client import CarousellClient, create_carousell_client
from carousearch.carousell.errors import CarousellError
from carousearch.carousell.rate_limit import DEFAULT_JITTER_SECONDS, DEFAULT_MIN_INTERVAL_SECONDS, RateLimiter
from carousearch.config import (
    CANARY_LOG_FILE_NAME,
    DATABASE_FILE_NAME,
    DEFAULT_ENV_FILE,
    TELEGRAM_BOT_TOKEN_VARIABLE,
    TELEGRAM_OWNER_ID_VARIABLE,
    TelegramSettings,
    default_data_dir,
    load_screening_settings,
    load_telegram_settings,
)
from carousearch.filters.schema import MAX_PAGE_SIZE, Condition, DealMethod, SearchRequest, SearchResult, SortOrder
from carousearch.fixture_capture import capture_fixtures
from carousearch.market.analysis import AnalysisSpec, analyse
from carousearch.market.harvest import DEFAULT_REUSE_WINDOW, HarvestOutcome, harvest
from carousearch.market.limits import HarvestLimits
from carousearch.market.render import render_analysis_json, render_analysis_text
from carousearch.screening.models import ScreenedListing
from carousearch.screening.pipeline import DEFAULT_MAX_DETAIL_FETCHES, ScreeningServices, screen_listings
from carousearch.screening.price_resolver import NoopPriceResolver, PriceResolver, build_price_resolver
from carousearch.screening.store import SqliteScreeningStore
from carousearch.text_render import (
    render_listing_detail,
    render_listing_table,
    render_screened_table,
    render_search_summary,
)
from carousearch.web.app import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, WebSettings, build_web_services, create_app

ClientFactory = Callable[[RateLimiter], CarousellClient]
PriceResolverFactory = Callable[[], PriceResolver]
WebServer = Callable[..., None]  # uvicorn.run's shape: (app, host=..., port=...)

# Shorthands accepted by --condition in addition to the exact condition names.
CONDITION_GROUPS: dict[str, list[Condition]] = {
    "new": [Condition.BRAND_NEW],
    "used": [Condition.LIKE_NEW, Condition.LIGHTLY_USED, Condition.WELL_USED, Condition.HEAVILY_USED],
}

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2


@dataclass(frozen=True)
class ScreeningOptions:
    hide_multi_item: bool = False
    max_detail_fetches: int = DEFAULT_MAX_DETAIL_FETCHES


def main(
    argv: list[str] | None = None,
    client_factory: ClientFactory = create_carousell_client,
    price_resolver_factory: PriceResolverFactory | None = None,
) -> int:
    """`price_resolver_factory` defaults to the resolver ANTHROPIC_API_KEY selects (build_price_resolver)."""
    arguments = build_argument_parser().parse_args(argv)
    data_dir: Path = arguments.data_dir or default_data_dir()
    rate_limiter = RateLimiter(arguments.min_interval, arguments.jitter)
    try:
        if arguments.command == "search":
            request = search_request_from_arguments(arguments)
            if arguments.screen or arguments.hide_multi_item:
                options = screening_options_from_arguments(arguments)
                resolver = (price_resolver_factory or default_price_resolver)()
                with client_factory(rate_limiter) as client:
                    return run_screened_search_command(
                        client, request, resolver, options, arguments.json, data_dir, sys.stdout
                    )
            with client_factory(rate_limiter) as client:
                return run_search_command(client, request, arguments.json, data_dir, sys.stdout)
        if arguments.command == "listing":
            with client_factory(rate_limiter) as client:
                return run_listing_command(client, arguments.listing_id, arguments.json, data_dir, sys.stdout)
        if arguments.command == "harvest":
            request = search_request_from_arguments(arguments)
            limits = HarvestLimits(max_pages=arguments.max_pages, max_age_days=arguments.max_age_days)
            with client_factory(rate_limiter) as client:
                return run_harvest_command(client, request, limits, _reuse_window(arguments), data_dir, sys.stdout)
        if arguments.command == "analyze":
            spec = read_analysis_spec(arguments.spec)
            with client_factory(rate_limiter) as client:
                return run_analyze_command(client, spec, arguments.json, _reuse_window(arguments), data_dir, sys.stdout)
        if arguments.command == "canary":
            log_path = arguments.log_file or data_dir / CANARY_LOG_FILE_NAME
            with client_factory(rate_limiter) as client:
                return run_canary_command(client, arguments.min_listings, log_path, sys.stdout)
        if arguments.command == "capture-fixtures":
            with client_factory(rate_limiter) as client:
                return run_capture_command(client, rate_limiter, arguments.output_dir, sys.stdout)
        if arguments.command == "bot":
            return run_bot_command(load_telegram_settings(), client_factory, rate_limiter, data_dir, sys.stderr)
        if arguments.command == "whoami":
            return run_whoami_command(load_telegram_settings(), sys.stdout, sys.stderr)
        if arguments.command == "web":
            settings = WebSettings(data_dir=data_dir, screening=load_screening_settings())
            return run_web_command(settings, client_factory, rate_limiter, arguments.host, arguments.port)
    except (ValidationError, ValueError) as error:
        print(f"error: {_describe_input_error(error)}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except CarousellError as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILURE
    raise AssertionError(f"unhandled command {arguments.command!r}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carousearch", description="Search Carousell Singapore from the terminal.")
    parser.add_argument("--data-dir", type=Path, help="database and canary log folder (default ~/carousearch/data)")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_SECONDS,
                        help="minimum seconds between Carousell requests (default %(default)s)")
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER_SECONDS,
                        help="random +/- seconds added to the interval (default %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="run a search and print the listings")
    _add_search_filter_arguments(search)
    search.add_argument("--limit", type=int, default=20, help=f"listings per page, 1 to {MAX_PAGE_SIZE}")
    search.add_argument("--page-cursor", help="next_page value from a previous --json result")
    search.add_argument("--json", action="store_true", help="print the result as JSON")
    search.add_argument("--screen", action="store_true",
                        help="flag multi-item posts and look for your variant's price in their descriptions")
    search.add_argument("--hide-multi-item", action="store_true", help="leave out multi-item posts (implies --screen)")
    search.add_argument("--max-detail-fetches", type=int, default=DEFAULT_MAX_DETAIL_FETCHES,
                        help="with --screen: Carousell detail requests to make at most (default %(default)s)")

    listing = commands.add_parser("listing", help="show one listing's details")
    listing.add_argument("listing_id")
    listing.add_argument("--json", action="store_true", help="print the full ListingDetail, including raw")

    harvest_parser = commands.add_parser("harvest", help="pull every page of a search into the listings table")
    _add_search_filter_arguments(harvest_parser)
    harvest_parser.add_argument("--max-pages", type=int, default=10, help="pages to fetch at most (default %(default)s)")
    harvest_parser.add_argument("--max-age-days", type=int, help="with --sort recent: stop once listings are older than this")
    harvest_parser.add_argument("--no-reuse", action="store_true", help="scrape even if the same search ran recently")

    analyze = commands.add_parser("analyze", help="harvest the searches in an analysis spec and print price statistics")
    analyze.add_argument("--spec", required=True, help="path to an AnalysisSpec JSON file, or - for stdin")
    analyze.add_argument("--json", action="store_true", help="print the full result as JSON")
    analyze.add_argument("--no-reuse", action="store_true", help="scrape even if the same searches ran recently")

    canary = commands.add_parser("canary", help="health check for cron: exit 0 if Carousell access works")
    canary.add_argument("--min-listings", type=int, default=DEFAULT_MINIMUM_LISTINGS)
    canary.add_argument("--log-file", type=Path, help="default <data-dir>/canary.log")

    capture = commands.add_parser("capture-fixtures", help="(dev) refresh tests/fixtures from the live site")
    capture.add_argument("--output-dir", type=Path, default=Path("tests/fixtures"))

    commands.add_parser("bot", help="run the Telegram bot (needs TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID)")
    commands.add_parser("whoami", help="print the user ids of people who recently messaged the bot")

    web = commands.add_parser("web", help="run the local web app (http://localhost:8765 by default)")
    web.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="default %(default)s")
    web.add_argument("--host", default=DEFAULT_WEB_HOST, help="default %(default)s (this machine only)")
    return parser


def _add_search_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """The filter options shared by `search` and `harvest` (everything a SearchRequest holds except paging)."""
    parser.add_argument("query", nargs="?", help="search keywords")
    parser.add_argument("--category", help="Carousell collection id, e.g. 5707 for Mobile Phones")
    parser.add_argument("--price-min", type=float, help="minimum price in SGD")
    parser.add_argument("--price-max", type=float, help="maximum price in SGD")
    parser.add_argument("--condition", help="comma-separated: " + ", ".join(_condition_choices()))
    parser.add_argument("--deal-method", help="comma-separated: meetup, mailing")
    parser.add_argument("--buyer-protection", action="store_true", help="only Buyer Protection listings")
    parser.add_argument("--free-delivery", action="store_true", help="only listings with free delivery")
    parser.add_argument("--sort", choices=[order.value for order in SortOrder], default=SortOrder.BEST_MATCH.value)
    parser.add_argument("--field", action="append", default=[], metavar="KEY=VALUE",
                        help="category facet, e.g. mobile_storage=MOBILE_STORAGE_256_GB (repeatable)")


def _reuse_window(arguments: argparse.Namespace) -> timedelta | None:
    return None if arguments.no_reuse else DEFAULT_REUSE_WINDOW


# --- argument translation (pure) ---------------------------------------------


def search_request_from_arguments(arguments: argparse.Namespace) -> SearchRequest:
    return SearchRequest(
        query=arguments.query,
        category=arguments.category,
        price_min=arguments.price_min,
        price_max=arguments.price_max,
        conditions=parse_condition_names(arguments.condition or ""),
        deal_methods=parse_deal_methods(arguments.deal_method or ""),
        buyer_protection_only=arguments.buyer_protection,
        free_delivery_only=arguments.free_delivery,
        sort=SortOrder(arguments.sort),
        category_fields=parse_category_fields(arguments.field),
        # `harvest` has no paging options: it always starts at the first page and sets its own page size.
        page_size=getattr(arguments, "limit", MAX_PAGE_SIZE),
        page_cursor=getattr(arguments, "page_cursor", None),
    )


def screening_options_from_arguments(arguments: argparse.Namespace) -> ScreeningOptions:
    if arguments.max_detail_fetches < 0:
        raise ValueError("--max-detail-fetches must be 0 or more")
    return ScreeningOptions(hide_multi_item=arguments.hide_multi_item, max_detail_fetches=arguments.max_detail_fetches)


def parse_condition_names(text: str) -> list[Condition]:
    """'used' -> the four used conditions; 'like-new,brand new' -> [LIKE_NEW, BRAND_NEW]."""
    conditions: list[Condition] = []
    for name in _split_names(text):
        if name in CONDITION_GROUPS:
            matched = CONDITION_GROUPS[name]
        elif name in {condition.value for condition in Condition}:
            matched = [Condition(name)]
        else:
            raise ValueError(f"unknown condition {name!r}; use one of: {', '.join(_condition_choices())}")
        conditions.extend(condition for condition in matched if condition not in conditions)
    return conditions


def parse_deal_methods(text: str) -> list[DealMethod]:
    methods: list[DealMethod] = []
    for name in _split_names(text):
        if name not in {method.value for method in DealMethod}:
            raise ValueError(f"unknown deal method {name!r}; use meetup or mailing")
        if DealMethod(name) not in methods:
            methods.append(DealMethod(name))
    return methods


def parse_category_fields(pairs: list[str]) -> dict[str, str | list[str]]:
    """['a=1', 'a=2', 'b=3'] -> {'a': ['1', '2'], 'b': '3'}."""
    values_by_field: dict[str, list[str]] = {}
    for pair in pairs:
        field_name, separator, value = pair.partition("=")
        if not separator or not field_name.strip() or not value.strip():
            raise ValueError(f"--field expects KEY=VALUE, got {pair!r}")
        values_by_field.setdefault(field_name.strip(), []).append(value.strip())
    return {name: values[0] if len(values) == 1 else values for name, values in values_by_field.items()}


def _split_names(text: str) -> list[str]:
    return [part.strip().lower().replace("-", "_").replace(" ", "_") for part in text.split(",") if part.strip()]


def _condition_choices() -> list[str]:
    return [condition.value for condition in Condition] + list(CONDITION_GROUPS)


def _describe_input_error(error: ValidationError | ValueError) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in problem['loc']) or 'search'}: {problem['msg']}" for problem in error.errors()
        )
    return str(error)


# --- commands ------------------------------------------------------------------


def run_search_command(
    client: CarousellClient, request: SearchRequest, as_json: bool, data_dir: Path, output: TextIO
) -> int:
    result = client.search(request)
    listings = result.listings[: request.page_size]
    with closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as connection:
        storage.record_seen_listings(connection, listings, datetime.now(UTC))
    if as_json:
        shown = result.model_copy(update={"listings": listings})
        print(shown.model_dump_json(indent=2, exclude={"listings": {"__all__": {"raw"}}}), file=output)
    else:
        print(render_listing_table(listings), file=output)
        print(render_search_summary(result, len(listings)), file=output)
    return EXIT_OK


def run_screened_search_command(
    client: CarousellClient,
    request: SearchRequest,
    resolver: PriceResolver,
    options: ScreeningOptions,
    as_json: bool,
    data_dir: Path,
    output: TextIO,
) -> int:
    result = client.search(request)
    listings = result.listings[: request.page_size]
    with closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as connection:
        services = ScreeningServices(
            client=client,
            store=SqliteScreeningStore(connection),
            resolver=resolver,
            clock=_now,
            max_detail_fetches=options.max_detail_fetches,
        )
        screened = screen_listings(listings, request, services)
        shown = [item for item in screened if not (options.hide_multi_item and item.is_multi_item)]
        storage.record_seen_listings(connection, [item.listing for item in shown], _now())
    if as_json:
        print(render_screened_json(result, shown), file=output)
    else:
        print(render_screened_table(shown), file=output)
        print(render_search_summary(result, len(shown)), file=output)
        resolution_on = not isinstance(resolver, NoopPriceResolver)
        print(describe_screening(screened, len(screened) - len(shown), resolution_on, options), file=output)
    return EXIT_OK


def render_screened_json(result: SearchResult, screened: list[ScreenedListing]) -> str:
    """Like `search --json`, with each listing as a ScreenedListing (raw Carousell objects left out)."""
    payload = {
        "screened_listings": [
            item.model_dump(mode="json", exclude={"listing": {"raw"}, "detail": {"raw"}}) for item in screened
        ],
        "total": result.total,
        "total_display": result.total_display,
        "next_page": result.next_page,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def describe_screening(
    screened: list[ScreenedListing], hidden_count: int, resolution_on: bool, options: ScreeningOptions
) -> str:
    """One line, e.g. 'Screening: 6 of 20 multi-item (6 hidden); 8 descriptions checked (detail budget 10); ...'."""
    multi_item_count = sum(item.is_multi_item for item in screened)
    checked_count = sum(item.detail is not None for item in screened)
    hidden = f" ({hidden_count} hidden)" if hidden_count else ""
    descriptions = "description" if checked_count == 1 else "descriptions"
    resolution = (
        "your variant's price read from the description by Claude"
        if resolution_on
        else "price resolution off (ANTHROPIC_API_KEY not set), so YOUR PRICE is the listed price"
    )
    return (
        f"Screening: {multi_item_count} of {len(screened)} multi-item{hidden}; {checked_count} {descriptions} checked "
        f"(detail budget {options.max_detail_fetches}); {resolution}."
    )


def default_price_resolver() -> PriceResolver:
    return build_price_resolver(load_screening_settings())


def run_listing_command(client: CarousellClient, listing_id: str, as_json: bool, data_dir: Path, output: TextIO) -> int:
    detail = client.get_listing(listing_id)
    with closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as connection:
        storage.cache_listing_detail(connection, detail, datetime.now(UTC))
    print(detail.model_dump_json(indent=2) if as_json else render_listing_detail(detail), file=output)
    return EXIT_OK


def read_analysis_spec(spec_argument: str) -> AnalysisSpec:
    """An AnalysisSpec from a JSON file path, or from stdin when the argument is '-'."""
    if spec_argument == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(spec_argument).read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"could not read the analysis spec: {error}") from error
    # Malformed JSON also surfaces as a ValidationError, which main() reports as a usage error.
    return AnalysisSpec.model_validate_json(text)


def run_harvest_command(
    client: CarousellClient,
    request: SearchRequest,
    limits: HarvestLimits,
    reuse_within: timedelta | None,
    data_dir: Path,
    output: TextIO,
) -> int:
    with closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as connection:
        outcome = harvest(client, connection, request, limits, _now, reuse_within)
    print(describe_harvest_outcome(outcome), file=output)
    return EXIT_OK


def run_analyze_command(
    client: CarousellClient,
    spec: AnalysisSpec,
    as_json: bool,
    reuse_within: timedelta | None,
    data_dir: Path,
    output: TextIO,
) -> int:
    with closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as connection:
        run_ids: list[int] = []
        listing_ids: dict[str, None] = {}  # insertion-ordered set: the union of the runs, first occurrence wins
        for request in spec.searches:
            outcome = harvest(client, connection, request, spec.limits, _now, reuse_within)
            if not as_json:
                print(describe_harvest_outcome(outcome), file=output)
            run_ids.append(outcome.run_id)
            listing_ids.update(dict.fromkeys(outcome.listing_ids))
        rows = storage.load_listing_rows(connection, list(listing_ids))
    result = analyse(spec, rows, run_ids, _now())
    print(render_analysis_json(result) if as_json else render_analysis_text(result), file=output)
    return EXIT_OK


def describe_harvest_outcome(outcome: HarvestOutcome) -> str:
    """One line, e.g. 'run 12: 212 listings, 5 pages, stopped: max_pages (37 new, 4 changed)'."""
    pages = f"{outcome.pages_fetched} page{'s' if outcome.pages_fetched != 1 else ''}"
    head = f"{'reused run' if outcome.reused else 'run'} {outcome.run_id}: {len(outcome.listing_ids)} listings, {pages}"
    line = f"{head}, stopped: {outcome.stop_reason.value}"
    if outcome.summary is not None:
        line += f" ({len(outcome.summary.new_ids)} new, {len(outcome.summary.changed_ids)} changed)"
    return line


def _now() -> datetime:
    return datetime.now(UTC)


def run_canary_command(client: CarousellClient, minimum_listings: int, log_path: Path, output: TextIO) -> int:
    started = time.monotonic()
    try:
        outcome = run_canary(client, minimum_listings)
    except Exception as error:  # noqa: BLE001 - a cron health check must always log a line, even on a bug
        outcome = CanaryOutcome(False, f"unexpected {type(error).__name__}: {error}")
    line = format_canary_log_line(outcome, datetime.now(UTC).astimezone(), time.monotonic() - started)
    append_canary_log(log_path, line)
    print(line, file=output)
    return EXIT_OK if outcome.succeeded else EXIT_FAILURE


def run_capture_command(client: CarousellClient, rate_limiter: RateLimiter, output_dir: Path, output: TextIO) -> int:
    if not output_dir.is_dir():
        raise ValueError(f"{output_dir} does not exist; run from the project root or pass --output-dir")
    with httpx.Client(timeout=20.0) as bot_http_client:
        report = capture_fixtures(client, bot_http_client, rate_limiter, output_dir)
    for path in report.written_files:
        print(f"wrote {path} ({path.stat().st_size:,} bytes)", file=output)
    for warning in report.warnings:
        print(f"warning: {warning}", file=output)
    return EXIT_OK


def run_bot_command(
    settings: TelegramSettings, client_factory: ClientFactory, rate_limiter: RateLimiter, data_dir: Path, errors: TextIO
) -> int:
    missing = settings.missing_variables()
    if missing:
        print(describe_missing_bot_settings(missing), file=errors)
        return EXIT_USAGE_ERROR
    with (
        client_factory(rate_limiter) as client,
        closing(storage.open_database(data_dir / DATABASE_FILE_NAME)) as database,
    ):
        run_polling(settings, create_bot_services(client, database, settings.owner_user_id))
    return EXIT_OK


def describe_missing_bot_settings(missing_variables: list[str]) -> str:
    lines = [
        f"error: {' and '.join(missing_variables)} not set.",
        f"Copy .env.example to {DEFAULT_ENV_FILE} and fill it in (README: 'Running the Telegram bot').",
    ]
    if TELEGRAM_OWNER_ID_VARIABLE in missing_variables:
        lines.append(
            "To find your user id: message @userinfobot in Telegram, or send /start to your bot and run `carousearch whoami`."
        )
    return "\n".join(lines)


def run_web_command(
    settings: WebSettings,
    client_factory: ClientFactory,
    rate_limiter: RateLimiter,
    host: str,
    port: int,
    serve: WebServer = uvicorn.run,
) -> int:
    """Serve the web app until Ctrl+C. Its Carousell client and database open when the server starts."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    app = create_app(settings, services_factory=lambda: build_web_services(settings, client_factory(rate_limiter)))
    serve(app, host=host, port=port)
    return EXIT_OK


def run_whoami_command(settings: TelegramSettings, output: TextIO, errors: TextIO) -> int:
    if not settings.bot_token:
        print(describe_missing_bot_settings([TELEGRAM_BOT_TOKEN_VARIABLE]), file=errors)
        return EXIT_USAGE_ERROR
    try:
        sender_lines = asyncio.run(fetch_recent_senders(settings.bot_token))
    except TelegramError as error:
        print(f"error: Telegram refused the request: {error}", file=errors)
        return EXIT_FAILURE
    if not sender_lines:
        print("No recent messages. Send /start to your bot in Telegram, then run this again.", file=output)
    else:
        print("User id     Name (people who messaged the bot in the last 24 hours)", file=output)
        for line in sender_lines:
            print(line, file=output)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
