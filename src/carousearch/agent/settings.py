"""The agent's settings: the planner and worker models, the budgets that cap one run, the provider API keys, and the
price table behind the run's cost estimate.

Models are Pydantic AI model strings, "<provider>:<model>" (carousearch.language_models): AGENT_PLANNER_MODEL runs
the agent loop, AGENT_WORKER_MODEL the judge tools; AGENT_MODEL is a deprecated alias for both. Everything is read
from the environment or the project's `.env` (environment wins).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from carousearch.config import DEFAULT_ENV_FILE, read_setting_values
from carousearch.language_models import (
    AGENT_MODEL_VARIABLE,
    AGENT_PLANNER_MODEL_VARIABLE,
    AGENT_WORKER_MODEL_VARIABLE,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_WORKER_MODEL,
    ProviderKeys,
    choose_models,
    load_provider_keys,
)

logger = logging.getLogger(__name__)

AGENT_MAX_TOOL_CALLS_VARIABLE = "AGENT_MAX_TOOL_CALLS"
AGENT_MAX_PHOTOS_VARIABLE = "AGENT_MAX_PHOTOS"
AGENT_MAX_LISTINGS_VARIABLE = "AGENT_MAX_LISTINGS"
AGENT_MAX_WEB_SEARCHES_VARIABLE = "AGENT_MAX_WEB_SEARCHES"

DEFAULT_MAX_TOOL_CALLS = 40  # matches the "about 40 Carousell requests per task" of the MCP usage notes
DEFAULT_MAX_PHOTOS = 15
DEFAULT_MAX_LISTINGS = 200  # about four search pages of 48
DEFAULT_MAX_WEB_SEARCHES = 3


@dataclass(frozen=True)
class AgentBudget:
    """Caps on one run. The tools refuse calls beyond them, and the answer's notes say which cap was reached."""

    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS  # every tool call, and every Carousell request a tool makes
    max_photos: int = DEFAULT_MAX_PHOTOS
    max_listings: int = DEFAULT_MAX_LISTINGS  # listings asked for across all search pages
    max_web_searches: int = DEFAULT_MAX_WEB_SEARCHES  # the provider's hosted web search, where it has one


@dataclass(frozen=True)
class ModelPrice:
    """US$ per million tokens. Cached input is what a prompt-cache hit costs instead of the input price."""

    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float


# List prices for the cost estimate, by model name without its provider. Checked on PRICES_CHECKED_ON:
# OpenAI at https://developers.openai.com/api/docs/pricing (standard tier, short context); Claude Haiku 5.5 from
# its launch coverage (prompts up to 100K tokens). Cache writes are counted at the input price.
PRICES_CHECKED_ON = "2026-10-27"
MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-5.4-mini": ModelPrice(0.75, 0.075, 4.50),
    "gpt-5.4-nano": ModelPrice(0.20, 0.02, 1.25),
    "gpt-5-mini": ModelPrice(0.25, 0.025, 2.00),
    "gpt-5-nano": ModelPrice(0.05, 0.005, 0.40),
    "claude-haiku-5-5": ModelPrice(0.10, 0.01, 0.50),
}
# OpenAI and Anthropic both charge US$10 per 1,000 hosted web searches, plus the result tokens at the model's rate.
WEB_SEARCH_USD_PER_CALL = 0.01


def price_for(model_name: str, prices: Mapping[str, ModelPrice] = MODEL_PRICES) -> ModelPrice | None:
    """The price of "openai:gpt-5.4-mini" (looked up as "gpt-5.4-mini"), or None for a model not in the table."""
    return prices.get(model_name.rpartition(":")[2])


@dataclass(frozen=True)
class AgentSettings:
    planner_model: str = DEFAULT_PLANNER_MODEL
    worker_model: str = DEFAULT_WORKER_MODEL
    budget: AgentBudget = field(default_factory=AgentBudget)
    keys: ProviderKeys = field(default_factory=ProviderKeys)  # never printed

    def missing_key_variable(self) -> str | None:
        """The variable to set when the planner's or the worker's provider needs a key we do not have, else None."""
        return self.keys.missing_variable(self.planner_model) or self.keys.missing_variable(self.worker_model)

    def missing_key_model(self) -> str | None:
        """The model whose key is missing (the planner first), else None."""
        for model_name in (self.planner_model, self.worker_model):
            if self.keys.missing_variable(model_name):
                return model_name
        return None


def load_agent_settings(environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> AgentSettings:
    """Read the agent settings. Unset values get the defaults; a malformed one raises ValueError."""
    values = read_setting_values(environment, env_file)
    budget = AgentBudget(
        max_tool_calls=parse_budget(values, AGENT_MAX_TOOL_CALLS_VARIABLE, DEFAULT_MAX_TOOL_CALLS, minimum=1),
        max_photos=parse_budget(values, AGENT_MAX_PHOTOS_VARIABLE, DEFAULT_MAX_PHOTOS, minimum=0),
        max_listings=parse_budget(values, AGENT_MAX_LISTINGS_VARIABLE, DEFAULT_MAX_LISTINGS, minimum=1),
        max_web_searches=parse_budget(values, AGENT_MAX_WEB_SEARCHES_VARIABLE, DEFAULT_MAX_WEB_SEARCHES, minimum=0),
    )
    models = choose_models(values)
    if models.from_deprecated_alias:
        logger.warning(
            "%s is deprecated; set %s and %s instead (README: 'Ask the agent')",
            AGENT_MODEL_VARIABLE,
            AGENT_PLANNER_MODEL_VARIABLE,
            AGENT_WORKER_MODEL_VARIABLE,
        )
    return AgentSettings(
        planner_model=models.planner, worker_model=models.worker, budget=budget, keys=load_provider_keys(values)
    )


def parse_budget(values: Mapping[str, str], variable: str, default: int, minimum: int) -> int:
    text = values.get(variable, "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) < minimum:
        raise ValueError(f"{variable} must be a whole number of {minimum} or more, got {text!r}")
    return int(text)
