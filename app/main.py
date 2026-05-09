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

from datetime import time as dt_time

from . import audit, breaker, health_endpoint, monitor
from .config import settings
from .limiter import check as rate_check
from .handlers import (
    handle_audit,
    handle_digest,
    handle_dlq,
    handle_document,
    handle_help,
    handle_health,
    handle_koszty,
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
    return update.effective_user.id in settings.admin_user_ids


def _action_key(update: Update) -> str:
    """Dedukuj action key dla limiter/audit (np. 'produkt' dla /produkt)."""
    if update.message:
        if update.message.text and update.message.text.startswith("/"):
            return update.message.text[1:].split()[0].split("@")[0]
        if update.message.document:
            return "document"
        if update.message.photo:
            return "photo"
        if update.message.voice:
            return "voice"
    return "unknown"


def _action_args(update: Update) -> str | None:
    """Argumenty komendy do audit log."""
    if update.message and update.message.text and update.message.text.startswith("/"):
        parts = update.message.text.split(maxsplit=1)
        if len(parts) > 1:
            return parts[1][:200]  # Cap dlugości
    return None


async def authorized_or_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper - whitelist check + rate limiting + audit log.

    Returns True jezeli mozna kontynuowac, False jezeli zablokowane (auth/limit).
    """
    user = update.effective_user
    user_id = user.id if user else None
    username = user.username if user else None
    action = _action_key(update)
    args = _action_args(update)

    # 1. Auth check
    if not is_authorized(update):
        audit.write(
            user_id=user_id,
            username=username,
            action=action,
            args=args,
            result="denied",
            error="unauthorized",
        )
        await handle_unauthorized(update, context)
        return False

    # 2. Rate limit check (only for authorized users)
    if user_id is not None:
        allowed, err_msg = rate_check(user_id, action)
        if not allowed:
            audit.write(
                user_id=user_id,
                username=username,
                action=action,
                args=args,
                result="rate_limited",
                error=err_msg,
            )
            if update.message:
                await update.message.reply_text(f"⏱ {err_msg}")
            return False

    # 3. Audit success
    audit.write(
        user_id=user_id,
        username=username,
        action=action,
        args=args,
        result="ok",
    )
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


async def cmd_koszty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_koszty(update, context)


async def cmd_dlq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_dlq(update, context)


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_digest(update, context)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_audit(update, context)


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
            BotCommand("koszty", "Estymata kosztow API + ingest stats"),
            BotCommand("dlq", "Dead Letter Queue (failed ingests)"),
            BotCommand("digest", "Daily summary (manualne wyzwolenie)"),
            BotCommand("audit", "Ostatnie 20 akcji z audit log"),
        ]
    )
    log.info(
        "Bot started: @%s (id=%s), whitelist=%s",
        (await app.bot.get_me()).username,
        (await app.bot.get_me()).id,
        list(settings.admin_user_ids),
    )

    # Startup audit
    audit.write(
        user_id=None,
        username="system",
        action="bot_startup",
        result="ok",
        extra={"whitelist_size": len(settings.admin_user_ids)},
    )

    # Background health monitor: pierwsza po 60s (warm up), potem co 5 min
    if app.job_queue is not None:
        app.job_queue.run_repeating(monitor.tick, interval=300, first=60, name="health_monitor")
        log.info("Health monitor scheduled (interval=300s)")

        # Daily digest 09:00 UTC (~11:00 PL czas zimowy / 11:00 PL letni)
        from .handlers import handle_digest_auto
        app.job_queue.run_daily(
            handle_digest_auto,
            time=dt_time(hour=9, minute=0),
            name="daily_digest",
        )
        log.info("Daily digest scheduled (09:00 UTC daily)")

    # HTTP health endpoint na port 8080 (dla zewnetrznego uptime monitora)
    try:
        await health_endpoint.start_health_server(port=8080)
    except Exception as e:
        log.warning("Health endpoint nie wstal (port 8080 zajety?): %s", e)


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
    app.add_handler(CommandHandler("koszty", cmd_koszty))
    app.add_handler(CommandHandler("dlq", cmd_dlq))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("audit", cmd_audit))

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
