import logging
from pathlib import Path

import pytest

from carousearch.agent.settings import (
    MODEL_PRICES,
    AgentBudget,
    AgentSettings,
    ModelPrice,
    load_agent_settings,
    price_for,
)
from carousearch.language_models import DEFAULT_PLANNER_MODEL, DEFAULT_WORKER_MODEL, ProviderKeys


def write_env_file(tmp_path: Path, text: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def load(environment: dict[str, str], tmp_path: Path) -> AgentSettings:
    return load_agent_settings(environment=environment, env_file=tmp_path / "missing.env")


def test_defaults_when_nothing_is_set(tmp_path: Path) -> None:
    settings = load({}, tmp_path)

    assert settings.planner_model == DEFAULT_PLANNER_MODEL == "openai:gpt-5.4-mini"
    assert settings.worker_model == DEFAULT_WORKER_MODEL == "openai:gpt-5.4-nano"
    assert settings.budget == AgentBudget(max_tool_calls=40, max_photos=15, max_listings=200, max_web_searches=3)
    assert settings.keys == ProviderKeys()


def test_settings_come_from_the_env_file_and_the_environment_wins(tmp_path: Path) -> None:
    env_file = write_env_file(
        tmp_path,
        "AGENT_PLANNER_MODEL=openai:gpt-5\nAGENT_WORKER_MODEL=openai:gpt-5-nano\nAGENT_MAX_TOOL_CALLS=12\n"
        "AGENT_MAX_PHOTOS=0\nAGENT_MAX_LISTINGS=96\nAGENT_MAX_WEB_SEARCHES=0\n"
        "OPENAI_API_KEY=sk-file\nANTHROPIC_API_KEY=sk-ant-file\n",
    )

    settings = load_agent_settings(environment={"OPENAI_API_KEY": "sk-env"}, env_file=env_file)

    assert (settings.planner_model, settings.worker_model) == ("openai:gpt-5", "openai:gpt-5-nano")
    assert settings.budget == AgentBudget(max_tool_calls=12, max_photos=0, max_listings=96, max_web_searches=0)
    assert settings.keys == ProviderKeys(anthropic="sk-ant-file", openai="sk-env")


def test_the_deprecated_agent_model_sets_both_tiers_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="carousearch.agent.settings"):
        settings = load({"AGENT_MODEL": "anthropic:claude-haiku-5-5"}, tmp_path)

    assert (settings.planner_model, settings.worker_model) == ("anthropic:claude-haiku-5-5",) * 2
    assert "AGENT_MODEL is deprecated" in caplog.text


def test_a_tier_set_itself_wins_over_the_deprecated_alias(tmp_path: Path) -> None:
    settings = load({"AGENT_MODEL": "openai:gpt-5.4-nano", "AGENT_PLANNER_MODEL": "openai:gpt-5.4-mini"}, tmp_path)

    assert (settings.planner_model, settings.worker_model) == ("openai:gpt-5.4-mini", "openai:gpt-5.4-nano")


def test_no_warning_when_both_tiers_are_set(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    environment = {"AGENT_MODEL": "x:y", "AGENT_PLANNER_MODEL": "openai:a", "AGENT_WORKER_MODEL": "openai:b"}
    with caplog.at_level(logging.WARNING, logger="carousearch.agent.settings"):
        settings = load(environment, tmp_path)

    assert (settings.planner_model, settings.worker_model) == ("openai:a", "openai:b")
    assert caplog.text == ""


def test_keys_are_never_printed() -> None:
    settings = AgentSettings(keys=ProviderKeys(anthropic="sk-ant-secret", openai="sk-secret"))

    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("AGENT_MAX_TOOL_CALLS", "0"),
        ("AGENT_MAX_TOOL_CALLS", "many"),
        ("AGENT_MAX_PHOTOS", "-1"),
        ("AGENT_MAX_LISTINGS", "2.5"),
        ("AGENT_MAX_WEB_SEARCHES", "-1"),
    ],
)
def test_a_malformed_budget_is_rejected(tmp_path: Path, variable: str, value: str) -> None:
    with pytest.raises(ValueError, match=variable):
        load({variable: value}, tmp_path)


@pytest.mark.parametrize("variable", ["AGENT_MODEL", "AGENT_PLANNER_MODEL", "AGENT_WORKER_MODEL"])
def test_a_model_without_its_provider_is_rejected(tmp_path: Path, variable: str) -> None:
    with pytest.raises(ValueError, match="provider:model"):
        load({variable: "gpt-5.4-mini"}, tmp_path)


@pytest.mark.parametrize(
    ("model", "expected_variable"),
    [
        ("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"),
        ("openai:gpt-5", "OPENAI_API_KEY"),
        ("openai-chat:gpt-5", "OPENAI_API_KEY"),
        ("openai-responses:gpt-5", "OPENAI_API_KEY"),
    ],
)
def test_the_missing_key_is_the_one_the_models_provider_needs(model: str, expected_variable: str) -> None:
    settings = AgentSettings(planner_model=model, worker_model=model)

    assert settings.missing_key_variable() == expected_variable
    assert settings.missing_key_model() == model


def test_the_worker_needs_its_own_providers_key() -> None:
    settings = AgentSettings(
        planner_model="openai:gpt-5.4-mini",
        worker_model="anthropic:claude-haiku-5-5",
        keys=ProviderKeys(openai="sk-openai"),
    )

    assert settings.missing_key_variable() == "ANTHROPIC_API_KEY"
    assert settings.missing_key_model() == "anthropic:claude-haiku-5-5"


def test_a_set_key_is_used_for_its_own_provider_only() -> None:
    keys = ProviderKeys(anthropic="sk-ant")

    assert keys.for_model("openai:gpt-5") is None
    assert AgentSettings(keys=keys).missing_key_variable() == "OPENAI_API_KEY"
    anthropic_only = AgentSettings(planner_model="anthropic:claude-haiku-5-5", worker_model="anthropic:x", keys=keys)
    assert anthropic_only.missing_key_variable() is None
    assert anthropic_only.missing_key_model() is None


def test_a_provider_outside_the_table_is_not_checked() -> None:
    settings = AgentSettings(planner_model="google:gemini-3-pro", worker_model="google:gemini-3-flash")

    assert settings.keys.for_model("google:gemini-3-pro") is None
    assert settings.missing_key_variable() is None


def test_prices_are_looked_up_by_model_name_whatever_the_provider_prefix() -> None:
    assert price_for("openai:gpt-5.4-mini") == ModelPrice(0.75, 0.075, 4.50)
    assert price_for("openai-responses:gpt-5.4-nano") == MODEL_PRICES["gpt-5.4-nano"]
    assert price_for("gpt-5.4-nano") == ModelPrice(0.20, 0.02, 1.25)
    assert price_for("anthropic:claude-haiku-5-5") == ModelPrice(0.10, 0.01, 0.50)
    assert price_for("function:scripted") is None
