from pathlib import Path

import pytest

from carousearch.config import TelegramSettings, load_telegram_settings


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
