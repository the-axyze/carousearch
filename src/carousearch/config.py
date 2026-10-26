"""Default file locations, the Telegram bot settings and the screening settings.

Set CAROUSEARCH_DATA_DIR to move all runtime data elsewhere. The bot reads TELEGRAM_BOT_TOKEN and
TELEGRAM_OWNER_ID, and screening reads the optional ANTHROPIC_API_KEY, from the environment or from a
`.env` file at the project root (environment wins).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DATA_DIR_ENVIRONMENT_VARIABLE = "CAROUSEARCH_DATA_DIR"
DATABASE_FILE_NAME = "carousearch.db"
CANARY_LOG_FILE_NAME = "canary.log"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
TELEGRAM_BOT_TOKEN_VARIABLE = "TELEGRAM_BOT_TOKEN"
TELEGRAM_OWNER_ID_VARIABLE = "TELEGRAM_OWNER_ID"
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
# The model that reads a multi-item listing's description for the searched variant's price.
DEFAULT_PRICE_MODEL = "claude-haiku-5-5"
# `carousearch mcp --transport http` listens here (the web app has 8765).
DEFAULT_MCP_HTTP_PORT = 8766


def default_data_dir() -> Path:
    """~/carousearch/data unless CAROUSEARCH_DATA_DIR says otherwise."""
    configured = os.environ.get(DATA_DIR_ENVIRONMENT_VARIABLE)
    return Path(configured).expanduser() if configured else Path.home() / "carousearch" / "data"


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str | None
    owner_user_id: int | None  # the only Telegram user allowed to use the bot

    def missing_variables(self) -> list[str]:
        missing = []
        if not self.bot_token:
            missing.append(TELEGRAM_BOT_TOKEN_VARIABLE)
        if self.owner_user_id is None:
            missing.append(TELEGRAM_OWNER_ID_VARIABLE)
        return missing


def load_telegram_settings(
    environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE
) -> TelegramSettings:
    """Read the bot settings. Unset values are None; a non-numeric owner id raises ValueError."""
    values = read_setting_values(environment, env_file)
    bot_token = values.get(TELEGRAM_BOT_TOKEN_VARIABLE, "").strip() or None
    return TelegramSettings(bot_token=bot_token, owner_user_id=parse_owner_user_id(values.get(TELEGRAM_OWNER_ID_VARIABLE, "")))


def parse_owner_user_id(text: str) -> int | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if not cleaned.isdigit():
        raise ValueError(f"{TELEGRAM_OWNER_ID_VARIABLE} must be your numeric Telegram user id, got {cleaned!r}")
    return int(cleaned)


@dataclass(frozen=True)
class ScreeningSettings:
    anthropic_api_key: str | None = field(default=None, repr=False)  # optional; never printed
    price_model: str = DEFAULT_PRICE_MODEL


def load_screening_settings(
    environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE
) -> ScreeningSettings:
    """Read the screening settings. Without ANTHROPIC_API_KEY, screening keeps listed prices."""
    values = read_setting_values(environment, env_file)
    return ScreeningSettings(anthropic_api_key=values.get(ANTHROPIC_API_KEY_VARIABLE, "").strip() or None)


def read_setting_values(environment: Mapping[str, str] | None, env_file: Path) -> dict[str, str]:
    """The `.env` file's values overlaid with the environment (the real environment when None)."""
    file_values = {name: value for name, value in dotenv_values(env_file).items() if value is not None}
    return {**file_values, **(os.environ if environment is None else environment)}
