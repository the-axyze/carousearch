from pathlib import Path

import pytest

from carousearch.config import (
    PROJECT_ROOT,
    TelegramSettings,
    WebAccessSettings,
    load_outreach_settings,
    load_telegram_settings,
    load_web_access_settings,
)


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


def test_web_access_is_open_and_not_a_demo_by_default(tmp_path: Path) -> None:
    settings = load_web_access_settings(environment={}, env_file=tmp_path / "missing.env")

    assert settings == WebAccessSettings(password=None, username="owner", public_demo=False)


def test_web_access_reads_the_password_username_and_demo_switch(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_PASSWORD=from-file\nWEB_USERNAME=friend\n")

    settings = load_web_access_settings(environment={"WEB_PUBLIC_DEMO": "1"}, env_file=env_file)

    assert settings == WebAccessSettings(password="from-file", username="friend", public_demo=True)


@pytest.mark.parametrize(("text", "expected"), [("1", True), ("true", True), ("Yes", True), ("0", False), ("", False)])
def test_the_public_demo_switch_accepts_common_true_words(tmp_path: Path, text: str, expected: bool) -> None:
    settings = load_web_access_settings(environment={"WEB_PUBLIC_DEMO": text}, env_file=tmp_path / "missing.env")

    assert settings.public_demo is expected


def test_an_empty_password_means_no_gate_and_the_password_never_shows_in_repr(tmp_path: Path) -> None:
    empty = load_web_access_settings(environment={"WEB_PASSWORD": ""}, env_file=tmp_path / "missing.env")
    set_one = load_web_access_settings(environment={"WEB_PASSWORD": "s3cret"}, env_file=tmp_path / "missing.env")

    assert empty.password is None
    assert "s3cret" not in repr(set_one)
