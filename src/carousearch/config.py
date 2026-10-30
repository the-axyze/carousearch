"""Default file locations, the Telegram bot settings, the screening settings and the outreach settings.

Set CAROUSEARCH_DATA_DIR to move all runtime data elsewhere. The bot reads TELEGRAM_BOT_TOKEN and
TELEGRAM_OWNER_ID, and screening's price lookup reads the worker model (AGENT_WORKER_MODEL, see language_models.py)
and its provider's API key, from the environment or from a `.env` file at the project root (environment wins).
Outreach reads the OUTREACH_* variables (docs/OUTREACH.md).
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
    parse_model_name,
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


OUTREACH_DAILY_CAP_VARIABLE = "OUTREACH_DAILY_CAP"
OUTREACH_MIN_GAP_SECONDS_VARIABLE = "OUTREACH_MIN_GAP_SECONDS"
OUTREACH_MAX_GAP_SECONDS_VARIABLE = "OUTREACH_MAX_GAP_SECONDS"
OUTREACH_DRAFT_MODEL_VARIABLE = "OUTREACH_DRAFT_MODEL"
OUTREACH_PROFILE_DIR_VARIABLE = "OUTREACH_PROFILE_DIR"
DEFAULT_OUTREACH_DAILY_CAP = 10  # new conversations in any 24 hours
DEFAULT_OUTREACH_MIN_GAP_SECONDS = 30
DEFAULT_OUTREACH_MAX_GAP_SECONDS = 60
DEFAULT_OUTREACH_PROFILE_DIR_NAME = "browser-profile"  # inside the data folder; gitignored with it


@dataclass(frozen=True)
class OutreachSettings:
    daily_cap: int = DEFAULT_OUTREACH_DAILY_CAP
    min_gap_seconds: float = DEFAULT_OUTREACH_MIN_GAP_SECONDS
    max_gap_seconds: float = DEFAULT_OUTREACH_MAX_GAP_SECONDS
    draft_model: str = DEFAULT_WORKER_MODEL  # OUTREACH_DRAFT_MODEL, else the worker model
    profile_dir: Path | None = None  # None: <data dir>/browser-profile
    headed: bool = True  # show the browser window when a display is available
    keys: ProviderKeys = field(default_factory=ProviderKeys)

    def profile_dir_in(self, data_dir: Path) -> Path:
        return self.profile_dir or data_dir / DEFAULT_OUTREACH_PROFILE_DIR_NAME

    def missing_key_variable(self) -> str | None:
        return self.keys.missing_variable(self.draft_model)


def load_outreach_settings(
    environment: Mapping[str, str] | None = None, env_file: Path = DEFAULT_ENV_FILE
) -> OutreachSettings:
    """Read the outreach settings; a malformed number raises ValueError."""
    values = read_setting_values(environment, env_file)
    min_gap = parse_number(values, OUTREACH_MIN_GAP_SECONDS_VARIABLE, DEFAULT_OUTREACH_MIN_GAP_SECONDS)
    max_gap = parse_number(values, OUTREACH_MAX_GAP_SECONDS_VARIABLE, DEFAULT_OUTREACH_MAX_GAP_SECONDS)
    if min_gap > max_gap:
        raise ValueError(f"{OUTREACH_MIN_GAP_SECONDS_VARIABLE} must not exceed {OUTREACH_MAX_GAP_SECONDS_VARIABLE}")
    profile_text = values.get(OUTREACH_PROFILE_DIR_VARIABLE, "").strip()
    return OutreachSettings(
        daily_cap=int(parse_number(values, OUTREACH_DAILY_CAP_VARIABLE, DEFAULT_OUTREACH_DAILY_CAP, whole=True)),
        min_gap_seconds=min_gap,
        max_gap_seconds=max_gap,
        draft_model=parse_model_name(values.get(OUTREACH_DRAFT_MODEL_VARIABLE, ""), choose_models(values).worker),
        profile_dir=resolve_project_path(profile_text) if profile_text else None,
        headed=bool(values.get("DISPLAY") or values.get("WAYLAND_DISPLAY")),
        keys=load_provider_keys(values),
    )


def parse_number(values: Mapping[str, str], variable: str, default: float, whole: bool = False) -> float:
    """A non-negative number from settings, or `default` when unset."""
    text = values.get(variable, "").strip()
    if not text:
        return default
    try:
        number = int(text) if whole else float(text)
    except ValueError:
        number = -1
    if number < 0:
        kind = "whole number" if whole else "number"
        raise ValueError(f"{variable} must be a {kind} of 0 or more, got {text!r}")
    return number


def resolve_project_path(text: str) -> Path:
    """A path from settings; a relative one is taken from the project root (e.g. data/browser-profile)."""
    path = Path(text).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
