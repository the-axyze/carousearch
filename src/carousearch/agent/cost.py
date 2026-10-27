"""What a run cost: token usage per model, and an estimate in US$ from the price table in agent/settings.py.

Pure: the run hands in token counts (from Pydantic AI's usage objects); nothing here reads the clock or the network.
The figures are estimates: list prices, cache writes at the input price, and no taxes or account discounts.
"""

from collections.abc import Callable, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from carousearch.agent.settings import PRICES_CHECKED_ON, WEB_SEARCH_USD_PER_CALL, ModelPrice, price_for

ModelRole = Literal["planner", "worker"]


class ModelUsage(BaseModel):
    """One model's token use in a run. input_tokens includes the cached ones."""

    role: ModelRole
    model: str
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    estimated_cost_usd: float | None = None  # None when the model is not in the price table


class UsageLedger:
    """Adds up token use per (role, model) while a run goes on."""

    def __init__(self) -> None:
        self._usage: dict[tuple[ModelRole, str], ModelUsage] = {}

    def add(
        self,
        role: ModelRole,
        model: str,
        *,
        requests: int = 0,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        web_searches: int = 0,
    ) -> None:
        current = self._usage.get((role, model)) or ModelUsage(role=role, model=model)
        self._usage[(role, model)] = current.model_copy(
            update={
                "requests": current.requests + requests,
                "input_tokens": current.input_tokens + input_tokens,
                "cached_input_tokens": current.cached_input_tokens + cached_input_tokens,
                "output_tokens": current.output_tokens + output_tokens,
                "web_searches": current.web_searches + web_searches,
            }
        )

    def add_usage(self, role: ModelRole, model: str, usage: Any, requests: int = 1) -> None:
        """Add a Pydantic AI RequestUsage or RunUsage (input_tokens, cache_read_tokens, output_tokens)."""
        self.add(
            role,
            model,
            requests=requests,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
        )

    def add_report(self, usages: Sequence[ModelUsage]) -> None:
        """Add an earlier phase's report (for example the planning phase's) to this run's totals."""
        for usage in usages:
            self.add(
                usage.role,
                usage.model,
                requests=usage.requests,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                web_searches=usage.web_searches,
            )

    def report(self, find_price: Callable[[str], ModelPrice | None] = price_for) -> list[ModelUsage]:
        """Planner first, then workers, each with its estimated cost."""
        ordered = sorted(self._usage.values(), key=lambda usage: (usage.role != "planner", usage.model))
        return [usage.model_copy(update={"estimated_cost_usd": estimate_cost_usd(usage, find_price(usage.model))})
                for usage in ordered]


def total_cost_usd(usages: Sequence[ModelUsage]) -> float:
    """The sum of the priced models' estimates (models without a price count as 0)."""
    return sum(usage.estimated_cost_usd or 0.0 for usage in usages)


def estimate_cost_usd(usage: ModelUsage, price: ModelPrice | None) -> float | None:
    """Uncached input, cached input and output tokens at their prices, plus the hosted web searches."""
    if price is None:
        return None
    uncached_input_tokens = max(usage.input_tokens - usage.cached_input_tokens, 0)
    token_cost = (
        uncached_input_tokens * price.input_usd_per_million
        + usage.cached_input_tokens * price.cached_input_usd_per_million
        + usage.output_tokens * price.output_usd_per_million
    ) / 1_000_000
    return token_cost + usage.web_searches * WEB_SEARCH_USD_PER_CALL


def format_cost_line(usages: Sequence[ModelUsage]) -> str:
    """'Cost (estimate, list prices of 2026-10-27): about US$0.0123. planner openai:gpt-5.4-mini: 6 requests,
    41,200 input tokens (30,100 cached), 1,900 output, 1 web search; worker openai:gpt-5.4-nano: ...'."""
    if not usages:
        return "Cost (estimate): no model requests were made."
    known_costs = [usage.estimated_cost_usd for usage in usages if usage.estimated_cost_usd is not None]
    unpriced = [usage.model for usage in usages if usage.estimated_cost_usd is None]
    total = f"about US${sum(known_costs):.4f}"
    if unpriced:
        total = f"at least US${sum(known_costs):.4f} (no price for {', '.join(unpriced)})"
    details = "; ".join(describe_usage(usage) for usage in usages)
    return f"Cost (estimate, list prices of {PRICES_CHECKED_ON}): {total}. {details}."


def describe_usage(usage: ModelUsage) -> str:
    parts = [
        f"{usage.requests:,} request{'' if usage.requests == 1 else 's'}",
        f"{usage.input_tokens:,} input tokens ({usage.cached_input_tokens:,} cached)",
        f"{usage.output_tokens:,} output",
    ]
    if usage.web_searches:
        parts.append(f"{usage.web_searches} web search{'' if usage.web_searches == 1 else 'es'}")
    return f"{usage.role} {usage.model}: " + ", ".join(parts)
