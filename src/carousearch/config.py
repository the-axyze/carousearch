"""Default file locations, the Telegram bot settings and the screening settings.

Set CAROUSEARCH_DATA_DIR to move all runtime data elsewhere. The bot reads TELEGRAM_BOT_TOKEN and
TELEGRAM_OWNER_ID, and screening's price lookup reads the worker model (AGENT_WORKER_MODEL, see language_models.py)
and its provider's API key, from the environment or from a `.env` file at the project root (environment wins).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from carousearch.language_models import (
    DEFAULT_WORKER_MODEL,
    ProviderKeys,
    choose_models,
    load_provider_keys,
)

DATA_DIR_ENVIRONMENT_VARIABLE = "CAROUSEARCH_DATA_DIR"
DATABASE_FILE_NAME = "carousearch.db"
CANARY_LOG_FILE_NAME = "canary.log"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
TELEGRAM_BOT_TOKEN_VARIABLE = "TELEGRAM_BOT_TOKEN"
TELEGRAM_OWNER_ID_VARIABLE = "TELEGRAM_OWNER_ID"
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
    # The model that reads a multi-item listing's description for the searched variant's price: the worker model.
    price_model: str = DEFAULT_WORKER_MODEL
    keys: ProviderKeys = field(default_factory=ProviderKeys)

    def missing_key_variable(self) -> str | None:
        """The variable to set before the price lookup can run, or None when its provider's key is there."""
        return self.keys.missing_variable(self.price_model)


def load_screening_settings(
    environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE
) -> ScreeningSettings:
    """Read the screening settings. Without the worker model's API key, screening keeps listed prices."""
    values = read_setting_values(environment, env_file)
    return ScreeningSettings(price_model=choose_models(values).worker, keys=load_provider_keys(values))


def read_setting_values(environment: Mapping[str, str] | None, env_file: Path) -> dict[str, str]:
    """The `.env` file's values overlaid with the environment (the real environment when None)."""
    file_values = {name: value for name, value in dotenv_values(env_file).items() if value is not None}
    return {**file_values, **(os.environ if environment is None else environment)}
