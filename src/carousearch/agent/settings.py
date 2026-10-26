"""The agent's settings: which model answers, the budgets that cap one run, and the provider API keys.

AGENT_MODEL is a Pydantic AI model string, "<provider>:<model>", e.g. "anthropic:claude-haiku-5-5" or
"openai:gpt-5". Everything is read from the environment or the project's `.env` (environment wins).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from carousearch.config import ANTHROPIC_API_KEY_VARIABLE, DEFAULT_ENV_FILE, read_setting_values

AGENT_MODEL_VARIABLE = "AGENT_MODEL"
AGENT_MAX_TOOL_CALLS_VARIABLE = "AGENT_MAX_TOOL_CALLS"
AGENT_MAX_PHOTOS_VARIABLE = "AGENT_MAX_PHOTOS"
AGENT_MAX_LISTINGS_VARIABLE = "AGENT_MAX_LISTINGS"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"

DEFAULT_AGENT_MODEL = "anthropic:claude-haiku-5-5"
DEFAULT_MAX_TOOL_CALLS = 40  # matches the "about 40 Carousell requests per task" of the MCP usage notes
DEFAULT_MAX_PHOTOS = 15
DEFAULT_MAX_LISTINGS = 200  # about four search pages of 48

# The variable holding each provider's API key, by the provider part of AGENT_MODEL. For any other provider,
# Pydantic AI reads that provider's own environment variables and no key check is made.
PROVIDER_KEY_VARIABLES = {
    "anthropic": ANTHROPIC_API_KEY_VARIABLE,
    "openai": OPENAI_API_KEY_VARIABLE,
    "openai-chat": OPENAI_API_KEY_VARIABLE,
    "openai-responses": OPENAI_API_KEY_VARIABLE,
}


@dataclass(frozen=True)
class AgentBudget:
    """Caps on one run. The tools refuse calls beyond them, and the answer's notes say which cap was reached."""

    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_photos: int = DEFAULT_MAX_PHOTOS
    max_listings: int = DEFAULT_MAX_LISTINGS  # listings asked for across all search pages


@dataclass(frozen=True)
class AgentSettings:
    model: str = DEFAULT_AGENT_MODEL
    budget: AgentBudget = field(default_factory=AgentBudget)
    anthropic_api_key: str | None = field(default=None, repr=False)  # never printed
    openai_api_key: str | None = field(default=None, repr=False)

    @property
    def provider(self) -> str:
        """The model string's provider part, e.g. "anthropic"."""
        return self.model.partition(":")[0]

    def api_key(self) -> str | None:
        """The configured key for the model's provider; None if unset or if the provider is not in the table."""
        keys_by_variable = {
            ANTHROPIC_API_KEY_VARIABLE: self.anthropic_api_key,
            OPENAI_API_KEY_VARIABLE: self.openai_api_key,
        }
        return keys_by_variable.get(PROVIDER_KEY_VARIABLES.get(self.provider, ""))

    def missing_key_variable(self) -> str | None:
        """The variable to set when the model's provider needs a key we do not have, else None."""
        variable = PROVIDER_KEY_VARIABLES.get(self.provider)
        return variable if variable and not self.api_key() else None


def load_agent_settings(environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE) -> AgentSettings:
    """Read the agent settings. Unset values get the defaults; a malformed one raises ValueError."""
    values = read_setting_values(environment, env_file)
    budget = AgentBudget(
        max_tool_calls=parse_budget(values, AGENT_MAX_TOOL_CALLS_VARIABLE, DEFAULT_MAX_TOOL_CALLS, minimum=1),
        max_photos=parse_budget(values, AGENT_MAX_PHOTOS_VARIABLE, DEFAULT_MAX_PHOTOS, minimum=0),
        max_listings=parse_budget(values, AGENT_MAX_LISTINGS_VARIABLE, DEFAULT_MAX_LISTINGS, minimum=1),
    )
    return AgentSettings(
        model=parse_model_name(values.get(AGENT_MODEL_VARIABLE, "")),
        budget=budget,
        anthropic_api_key=values.get(ANTHROPIC_API_KEY_VARIABLE, "").strip() or None,
        openai_api_key=values.get(OPENAI_API_KEY_VARIABLE, "").strip() or None,
    )


def parse_model_name(text: str) -> str:
    """The model string, or the default when blank. It must name its provider: "anthropic:claude-haiku-5-5"."""
    model = text.strip() or DEFAULT_AGENT_MODEL
    provider, separator, name = model.partition(":")
    if not (provider and separator and name):
        raise ValueError(f"{AGENT_MODEL_VARIABLE} must look like provider:model, e.g. {DEFAULT_AGENT_MODEL}; got {model!r}")
    return model


def parse_budget(values: Mapping[str, str], variable: str, default: int, minimum: int) -> int:
    text = values.get(variable, "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) < minimum:
        raise ValueError(f"{variable} must be a whole number of {minimum} or more, got {text!r}")
    return int(text)
