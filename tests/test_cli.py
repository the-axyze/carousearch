import json
import sys
from pathlib import Path

import httpx
import pytest

from carousearch import cli
from carousearch.carousell.rate_limit import RateLimiter
from carousearch.config import TelegramSettings
from carousearch.filters.schema import Condition, DealMethod, SortOrder

from fake_carousell import FakeCarousell

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
