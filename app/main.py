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
import os
from datetime import time as dt_time

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import audit, health_endpoint, monitor, observability
from .config import settings
from .handlers import (
    handle_alerts,
    handle_ask,
    handle_audit,
    handle_breakers,
    handle_digest,
    handle_dlq,
    handle_document,
    handle_generate,
    handle_health,
    handle_help,
    handle_koszty,
    handle_limits,
    handle_mcp_status,
    handle_mcp_szukaj,
    handle_mcp_tools,
    handle_ostatnie,
    handle_photo,
    handle_produkt,
    handle_research,
    handle_start,
    handle_status,
    handle_szukaj,
    handle_ulos_status,
    handle_unauthorized,
    handle_upload_stats,
    handle_voice,
)
from .handlers_claude import (
    handle_claude,
    handle_claude_cost,
    handle_claude_history,
    handle_claude_new,
    handle_claude_pause,
    handle_claude_resume,
    handle_claude_status,
    handle_edit,
    handle_no,
    handle_yes,
    notify_restart_resume,
)
from .handlers_voice import (
    handle_voice_off,
    handle_voice_on,
    handle_voice_status,
)
from .limiter import check as rate_check
from .handlers_komplet import cmd_komplet

# Setup logging (text format dla dev / JSON dla produkcji - LOG_FORMAT=json env)
observability.setup_logging(level=logging.INFO)

# Setup Sentry (NoOp jezeli SENTRY_DSN nieustawiony)
observability.setup_sentry(environment=os.getenv("SENTRY_ENVIRONMENT", "development"))

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


async def cmd_breakers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_breakers(update, context)


async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_limits(update, context)


async def cmd_mcp_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_mcp_tools(update, context)


# === Sprint 1.6 / 1.7 / 1.9 / 1.10 / 1.11 ===

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_status(update, context)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_alerts(update, context)


async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_generate(update, context)


async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_research(update, context)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_ask(update, context)


# ─── /claude agent mode ────────────────────────────────────────────────────────
async def cmd_claude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude(update, context)


async def cmd_claude_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_new(update, context)


async def cmd_claude_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_status(update, context)


async def cmd_claude_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_history(update, context)


async def cmd_claude_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_pause(update, context)


async def cmd_claude_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_resume(update, context)


async def cmd_claude_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_claude_cost(update, context)


async def cmd_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_yes(update, context)


async def cmd_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_no(update, context)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_edit(update, context)


async def cmd_upload_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_upload_stats(update, context)


async def cmd_voice_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_voice_on(update, context)


async def cmd_voice_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_voice_off(update, context)


async def cmd_voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized_or_ignore(update, context):
        return
    await handle_voice_status(update, context)


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
    # If chat has an active /claude session, intercept voice → transcribe → continue.
    # Returns True when handled — otherwise fall through to default Whisper → HOS upload.
    from .handlers_claude import maybe_continue_via_voice
    if await maybe_continue_via_voice(update, context):
        return
    await handle_voice(update, context)


async def msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain text (no command). Continues active /claude session if any; ignored otherwise."""
    if not await authorized_or_ignore(update, context):
        return
    from .handlers_claude import maybe_continue_via_text
    await maybe_continue_via_text(update, context)


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
            BotCommand("breakers", "Status circuit breakers (Directus/MCP/Anthropic)"),
            BotCommand("limits", "Twoje rate limits per komenda"),
            BotCommand("mcp_tools", "Lista 5 tools wystawionych przez MCP"),
            BotCommand("status", "Agregat statusu UL OS (bot/Directus/MCP/HOS/breakers)"),
            BotCommand("alerts", "Manualnie sprawdz alerty (DLQ/queue/review/deadlines)"),
            BotCommand("generate", "/generate <vault-path> → DOCX z Vault + papier firmowy"),
            BotCommand("research", "/research <prompt> → Perplexity Deep Research → Vault"),
            BotCommand("ask", "/ask <pytanie> → Claude z dostępem do Vault (multi-turn)"),
            BotCommand("upload_stats", "Statystyki upload per user (dziś/7d/30d)"),
            BotCommand("claude", "/claude <zadanie> → Agent z pełnym MCP (commit/deploy)"),
            BotCommand("claude_new", "/claude_new <prompt> → wymuś nową sesję"),
            BotCommand("claude_status", "Status aktywnej sesji /claude"),
            BotCommand("claude_history", "Ostatnie 10 sesji /claude"),
            BotCommand("claude_pause", "Zapauzuj aktywną sesję"),
            BotCommand("claude_resume", "Wznów zapauzowaną sesję"),
            BotCommand("claude_cost", "Kumulatywny koszt /claude"),
            BotCommand("yes", "Zaakceptuj oczekującą akcję agenta"),
            BotCommand("no", "/no [powód] → odrzuć akcję agenta"),
            BotCommand("edit", "/edit <nowa instrukcja> → anuluj akcję, zlec inne"),
            BotCommand("voice_on", "🔊 Włącz TTS (ElevenLabs) dla odpowiedzi /claude"),
            BotCommand("voice_off", "🔇 Wyłącz TTS"),
            BotCommand("voice_status", "Stan voice mode + dostępność klucza"),
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

    # Notify chats with active /claude sessions that bot restarted
    try:
        await notify_restart_resume(app)
    except Exception as e:  # noqa: BLE001
        log.warning("notify_restart_resume failed: %s", e)

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

        # Sprint 1.11 — Proactive notifications co N sek (default 4h = 14400s)
        from .services import notifier as _notifier
        _notif_interval = settings.notifier_interval_seconds
        app.job_queue.run_repeating(
            _notifier.tick,
            interval=_notif_interval,
            first=120,  # 2 min po starcie żeby pierwszy scan się wykonał
            name="proactive_notifier",
        )
        log.info(f"Proactive notifier scheduled (interval={_notif_interval}s = {_notif_interval/3600:.1f}h)")

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
    app.add_handler(CommandHandler("breakers", cmd_breakers))
    app.add_handler(CommandHandler("limits", cmd_limits))
    app.add_handler(CommandHandler("mcp_tools", cmd_mcp_tools))

    # Sprint 1.6 / 1.7 / 1.9 / 1.10 / 1.11
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("upload_stats", cmd_upload_stats))
    app.add_handler(CommandHandler("uploadstats", cmd_upload_stats))  # alias
    app.add_handler(CommandHandler("komplet", cmd_komplet))

    # /claude agent mode (Sprint: VPS-native agent)
    app.add_handler(CommandHandler("claude", cmd_claude))
    app.add_handler(CommandHandler("claude_new", cmd_claude_new))
    app.add_handler(CommandHandler("claude_status", cmd_claude_status))
    app.add_handler(CommandHandler("claude_history", cmd_claude_history))
    app.add_handler(CommandHandler("claude_pause", cmd_claude_pause))
    app.add_handler(CommandHandler("claude_resume", cmd_claude_resume))
    app.add_handler(CommandHandler("claude_cost", cmd_claude_cost))
    app.add_handler(CommandHandler("yes", cmd_yes))
    app.add_handler(CommandHandler("no", cmd_no))
    app.add_handler(CommandHandler("edit", cmd_edit))

    # TTS voice mode (Sprint TTS-minimal)
    app.add_handler(CommandHandler("voice_on", cmd_voice_on))
    app.add_handler(CommandHandler("voice_off", cmd_voice_off))
    app.add_handler(CommandHandler("voice_status", cmd_voice_status))

    app.add_handler(MessageHandler(filters.Document.ALL, msg_document))
    app.add_handler(MessageHandler(filters.PHOTO, msg_photo))
    app.add_handler(MessageHandler(filters.VOICE, msg_voice))
    # Plain text without command — continues active /claude session when present.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text))

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
