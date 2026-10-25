"""Wiring: build the BotServices and the python-telegram-bot Application, then poll for updates."""

import logging
import sqlite3
from collections.abc import Sequence

from telegram import Bot, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from carousearch.bot import handlers
from carousearch.bot.handlers import SERVICES_KEY, BotServices, utc_now
from carousearch.bot.session_store import SessionStore
from carousearch.carousell.client import CarousellClient
from carousearch.config import TelegramSettings

logger = logging.getLogger(__name__)


def create_bot_services(
    carousell_client: CarousellClient, database: sqlite3.Connection, owner_user_id: int
) -> BotServices:
    return BotServices(
        carousell_client=carousell_client,
        database=database,
        session_store=SessionStore(clock=utc_now),
        owner_user_id=owner_user_id,
        clock=utc_now,
    )


def build_application(settings: TelegramSettings, services: BotServices) -> Application:
    if not settings.bot_token:
        raise ValueError("a Telegram bot token is required to build the application")
    application = ApplicationBuilder().token(settings.bot_token).build()
    application.bot_data[SERVICES_KEY] = services
    application.add_handler(CommandHandler("start", handlers.handle_start))
    application.add_handler(CommandHandler("help", handlers.handle_help))
    application.add_handler(CommandHandler("search", handlers.handle_search_command))
    application.add_handler(CommandHandler("end", handlers.handle_end_command))
    application.add_handler(CommandHandler("status", handlers.handle_status_command))
    application.add_handler(CommandHandler("save", handlers.handle_save_command))
    application.add_handler(CommandHandler("saved", handlers.handle_saved_command))
    application.add_handler(CallbackQueryHandler(handlers.handle_button))
    # New text messages only: editing an earlier message should not count as an answer or a new search.
    new_text_messages = filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND
    application.add_handler(MessageHandler(new_text_messages, handlers.handle_text_message))
    application.add_error_handler(handlers.log_unhandled_error)
    return application


def run_polling(settings: TelegramSettings, services: BotServices) -> None:
    """Run the bot until Ctrl+C."""
    configure_logging()
    application = build_application(settings, services)
    logger.info("Starting the bot; only Telegram user id %s may use it", services.owner_user_id)
    application.run_polling()


def configure_logging() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs every request URL at INFO, and Telegram API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --- first-run helper behind `carousearch whoami` ---------------------------------------


async def fetch_recent_senders(bot_token: str) -> list[str]:
    """Who has messaged the bot recently (Telegram keeps undelivered updates for 24 hours)."""
    async with Bot(bot_token) as bot:
        updates = await bot.get_updates(timeout=0)
    return describe_senders(updates)


def describe_senders(updates: Sequence[Update]) -> list[str]:
    lines = []
    seen_user_ids = set()
    for update in updates:
        user = update.effective_user
        if user is None or user.id in seen_user_ids:
            continue
        seen_user_ids.add(user.id)
        username = f" (@{user.username})" if user.username else ""
        lines.append(f"{user.id}  {user.full_name}{username}")
    return lines
