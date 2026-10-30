from pathlib import Path

import pytest

from carousearch.config import PROJECT_ROOT, TelegramSettings, load_outreach_settings, load_telegram_settings


def test_settings_come_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_OWNER_ID= 42 \n")

    settings = load_telegram_settings(environment={}, env_file=env_file)

    assert settings == TelegramSettings(bot_token="123:abc", owner_user_id=42)
    assert settings.missing_variables() == []


def test_environment_overrides_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\nTELEGRAM_OWNER_ID=1\n")

    settings = load_telegram_settings(environment={"TELEGRAM_OWNER_ID": "7"}, env_file=env_file)

    assert settings == TelegramSettings(bot_token="from-file", owner_user_id=7)


def test_missing_file_and_environment_leave_settings_unset(tmp_path: Path) -> None:
    settings = load_telegram_settings(environment={}, env_file=tmp_path / "missing.env")

    assert settings == TelegramSettings(bot_token=None, owner_user_id=None)
    assert settings.missing_variables() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"]


def test_non_numeric_owner_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="numeric Telegram user id"):
        load_telegram_settings(environment={"TELEGRAM_OWNER_ID": "@moses"}, env_file=tmp_path / "missing.env")


def test_outreach_settings_default_to_ten_a_day_thirty_to_sixty_seconds_and_the_worker_model(tmp_path: Path) -> None:
    settings = load_outreach_settings(environment={"AGENT_WORKER_MODEL": "openai:small"}, env_file=tmp_path / "x")

    assert (settings.daily_cap, settings.min_gap_seconds, settings.max_gap_seconds) == (10, 30, 60)
    assert settings.draft_model == "openai:small" and not settings.headed
    assert settings.profile_dir_in(tmp_path) == tmp_path / "browser-profile"
    assert settings.missing_key_variable() == "OPENAI_API_KEY"


def test_outreach_settings_are_read_from_the_environment(tmp_path: Path) -> None:
    environment = {
        "OUTREACH_DAILY_CAP": "4", "OUTREACH_MIN_GAP_SECONDS": "45", "OUTREACH_MAX_GAP_SECONDS": "90.5",
        "OUTREACH_DRAFT_MODEL": "anthropic:claude-haiku-5-5", "OUTREACH_PROFILE_DIR": "data/other-profile",
        "DISPLAY": ":0",
    }

    settings = load_outreach_settings(environment=environment, env_file=tmp_path / "x")

    assert (settings.daily_cap, settings.min_gap_seconds, settings.max_gap_seconds) == (4, 45, 90.5)
    assert settings.draft_model == "anthropic:claude-haiku-5-5" and settings.headed
    assert settings.profile_dir == PROJECT_ROOT / "data" / "other-profile"


@pytest.mark.parametrize(
    "environment",
    [{"OUTREACH_DAILY_CAP": "ten"}, {"OUTREACH_DAILY_CAP": "2.5"}, {"OUTREACH_MIN_GAP_SECONDS": "-1"},
     {"OUTREACH_MIN_GAP_SECONDS": "90", "OUTREACH_MAX_GAP_SECONDS": "60"}, {"OUTREACH_DRAFT_MODEL": "no-provider"}],
)
def test_malformed_outreach_settings_are_refused(tmp_path: Path, environment: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_outreach_settings(environment=environment, env_file=tmp_path / "x")
