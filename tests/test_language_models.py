"""Vendor-neutral model selection (pure, plus building model objects offline)."""

import pytest

from carousearch.language_models import (
    ModelChoice,
    ProviderKeys,
    build_model,
    choose_models,
    parse_model_name,
    provider_of,
)


def test_each_tier_falls_back_to_the_alias_then_to_its_default() -> None:
    assert choose_models({}) == ModelChoice("openai:gpt-5.4-mini", "openai:gpt-5.4-nano", from_deprecated_alias=False)
    assert choose_models({"AGENT_MODEL": "anthropic:claude-haiku-5-5"}) == ModelChoice(
        "anthropic:claude-haiku-5-5", "anthropic:claude-haiku-5-5", from_deprecated_alias=True
    )
    assert choose_models({"AGENT_WORKER_MODEL": " openai:gpt-5-nano ", "AGENT_MODEL": " "}) == ModelChoice(
        "openai:gpt-5.4-mini", "openai:gpt-5-nano", from_deprecated_alias=False
    )


def test_a_model_name_needs_its_provider() -> None:
    assert parse_model_name("  ", "openai:x") == "openai:x"
    assert provider_of("openai-responses:gpt-5.4-mini") == "openai-responses"
    for text in ("gpt-5", ":gpt-5", "openai:"):
        with pytest.raises(ValueError, match="provider:model"):
            parse_model_name(text, "openai:x")


def test_keys_are_matched_to_the_models_provider() -> None:
    keys = ProviderKeys(openai="sk-openai")

    assert keys.for_model("openai-chat:gpt-5") == "sk-openai"
    assert keys.missing_variable("anthropic:claude-haiku-5-5") == "ANTHROPIC_API_KEY"
    assert keys.missing_variable("openai:gpt-5") is None
    assert keys.missing_variable("google:gemini-3-flash") is None
    assert "sk-openai" not in repr(keys)


def test_build_model_makes_the_providers_model_without_any_request() -> None:
    model = build_model("openai:gpt-5.4-nano", ProviderKeys(openai="sk-test"))

    assert (model.system, model.model_name) == ("openai", "gpt-5.4-nano")
