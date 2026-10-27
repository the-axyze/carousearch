"""Vendor-neutral model selection, shared by the agent and screening's price lookup.

A model is named by a Pydantic AI model string, "<provider>:<model>", e.g. "openai:gpt-5.4-mini" or
"anthropic:claude-haiku-5-5". There are two tiers:

- the planner runs the agent loop (`carousearch ask`, the web app's Ask mode): AGENT_PLANNER_MODEL;
- the worker does the many small jobs: the agent's judge tools and screening's price lookup: AGENT_WORKER_MODEL.

AGENT_MODEL is the older single setting. It still works, as a deprecated alias that sets whichever tier is not set
itself, so an existing `.env` keeps working.

Pure, except build_model, which builds a Pydantic AI model object (it sends nothing until it is used). Pydantic AI is
imported inside build_model only, so reading settings stays cheap for the commands that never call a model.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai.models import Model
    from pydantic_ai.providers import Provider

ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"
AGENT_PLANNER_MODEL_VARIABLE = "AGENT_PLANNER_MODEL"
AGENT_WORKER_MODEL_VARIABLE = "AGENT_WORKER_MODEL"
AGENT_MODEL_VARIABLE = "AGENT_MODEL"  # deprecated: sets both tiers

# OpenAI's current small models (checked on https://developers.openai.com/api/docs/pricing, 2026-10-27).
DEFAULT_PLANNER_MODEL = "openai:gpt-5.4-mini"
DEFAULT_WORKER_MODEL = "openai:gpt-5.4-nano"

# The variable holding each provider's API key, by the provider part of the model string. For any other provider,
# Pydantic AI reads that provider's own environment variables and no key check is made.
PROVIDER_KEY_VARIABLES = {
    "anthropic": ANTHROPIC_API_KEY_VARIABLE,
    "openai": OPENAI_API_KEY_VARIABLE,
    "openai-chat": OPENAI_API_KEY_VARIABLE,
    "openai-responses": OPENAI_API_KEY_VARIABLE,
}


@dataclass(frozen=True)
class ProviderKeys:
    """The provider API keys from settings. Never printed."""

    anthropic: str | None = field(default=None, repr=False)
    openai: str | None = field(default=None, repr=False)

    def for_model(self, model_name: str) -> str | None:
        """The key for the model's provider; None if unset or if the provider is not in PROVIDER_KEY_VARIABLES."""
        keys_by_variable = {ANTHROPIC_API_KEY_VARIABLE: self.anthropic, OPENAI_API_KEY_VARIABLE: self.openai}
        return keys_by_variable.get(PROVIDER_KEY_VARIABLES.get(provider_of(model_name), ""))

    def missing_variable(self, model_name: str) -> str | None:
        """The variable to set when the model's provider needs a key we do not have, else None."""
        variable = PROVIDER_KEY_VARIABLES.get(provider_of(model_name))
        return variable if variable and not self.for_model(model_name) else None


@dataclass(frozen=True)
class ModelChoice:
    planner: str
    worker: str
    from_deprecated_alias: bool  # AGENT_MODEL filled at least one tier


def provider_of(model_name: str) -> str:
    """provider_of("openai:gpt-5.4-mini") == "openai"."""
    return model_name.partition(":")[0]


def parse_model_name(text: str, default: str) -> str:
    """The model string, or `default` when blank. It must name its provider, e.g. "openai:gpt-5.4-mini"."""
    model_name = text.strip() or default
    provider, separator, name = model_name.partition(":")
    if not (provider and separator and name):
        raise ValueError(f"a model must look like provider:model, e.g. {DEFAULT_PLANNER_MODEL}; got {model_name!r}")
    return model_name


def choose_models(values: Mapping[str, str]) -> ModelChoice:
    """The planner and worker models from settings: each tier's own variable, else AGENT_MODEL, else the default."""
    alias = values.get(AGENT_MODEL_VARIABLE, "").strip()
    planner_text = values.get(AGENT_PLANNER_MODEL_VARIABLE, "").strip()
    worker_text = values.get(AGENT_WORKER_MODEL_VARIABLE, "").strip()
    return ModelChoice(
        planner=parse_model_name(planner_text or alias, DEFAULT_PLANNER_MODEL),
        worker=parse_model_name(worker_text or alias, DEFAULT_WORKER_MODEL),
        from_deprecated_alias=bool(alias) and not (planner_text and worker_text),
    )


def load_provider_keys(values: Mapping[str, str]) -> ProviderKeys:
    return ProviderKeys(
        anthropic=values.get(ANTHROPIC_API_KEY_VARIABLE, "").strip() or None,
        openai=values.get(OPENAI_API_KEY_VARIABLE, "").strip() or None,
    )


def build_model(model_name: str, keys: ProviderKeys) -> "Model":
    """The Pydantic AI model `model_name` names, given its provider's key from settings when we hold one.
    Raises pydantic_ai.exceptions.UserError for a provider Pydantic AI does not know."""
    from pydantic_ai.models import infer_model

    api_key = keys.for_model(model_name)
    return infer_model(model_name, provider_factory=lambda provider_name: build_provider(provider_name, api_key))


def build_provider(provider_name: str, api_key: str | None) -> "Provider[Any]":
    from pydantic_ai.providers import infer_provider, infer_provider_class

    if api_key is None:
        return infer_provider(provider_name)  # the provider reads its own environment variables
    return infer_provider_class(provider_name)(api_key=api_key)
