from pathlib import Path

import pytest

from carousearch.agent.settings import (
    DEFAULT_AGENT_MODEL,
    AgentBudget,
    AgentSettings,
    load_agent_settings,
)


def write_env_file(tmp_path: Path, text: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return env_file


def test_defaults_when_nothing_is_set(tmp_path: Path) -> None:
    settings = load_agent_settings(environment={}, env_file=tmp_path / "missing.env")

    assert settings.model == DEFAULT_AGENT_MODEL == "anthropic:claude-haiku-5-5"
    assert settings.budget == AgentBudget(max_tool_calls=40, max_photos=15, max_listings=200)
    assert settings.anthropic_api_key is None and settings.openai_api_key is None


def test_settings_come_from_the_env_file_and_the_environment_wins(tmp_path: Path) -> None:
    env_file = write_env_file(
        tmp_path,
        "AGENT_MODEL=openai:gpt-5\nAGENT_MAX_TOOL_CALLS=12\nAGENT_MAX_PHOTOS=0\nAGENT_MAX_LISTINGS=96\n"
        "OPENAI_API_KEY=sk-file\nANTHROPIC_API_KEY=sk-ant-file\n",
    )

    settings = load_agent_settings(environment={"OPENAI_API_KEY": "sk-env"}, env_file=env_file)

    assert settings.model == "openai:gpt-5"
    assert settings.budget == AgentBudget(max_tool_calls=12, max_photos=0, max_listings=96)
    assert settings.openai_api_key == "sk-env"
    assert settings.anthropic_api_key == "sk-ant-file"


def test_keys_are_never_printed() -> None:
    settings = AgentSettings(anthropic_api_key="sk-ant-secret", openai_api_key="sk-secret")

    assert "secret" not in repr(settings)


@pytest.mark.parametrize(
    ("variable", "value"),
    [("AGENT_MAX_TOOL_CALLS", "0"), ("AGENT_MAX_TOOL_CALLS", "many"), ("AGENT_MAX_PHOTOS", "-1"), ("AGENT_MAX_LISTINGS", "2.5")],
)
def test_a_malformed_budget_is_rejected(tmp_path: Path, variable: str, value: str) -> None:
    with pytest.raises(ValueError, match=variable):
        load_agent_settings(environment={variable: value}, env_file=tmp_path / "missing.env")


def test_a_model_without_its_provider_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provider:model"):
        load_agent_settings(environment={"AGENT_MODEL": "claude-haiku-5-5"}, env_file=tmp_path / "missing.env")


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
    assert AgentSettings(model=model).missing_key_variable() == expected_variable


def test_a_set_key_is_used_for_its_own_provider_only() -> None:
    settings = AgentSettings(model="openai:gpt-5", anthropic_api_key="sk-ant")

    assert settings.api_key() is None
    assert settings.missing_key_variable() == "OPENAI_API_KEY"
    assert AgentSettings(model="anthropic:claude-haiku-5-5", anthropic_api_key="sk-ant").missing_key_variable() is None


def test_a_provider_outside_the_table_is_not_checked() -> None:
    settings = AgentSettings(model="google:gemini-3-pro")

    assert settings.api_key() is None
    assert settings.missing_key_variable() is None
