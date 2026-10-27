import copy
import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import Client
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch import cli
from carousearch.agent.result import AgentAnswer
from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.carousell.rate_limit import RateLimiter
from carousearch.config import TelegramSettings
from carousearch.filters.schema import Condition, DealMethod, SortOrder
from carousearch.language_models import ProviderKeys
from carousearch.screening.models import PriceSource, ResolvedPrice
from carousearch.screening.price_resolver import NoopPriceResolver, PriceResolver

from fake_carousell import FakeCarousell
from fixture_files import load_json_fixture
from scripted_model import PlanFirst

NO_WAIT = ["--min-interval", "0", "--jitter", "0"]


def run_cli(arguments: list[str], carousell: FakeCarousell, data_dir: Path) -> int:
    return cli.main([*NO_WAIT, "--data-dir", str(data_dir), *arguments], client_factory=carousell.client)


def test_condition_names_and_groups() -> None:
    assert cli.parse_condition_names("used") == [
        Condition.LIKE_NEW, Condition.LIGHTLY_USED, Condition.WELL_USED, Condition.HEAVILY_USED
    ]
    assert cli.parse_condition_names("Like-new, brand new,like_new") == [Condition.LIKE_NEW, Condition.BRAND_NEW]
    assert cli.parse_condition_names("") == []
    with pytest.raises(ValueError, match="unknown condition 'mint'"):
        cli.parse_condition_names("mint")


def test_deal_methods() -> None:
    assert cli.parse_deal_methods("meetup,MAILING") == [DealMethod.MEETUP, DealMethod.MAILING]
    with pytest.raises(ValueError, match="unknown deal method"):
        cli.parse_deal_methods("courier")


def test_repeated_fields_become_lists() -> None:
    assert cli.parse_category_fields(["mobile_storage=A", "mobile_storage=B", "mobile_color=C"]) == {
        "mobile_storage": ["A", "B"],
        "mobile_color": "C",
    }
    with pytest.raises(ValueError, match="KEY=VALUE"):
        cli.parse_category_fields(["mobile_storage"])


def test_search_arguments_become_a_search_request() -> None:
    arguments = cli.build_argument_parser().parse_args(
        ["search", "iphone 15", "--price-max", "900", "--condition", "used", "--sort", "recent", "--limit", "10",
         "--category", "5707", "--field", "mobile_storage=MOBILE_STORAGE_256_GB", "--deal-method", "meetup",
         "--buyer-protection"]
    )

    request = cli.search_request_from_arguments(arguments)

    assert request.query == "iphone 15"
    assert request.price_max == 900
    assert request.conditions == cli.CONDITION_GROUPS["used"]
    assert request.sort == SortOrder.RECENT
    assert request.page_size == 10
    assert request.category == "5707"
    assert request.category_fields == {"mobile_storage": "MOBILE_STORAGE_256_GB"}
    assert request.deal_methods == [DealMethod.MEETUP]
    assert request.buyer_protection_only is True


def test_search_prints_a_table_and_records_seen_listings(tmp_path: Path, capsys) -> None:
    exit_code = run_cli(["search", "iphone 15", "--limit", "3"], FakeCarousell(), tmp_path)

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert exit_code == 0
    assert lines[0].split()[:4] == ["ID", "PRICE", "CONDITION", "LIKES"]
    assert len(lines) == 1 + 3 + 1  # header, 3 listings, summary
    assert "https://www.carousell.sg/p/" in lines[1]
    assert (tmp_path / "carousearch.db").exists()


def test_search_json_output_omits_raw(tmp_path: Path, capsys) -> None:
    run_cli(["search", "iphone 15", "--json"], FakeCarousell(), tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert result["listings"] and "raw" not in result["listings"][0]
    assert result["next_page"]


def test_search_with_no_matches_prints_an_empty_table_and_the_summary(tmp_path: Path, capsys) -> None:
    no_results = httpx.Response(200, json=load_json_fixture("search_no_results.json"))

    exit_code = run_cli(["search", "r15 bike with low quality image"], FakeCarousell([no_results]), tmp_path)

    header, summary = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert header.split()[:2] == ["ID", "PRICE"]
    assert summary == "Showing 0 of 0 results; no more pages."


def test_invalid_search_is_a_usage_error_without_network(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()

    exit_code = run_cli(["search", "sofa", "--price-min", "500", "--price-max", "100"], carousell, tmp_path)

    assert exit_code == 2
    assert "price_min must not be greater" in capsys.readouterr().err
    assert carousell.requests == []


def test_listing_prints_details(tmp_path: Path, capsys) -> None:
    exit_code = run_cli(["listing", "1469923094"], FakeCarousell(), tmp_path)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Seller:" in output and "Description:" in output


def test_blocked_search_exits_with_failure(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell(api_responses=[httpx.Response(403), httpx.Response(403)])

    exit_code = run_cli(["search", "sofa"], carousell, tmp_path)

    assert exit_code == 1
    assert "CarousellBlockedError" in capsys.readouterr().err


def test_canary_success_writes_ok_log_line(tmp_path: Path, capsys) -> None:
    exit_code = run_cli(["canary", "--min-listings", "3"], FakeCarousell(), tmp_path)

    log_lines = (tmp_path / "canary.log").read_text().splitlines()
    assert exit_code == 0
    assert len(log_lines) == 1 and " OK " in log_lines[0]
    assert capsys.readouterr().out.strip() == log_lines[0]


def test_canary_with_too_few_listings_fails(tmp_path: Path) -> None:
    exit_code = run_cli(["canary", "--min-listings", "50"], FakeCarousell(), tmp_path)

    log_line = (tmp_path / "canary.log").read_text().strip()
    assert exit_code == 1
    assert " FAIL " in log_line and "expected >= 50" in log_line


def test_canary_blocked_fails_with_one_line_reason(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell(api_responses=[httpx.Response(403), httpx.Response(403)])

    exit_code = run_cli(["canary"], carousell, tmp_path)

    output = capsys.readouterr().out.strip()
    assert exit_code == 1
    assert len(output.splitlines()) == 1
    assert "FAIL search failed: CarousellBlockedError" in output


def test_bot_without_settings_exits_with_a_clear_message(tmp_path: Path, capsys) -> None:
    def client_factory_that_must_not_run(rate_limiter: RateLimiter) -> None:
        raise AssertionError("no Carousell client should be created")

    exit_code = cli.run_bot_command(
        TelegramSettings(bot_token=None, owner_user_id=None), client_factory_that_must_not_run,
        RateLimiter(0, 0), tmp_path, sys.stderr,
    )

    message = capsys.readouterr().err
    assert exit_code == 2
    assert "TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID not set" in message
    assert "carousearch whoami" in message


def test_bot_with_only_the_owner_missing_names_that_setting(tmp_path: Path, capsys) -> None:
    exit_code = cli.run_bot_command(
        TelegramSettings(bot_token="123:abc", owner_user_id=None), FakeCarousell().client, RateLimiter(0, 0),
        tmp_path, sys.stderr,
    )

    assert exit_code == 2
    assert "error: TELEGRAM_OWNER_ID not set" in capsys.readouterr().err


# --- harvest and analyze ---------------------------------------------------------


def test_harvest_prints_the_run_outcome_and_stores_listings(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()  # the fixture page says hasMore, then repeats itself, which ends the run

    exit_code = run_cli(["harvest", "iphone 15", "--max-pages", "3"], carousell, tmp_path)

    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert output.startswith("run 1: 5 listings, 2 pages, stopped: no_more_pages (5 new, 0 changed)")
    assert len(carousell.api_requests) == 2
    assert json.loads(carousell.api_requests[0].content)["count"] == 48


def test_harvest_reuses_a_recent_run_unless_told_not_to(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()
    run_cli(["harvest", "iphone 15"], carousell, tmp_path)
    run_cli(["harvest", "iphone 15"], carousell, tmp_path)
    assert capsys.readouterr().out.splitlines()[1].startswith("reused run 1: 5 listings")
    requests_before = len(carousell.api_requests)

    run_cli(["harvest", "iphone 15", "--no-reuse"], carousell, tmp_path)

    assert capsys.readouterr().out.startswith("run 2:")
    assert len(carousell.api_requests) > requests_before


def test_harvest_max_age_needs_sort_recent(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()
    exit_code = run_cli(["harvest", "iphone 15", "--max-age-days", "7"], carousell, tmp_path)
    assert exit_code == 2
    assert "sort=recent" in capsys.readouterr().err
    assert carousell.requests == []


def test_analyze_runs_the_spec_and_prints_statistics(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "question": "iPhone 15 prices",
        "searches": [{"query": "iphone 15", "sort": "recent"}],
        "limits": {"max_pages": 2},
        "group_by": "condition",
    }))

    exit_code = run_cli(["analyze", "--spec", str(spec_path)], FakeCarousell(), tmp_path)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.startswith("run 1:")
    assert "iPhone 15 prices" in output
    assert "median" in output.lower()


def test_analyze_json_output_round_trips(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"question": "q", "searches": [{"query": "iphone 15"}]}))

    exit_code = run_cli(["analyze", "--spec", str(spec_path), "--json"], FakeCarousell(), tmp_path)

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["spec"]["question"] == "q"
    assert result["fetched_count"] == 5
    assert result["overall"]["count"] >= 1


def test_analyze_rejects_a_bad_spec_without_network(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"question": "q", "searches": [], "unexpected": 1}))

    exit_code = run_cli(["analyze", "--spec", str(spec_path)], carousell, tmp_path)

    assert exit_code == 2
    assert "searches" in capsys.readouterr().err
    assert carousell.requests == []


def test_analyze_reports_a_missing_spec_file(tmp_path: Path, capsys) -> None:
    exit_code = run_cli(["analyze", "--spec", str(tmp_path / "missing.json")], FakeCarousell(), tmp_path)
    assert exit_code == 2
    assert "could not read the analysis spec" in capsys.readouterr().err


def test_the_example_spec_in_docs_is_valid() -> None:
    example = Path(__file__).parents[1] / "docs" / "examples" / "analysis_spec.example.json"
    spec = cli.read_analysis_spec(str(example))
    assert len(spec.searches) == 2
    assert spec.limits.max_age_days == 30


def test_analyze_exits_with_failure_when_carousell_blocks(tmp_path: Path, capsys) -> None:
    blocked = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="<title>Just a moment</title>")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"question": "q", "searches": [{"query": "iphone 15"}]}))

    exit_code = run_cli(["analyze", "--spec", str(spec_path)], FakeCarousell(page_responses={None: blocked}), tmp_path)

    assert exit_code == 1
    assert capsys.readouterr().err.startswith("error: CarousellBlockedError")


def test_analyze_reads_the_spec_from_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"question": "from stdin", "searches": [{"query": "iphone 15"}]})))

    exit_code = run_cli(["analyze", "--spec", "-"], FakeCarousell(), tmp_path)

    assert exit_code == 0
    assert "from stdin" in capsys.readouterr().out


def test_analyze_rejects_an_age_cutoff_without_recent_sort_before_any_request(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"question": "q", "searches": [{"query": "x"}], "limits": {"max_age_days": 7}}))

    exit_code = run_cli(["analyze", "--spec", str(spec_path)], carousell, tmp_path)

    assert exit_code == 2
    assert "sort=recent" in capsys.readouterr().err
    assert carousell.requests == []


# --- search --screen -----------------------------------------------------------------------

CATALOGUE_TITLE = "iPhone 15 / 15 Pro / 15 Pro Max all storage"


def carousell_with_one_catalogue_post() -> FakeCarousell:
    """The saved search, with the second listing (1463757293, S$750) retitled as a catalogue post."""
    response_json = copy.deepcopy(load_json_fixture("search_price_condition.json"))
    cards = [item["listingCard"] for item in response_json["data"]["results"] if "listingCard" in item]
    cards[1]["title"] = CATALOGUE_TITLE
    return FakeCarousell(api_responses=[httpx.Response(200, json=response_json)])


class FixedPriceResolver:
    def resolve(self, query: str, title: str, description: str, listed_price: float | None) -> ResolvedPrice:
        return ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="15 Pro 256GB - $850")


def run_screening_cli(
    arguments: list[str], carousell: FakeCarousell, data_dir: Path, resolver: PriceResolver | None = None
) -> int:
    return cli.main(
        [*NO_WAIT, "--data-dir", str(data_dir), *arguments],
        client_factory=carousell.client,
        price_resolver_factory=lambda: resolver or NoopPriceResolver(),
    )


def detail_requests(carousell: FakeCarousell) -> list:
    return [request for request in carousell.api_requests if request.url.path.startswith("/ds/listing-detail/")]


def test_screen_adds_your_price_and_flags_columns(tmp_path: Path, capsys) -> None:
    carousell = carousell_with_one_catalogue_post()

    exit_code = run_screening_cli(["search", "iphone 15 pro 256gb", "--limit", "5", "--screen"], carousell, tmp_path)

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert lines[0].split()[:6] == ["ID", "PRICE", "YOUR", "PRICE", "FLAGS", "CONDITION"]
    catalogue_row = next(line for line in lines if line.startswith("1463757293"))
    assert "Multi-item title" in catalogue_row
    assert len(detail_requests(carousell)) == 1
    assert lines[-1] == (
        "Screening: 1 of 5 multi-item; 1 description checked (detail budget 10); "
        "price resolution off (no API key for the worker model), so YOUR PRICE is the listed price."
    )


def test_screen_shows_the_resolved_price(tmp_path: Path, capsys) -> None:
    run_screening_cli(
        ["search", "iphone 15 pro 256gb", "--limit", "5", "--screen"],
        carousell_with_one_catalogue_post(),
        tmp_path,
        resolver=FixedPriceResolver(),
    )

    output = capsys.readouterr().out
    catalogue_row = next(line for line in output.splitlines() if line.startswith("1463757293"))
    assert "S$850 (desc)" in catalogue_row
    assert "read from the description by the price resolver" in output


def test_screen_json_has_the_screened_listing_fields(tmp_path: Path, capsys) -> None:
    arguments = ["search", "iphone 15 pro", "--limit", "5", "--screen", "--json"]
    run_screening_cli(arguments, carousell_with_one_catalogue_post(), tmp_path)

    result = json.loads(capsys.readouterr().out)
    screened = {item["listing"]["id"]: item for item in result["screened_listings"]}
    catalogue = screened["1463757293"]
    assert set(catalogue) == {"listing", "detail", "tags", "resolved_price", "is_multi_item"}
    assert catalogue["is_multi_item"] is True
    assert catalogue["tags"][0]["code"] == "MULTI_ITEM_TITLE"
    assert catalogue["resolved_price"]["source"] == "listed"
    assert "raw" not in catalogue["listing"] and "raw" not in catalogue["detail"]
    assert screened["1469923094"]["detail"] is None and screened["1469923094"]["tags"] == []
    assert result["next_page"]


def test_hide_multi_item_implies_screening(tmp_path: Path, capsys) -> None:
    exit_code = run_screening_cli(
        ["search", "iphone 15 pro", "--limit", "5", "--hide-multi-item"], carousell_with_one_catalogue_post(), tmp_path
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "1463757293" not in output
    assert "Showing 4 of" in output
    assert "1 of 5 multi-item (1 hidden)" in output


def test_negative_detail_budget_is_a_usage_error(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()

    exit_code = run_screening_cli(["search", "iphone", "--screen", "--max-detail-fetches", "-1"], carousell, tmp_path)

    assert exit_code == 2
    assert "--max-detail-fetches" in capsys.readouterr().err
    assert carousell.requests == []


# --- web ---------------------------------------------------------------------------


def test_web_serves_the_app_on_the_given_port_with_services_built_at_startup(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from carousearch.config import ScreeningSettings
    from carousearch.web.app import WebSettings

    carousell = FakeCarousell()
    served = {}

    def fake_serve(app, host: str, port: int) -> None:
        served.update(app=app, host=host, port=port)

    agent = AgentSettings(planner_model="openai:gpt-5", keys=ProviderKeys(openai="sk-test"))
    settings = WebSettings(data_dir=tmp_path, screening=ScreeningSettings(), agent=agent)
    exit_code = cli.run_web_command(settings, carousell.client, RateLimiter(0, 0), "127.0.0.1", 9000, serve=fake_serve)

    assert exit_code == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 9000)
    assert carousell.requests == []  # nothing is opened until the server starts
    with TestClient(served["app"]) as client:
        page = client.get("/results?q=iphone+15").text
        assert served["app"].state.services.agent == agent
    assert page.count('class="card"') == 5
    assert (tmp_path / "carousearch.db").exists()


def test_web_loads_the_agent_settings_for_ask_mode(tmp_path: Path, monkeypatch) -> None:
    passed = {}

    def fake_run_web_command(settings, client_factory, rate_limiter, host: str, port: int) -> int:
        passed["settings"] = settings
        return 0

    monkeypatch.setattr(cli, "run_web_command", fake_run_web_command)
    monkeypatch.setenv("AGENT_PLANNER_MODEL", "openai:gpt-5")
    monkeypatch.setenv("AGENT_WORKER_MODEL", "openai:gpt-5-nano")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_MAX_PHOTOS", "3")

    exit_code = cli.main(["--data-dir", str(tmp_path), "web"])

    agent = passed["settings"].agent
    assert exit_code == 0
    assert (agent.planner_model, agent.worker_model) == ("openai:gpt-5", "openai:gpt-5-nano")
    assert (agent.budget.max_photos, agent.missing_key_variable()) == (3, None)


def test_web_with_a_malformed_agent_setting_is_a_usage_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "run_web_command", lambda *arguments: pytest.fail("the server must not start"))
    monkeypatch.setenv("AGENT_MAX_PHOTOS", "-1")

    exit_code = cli.main(["--data-dir", str(tmp_path), "web"])

    assert exit_code == 2
    assert "AGENT_MAX_PHOTOS" in capsys.readouterr().err


def test_importing_the_cli_leaves_pydantic_ai_and_the_mcp_sdk_unloaded() -> None:
    """Every command imports the web app, so Ask mode must import Pydantic AI and the MCP SDK only when a run starts."""
    check = "import sys, carousearch.cli; print('pydantic_ai' in sys.modules, 'mcp' in sys.modules)"
    completed = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == "False False"


def test_web_defaults_to_port_8765_on_this_machine_only() -> None:
    arguments = cli.build_argument_parser().parse_args(["web"])
    assert (arguments.host, arguments.port) == ("127.0.0.1", 8765)


# --- mcp ---------------------------------------------------------------------------


def test_mcp_help_lists_the_transport_and_port_options(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["mcp", "--help"])

    help_text = capsys.readouterr().out
    assert exit_info.value.code == 0
    assert "--transport {stdio,http}" in help_text
    assert "--port" in help_text


def test_mcp_defaults_to_stdio_and_to_port_8766_for_http() -> None:
    parser = cli.build_argument_parser()

    assert parser.parse_args(["mcp"]).transport == "stdio"
    http = parser.parse_args(["mcp", "--transport", "http"])
    assert (http.transport, http.port) == ("http", 8766)


def test_mcp_serves_a_server_wired_to_carousell_and_the_detail_cache(tmp_path: Path) -> None:
    carousell = FakeCarousell()
    served = {}

    async def fetch_one_listing_twice(server) -> None:
        async with Client(server) as client:
            for _ in range(2):  # the second call is answered from the SQLite cache, in another worker thread
                result = await client.call_tool("listing_details", {"listing_id": "1469923094"})
                assert result.is_error is False

    def fake_serve(server, transport: str, port: int) -> None:
        served.update(transport=transport, port=port, requests_before_any_tool_call=len(carousell.requests))
        anyio.run(fetch_one_listing_twice, server)

    exit_code = cli.run_mcp_command(tmp_path, carousell.client, RateLimiter(0, 0), "http", 9001, serve=fake_serve)

    assert exit_code == 0
    assert (served["transport"], served["port"]) == ("http", 9001)
    assert served["requests_before_any_tool_call"] == 0
    assert len(carousell.api_requests) == 1


# --- ask ---------------------------------------------------------------------------


def client_factory_that_must_not_run(rate_limiter: RateLimiter) -> None:
    raise AssertionError("no Carousell client should be created")


def unused_worker(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise AssertionError("no judge tool is called in these tests")


def run_ask(
    carousell: FakeCarousell, data_dir: Path, as_json: bool, script: Callable, settings: AgentSettings | None = None
) -> int:
    """`ask` with the planning phase answered by PlanFirst and the execution phase by `script`."""
    return cli.run_ask_command(
        "Find an iPhone 15 under S$600.", settings or AgentSettings(), data_dir, carousell.client, RateLimiter(0, 0),
        as_json, sys.stdout, sys.stderr, model=FunctionModel(PlanFirst(script)),
        worker_model=FunctionModel(unused_worker),
    )


def search_then_details_twice_then_answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A scripted model: one search, the same listing's details twice (the second from the SQLite cache), an answer."""
    turn = sum(isinstance(message, ModelResponse) for message in messages)
    if turn == 0:
        return ModelResponse(parts=[ToolCallPart("search_listings", {"request": {"query": "iphone 15"}, "limit": 3})])
    if turn in (1, 2):
        return ModelResponse(parts=[ToolCallPart("listing_details", {"listing_id": "1469923094"})])
    kept = {
        "listing_id": "1469923094", "title": "iPhone 15 256GB Blue", "price": 579.0,
        "url": "https://www.carousell.sg/p/1469923094/", "seller": "seller", "reason": "under S$600, one phone",
    }
    dropped = {"listing_id": "1463757293", "title": "iPhone 15 Pro", "reason": "a Pro, not an iPhone 15"}
    return ModelResponse(
        parts=[ToolCallPart(OUTPUT_TOOL_NAME, {"kept": [kept], "dropped": [dropped], "notes": "One page only."})]
    )


@pytest.mark.parametrize(
    ("model", "variable"), [("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"), ("openai:gpt-5", "OPENAI_API_KEY")]
)
def test_ask_without_the_models_provider_key_exits_with_a_clear_message(
    tmp_path: Path, capsys, model: str, variable: str
) -> None:
    exit_code = cli.run_ask_command(
        "find a switch", AgentSettings(planner_model=model, worker_model=model), tmp_path,
        client_factory_that_must_not_run, RateLimiter(0, 0), False, sys.stdout, sys.stderr,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert f"error: {variable} not set; the model {model} needs it." in captured.err
    assert ".env" in captured.err and "--model" in captured.err and "AGENT_PLANNER_MODEL" in captured.err


def test_ask_json_output_parses_back_into_an_agent_answer(tmp_path: Path, capsys) -> None:
    carousell = FakeCarousell()

    exit_code = run_ask(carousell, tmp_path, as_json=True, script=search_then_details_twice_then_answer)

    answer = AgentAnswer.model_validate_json(capsys.readouterr().out)
    assert exit_code == 0
    assert [item.listing_id for item in answer.kept] == ["1469923094"]
    assert [item.reason for item in answer.dropped] == ["a Pro, not an iPhone 15"]
    assert answer.notes == "One page only."
    assert [call.tool_name for call in answer.tool_trace] == ["search_listings", "listing_details", "listing_details"]
    assert {call.status for call in answer.tool_trace} == {"ok"}
    assert len(carousell.api_requests) == 2  # the search and one detail; the repeat came from the SQLite cache
    (run_directory,) = (tmp_path / "runs").iterdir()
    assert (run_directory / "listings.db").exists() and (run_directory / "listings.json").exists()
    assert [usage.role for usage in answer.usage] == ["planner"]


def test_ask_prints_the_trace_then_kept_dropped_and_notes(tmp_path: Path, capsys) -> None:
    exit_code = run_ask(FakeCarousell(), tmp_path, as_json=False, script=search_then_details_twice_then_answer)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.startswith("Tool calls (3): listing_details 2, search_listings 1:\n")
    assert output.index("Kept (1):") < output.index("Dropped (1):") < output.index("Notes:\n  One page only.")
    assert output.index("Not shortlisted: 0 more listings seen.") < output.index("Notes:")
    assert output.rstrip().splitlines()[-1].startswith("Cost (estimate, list prices of ")
    assert "https://www.carousell.sg/p/1469923094/  under S$600, one phone" in output


def test_ask_reports_a_failed_model_request_and_exits_with_failure(tmp_path: Path, capsys) -> None:
    def refused_key(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=401, model_name="claude-haiku-5-5", body="invalid x-api-key")

    exit_code = run_ask(FakeCarousell(), tmp_path, as_json=False, script=refused_key)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "error: the agent run failed: ModelHTTPError" in captured.err


def test_ask_options_override_the_models_and_the_photo_budget() -> None:
    arguments = cli.build_argument_parser().parse_args(
        ["ask", "find a bike", "--model", "openai:gpt-5", "--worker-model", "openai:gpt-5-nano", "--max-photos", "3",
         "--json"]
    )

    settings = cli.agent_settings_from_arguments(AgentSettings(budget=AgentBudget(max_tool_calls=10)), arguments)

    assert (arguments.prompt, arguments.json) == ("find a bike", True)
    assert (settings.planner_model, settings.worker_model) == ("openai:gpt-5", "openai:gpt-5-nano")
    assert settings.budget == AgentBudget(max_tool_calls=10, max_photos=3)


def test_ask_without_model_options_keeps_the_settings_models() -> None:
    arguments = cli.build_argument_parser().parse_args(["ask", "find a bike"])
    configured = AgentSettings(planner_model="anthropic:a", worker_model="anthropic:b")

    settings = cli.agent_settings_from_arguments(configured, arguments)

    assert (settings.planner_model, settings.worker_model) == ("anthropic:a", "anthropic:b")


def test_ask_options_are_checked_before_anything_runs() -> None:
    parser = cli.build_argument_parser()

    with pytest.raises(ValueError, match="--max-photos must be 0 or more"):
        cli.agent_settings_from_arguments(AgentSettings(), parser.parse_args(["ask", "x", "--max-photos", "-1"]))
    with pytest.raises(ValueError, match="provider:model"):
        cli.agent_settings_from_arguments(AgentSettings(), parser.parse_args(["ask", "x", "--model", "gpt-5"]))
