"""
UL OS Telegram Bot - main entry point.

Long-polling Telegram bot dla ekosystemu HiveLive.
Nasluchuje na komendy od admina (whitelist), forwarduje pliki do Workera UL OS,
zapisuje voice memo, generuje raporty.

Architektura: docelowo Docker container w Coolify (per ADR-006 z UL_OS_infrastructure_v1.md).
Tymczasowo - long polling, bez webhook (do testow lokalnie zanim domena bot.osadathehive.pl bedzie skonfigurowana).

Stack:
- python-telegram-bot v21+ (asyncio)
- httpx (calls do Workera/Directusa)
- pydantic-settings (env config)
"""

from __future__ import annotations

import logging
import sys

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import settings
from .handlers import (
    handle_document,
    handle_help,
    handle_health,
    handle_mcp_status,
    handle_mcp_szukaj,
    handle_ostatnie,
    handle_photo,
    handle_produkt,
    handle_start,
    handle_szukaj,
    handle_ulos_status,
    handle_unauthorized,
    handle_voice,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def is_authorized(update: Update) -> bool:
    """Whitelist check: only ADMIN_CHAT_IDS can use the bot."""
    if not update.effective_user:
        return False
    return update.effective_user.id in settings.admin_chat_ids


async def authorized_or_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper - if user not in whitelist, send polite NO and log."""
    if not is_authorized(update):
        await handle_unauthorized(update, context)
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_start(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_help(update, context)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_health(update, context)


async def cmd_szukaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_szukaj(update, context)


async def cmd_produkt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_produkt(update, context)


async def cmd_ostatnie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_ostatnie(update, context)


async def cmd_ulos_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_ulos_status(update, context)


async def cmd_mcp_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_mcp_status(update, context)


async def cmd_mcp_szukaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_mcp_szukaj(update, context)


async def msg_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_document(update, context)


async def msg_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_photo(update, context)


async def msg_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_voice(update, context)


async def post_init(app: Application):
    """Setup commands list (visible in bot menu)."""
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Powitanie i sprawdzenie dostępu"),
            BotCommand("help", "Lista komend"),
            BotCommand("health", "Status systemu UL OS"),
            BotCommand("szukaj", "Wyszukaj w bazie wiedzy"),
            BotCommand("produkt", "Info o produkcie BEEzzy"),
            BotCommand("ostatnie", "Ostatnio dodane dokumenty"),
            BotCommand("ulos_status", "Statystyki UL OS"),
            BotCommand("mcp_status", "Status MCP server (mcp.bidbee.pl)"),
            BotCommand("mcp_szukaj", "Search w Vault przez MCP (WIP)"),
        ]
    )
    log.info(
        "Bot started: @%s (id=%s), whitelist=%s",
        (await app.bot.get_me()).username,
        (await app.bot.get_me()).id,
        list(settings.admin_chat_ids),
    )


def build_app() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("szukaj", cmd_szukaj))
    app.add_handler(CommandHandler("produkt", cmd_produkt))
    app.add_handler(CommandHandler("ostatnie", cmd_ostatnie))
    app.add_handler(CommandHandler("ulos_status", cmd_ulos_status))
    app.add_handler(CommandHandler("mcp_status", cmd_mcp_status))
    app.add_handler(CommandHandler("mcp_szukaj", cmd_mcp_szukaj))

    app.add_handler(MessageHandler(filters.Document.ALL, msg_document))
    app.add_handler(MessageHandler(filters.PHOTO, msg_photo))
    app.add_handler(MessageHandler(filters.VOICE, msg_voice))

    return app


def main():
    app = build_app()
    if settings.use_webhook:
        log.info("Starting in WEBHOOK mode on %s", settings.webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=settings.webhook_port,
            url_path=settings.webhook_path,
            webhook_url=settings.webhook_url,
            secret_token=settings.webhook_secret,
        )
    else:
        log.info("Starting in LONG-POLLING mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
