import pytest

from carousearch.agent.cost import ModelUsage, UsageLedger, estimate_cost_usd, format_cost_line
from carousearch.agent.settings import WEB_SEARCH_USD_PER_CALL, ModelPrice

PRICE = ModelPrice(input_usd_per_million=1.0, cached_input_usd_per_million=0.1, output_usd_per_million=4.0)


def test_cached_input_is_charged_at_the_cached_price_and_the_rest_at_the_input_price() -> None:
    usage = ModelUsage(role="planner", model="m", input_tokens=1_000_000, cached_input_tokens=400_000,
                       output_tokens=250_000)

    assert estimate_cost_usd(usage, PRICE) == pytest.approx(0.6 + 0.04 + 1.0)


def test_web_searches_are_added_per_call() -> None:
    usage = ModelUsage(role="planner", model="m", web_searches=3)

    assert estimate_cost_usd(usage, PRICE) == pytest.approx(3 * WEB_SEARCH_USD_PER_CALL)


def test_an_unpriced_model_has_no_estimate() -> None:
    assert estimate_cost_usd(ModelUsage(role="worker", model="m", input_tokens=10), None) is None


class FakeUsage:
    def __init__(self, input_tokens: int, cache_read_tokens: int, output_tokens: int) -> None:
        self.input_tokens, self.cache_read_tokens, self.output_tokens = input_tokens, cache_read_tokens, output_tokens


def test_the_ledger_adds_up_per_role_and_model_and_reports_the_planner_first() -> None:
    ledger = UsageLedger()
    ledger.add_usage("worker", "openai:gpt-5.4-nano", FakeUsage(1000, 0, 100))
    ledger.add_usage("planner", "openai:gpt-5.4-mini", FakeUsage(20_000, 15_000, 500))
    ledger.add_usage("planner", "openai:gpt-5.4-mini", FakeUsage(22_000, 18_000, 700))
    ledger.add("planner", "openai:gpt-5.4-mini", web_searches=1)

    planner, worker = ledger.report()

    assert (planner.role, planner.requests, planner.input_tokens, planner.cached_input_tokens) == (
        "planner", 2, 42_000, 33_000
    )
    assert (planner.output_tokens, planner.web_searches) == (1200, 1)
    expected_planner = (9_000 * 0.75 + 33_000 * 0.075 + 1200 * 4.50) / 1_000_000 + 0.01
    assert planner.estimated_cost_usd == pytest.approx(expected_planner)
    assert (worker.role, worker.requests) == ("worker", 1)
    assert worker.estimated_cost_usd == pytest.approx((1000 * 0.20 + 100 * 1.25) / 1_000_000)


def test_the_cost_line_marks_an_estimate_and_names_each_model() -> None:
    usages = [
        ModelUsage(role="planner", model="openai:gpt-5.4-mini", requests=6, input_tokens=41_200,
                   cached_input_tokens=30_100, output_tokens=1_900, web_searches=1, estimated_cost_usd=0.0213),
        ModelUsage(role="worker", model="openai:gpt-5.4-nano", requests=1, input_tokens=900, output_tokens=80,
                   estimated_cost_usd=0.0003),
    ]

    line = format_cost_line(usages)

    assert line.startswith("Cost (estimate, list prices of 2026-10-27): about US$0.0216.")
    assert "planner openai:gpt-5.4-mini: 6 requests, 41,200 input tokens (30,100 cached), 1,900 output, 1 web search" in line
    assert "worker openai:gpt-5.4-nano: 1 request, 900 input tokens (0 cached), 80 output" in line


def test_the_cost_line_says_when_a_model_has_no_price() -> None:
    usages = [
        ModelUsage(role="planner", model="openai:gpt-5.4-mini", requests=1, estimated_cost_usd=0.01),
        ModelUsage(role="worker", model="custom:model", requests=1),
    ]

    assert "at least US$0.0100 (no price for custom:model)" in format_cost_line(usages)


def test_no_requests_no_cost() -> None:
    assert format_cost_line([]) == "Cost (estimate): no model requests were made."
