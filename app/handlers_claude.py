"""
Telegram handlers for /claude agent mode + lifecycle commands.

Commands:
  /claude <prompt>      Start or continue agent session
  /claude_new <prompt>  Force-start new session (drops active)
  /claude_status        Show active session summary
  /claude_history       List recent sessions (up to 10)
  /claude_pause         Pause active session (won't continue on next message)
  /claude_resume        Resume paused/awaiting session
  /claude_cost          Cumulative cost summary
  /yes                  Approve pending tool action
  /no [reason]          Deny pending tool action
  /edit <new>           Cancel pending tool, inject new directive
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from . import audit
from . import limiter as lim
from .config import settings
from .services import agent, agent_session

log = logging.getLogger(__name__)

# Rate limit: N starts per window per user (continuations/approvals don't count)
# Configurable via CLAUDE_RATE_LIMIT and CLAUDE_RATE_WINDOW_S env vars.
CLAUDE_RATE_LIMIT = settings.claude_rate_limit
CLAUDE_RATE_WINDOW_S = settings.claude_rate_window_s

_limiter = lim.RateLimiter()


# ─── Authorization helper (mirrors handlers.py pattern) ────────────────────────

def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id in settings.admin_user_ids


async def _deny(update: Update) -> None:
    user = update.effective_user
    if update.message:
        await update.message.reply_text(
            "Ten bot jest prywatny dla ekosystemu HiveLive.\n"
            f"Twoj user_id: {user.id if user else '?'}\n"
            "Skontaktuj sie z h.gorecki@bidbee.pl jesli potrzebujesz dostepu."
        )
    audit.write(
        user_id=user.id if user else None,
        username=user.username if user else None,
        action="claude_denied",
        result="denied",
    )


# ─── Telegram message edit helper (used as progress_cb) ────────────────────────

def _make_progress_cb(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Returns an async callback used by the agent engine to stream progress."""
    state = {"buf": []}

    async def cb(emoji: str, msg: str) -> None:
        line = f"{emoji} {msg}".strip()
        state["buf"].append(line)
        # Keep only the last 10 lines visible
        tail = state["buf"][-10:]
        text = "\n".join(tail)
        if len(text) > 3900:
            text = text[-3900:]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except BadRequest as e:
            # "Message is not modified" is benign; rest we log
            if "not modified" not in str(e).lower():
                log.debug("progress edit BadRequest: %s", e)
        except TelegramError as e:
            log.debug("progress edit TelegramError: %s", e)

    return cb


async def _send_final(update: Update, text: str) -> None:
    """Send final result (split if > Telegram limit). Jesli voice_on, dodaj TTS audio."""
    if not update.message:
        return
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, ch in enumerate(chunks):
        prefix = "" if len(chunks) == 1 else f"({i + 1}/{len(chunks)})\n"
        await update.message.reply_text(prefix + ch)

    # Voice mode (Sprint TTS-minimal): jesli on, syntetyzuj caly text przez ElevenLabs
    # i wyslij jako voice. Non-blocking — failure tylko log, tekstowa odpowiedz juz dotarla.
    try:
        from .voice_mode import maybe_send_tts
        await maybe_send_tts(update, text)
    except Exception:
        log.exception("_send_final: TTS dispatch failed (non-blocking)")


# ─── /claude main entry ────────────────────────────────────────────────────────

async def handle_claude(update: Update, context: ContextTypes.DEFAULT_TYPE, *, force_new: bool = False) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = user.id if user else 0

    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        usage = (
            "Użycie: /claude <zadanie dla agenta>\n\n"
            "Przykład: `/claude sprawdź ostatni commit w THE-HIVE i wypisz 3 największe luki w /prototypy`\n\n"
            "Komendy pomocnicze:\n"
            " • /claude_status, /claude_history, /claude_pause, /claude_resume\n"
            " • /claude_new <prompt> — wymuszenie nowej sesji\n"
            " • /claude_cost — kumulatywny koszt aktywnej sesji\n"
            " • /yes /no /edit — odpowiedzi na zapytania agenta"
        )
        await update.message.reply_text(usage)
        return

    if not settings.anthropic_api_key:
        await update.message.reply_text("⚠️ ANTHROPIC_API_KEY nie skonfigurowany.")
        return

    # Resolve session
    if force_new:
        session = agent_session.new_session(
            chat_id=chat_id, user_id=user_id,
            model=settings.anthropic_agent_model, title_seed=prompt,
        )
        continuation = False
    else:
        existing = agent_session.load_active(chat_id)
        if existing and existing.status == "awaiting_approval":
            await update.message.reply_text(
                f"⏳ Aktywna sesja czeka na Twoją decyzję:\n"
                f"`{existing.pending_approval.get('tool_name')}` — {existing.pending_approval.get('reason')}\n\n"
                f"Odpowiedz `/yes`, `/no [powód]` albo `/edit <nowa instrukcja>`."
            )
            return
        if existing and existing.status in ("active", "paused"):
            session = existing
            continuation = True
        else:
            session = agent_session.new_session(
                chat_id=chat_id, user_id=user_id,
                model=settings.anthropic_agent_model, title_seed=prompt,
            )
            continuation = False

    # Rate limit only counts NEW session starts
    if not continuation:
        if not _limiter.allow(user_id, "claude", limit=CLAUDE_RATE_LIMIT, window=CLAUDE_RATE_WINDOW_S):
            await update.message.reply_text(
                f"🛑 Rate limit: max {CLAUDE_RATE_LIMIT} nowych sesji /claude na godzinę "
                "(chroni budget Anthropic). Spróbuj za chwilę albo kontynuuj istniejącą."
            )
            return

    audit.write(
        user_id=user_id,
        username=user.username if user else None,
        action="claude_new" if not continuation else "claude_continue",
        args=prompt[:200],
        extra={"session_id": session.id, "backend": agent_session.backend_label()},
    )

    progress = await update.message.reply_text(
        f"🧠 Sesja `{session.id[:8]}` ({'kontynuacja' if continuation else 'nowa'}) — agent myśli..."
    )
    session.last_progress_message_id = progress.message_id
    progress_cb = _make_progress_cb(context, chat_id, progress.message_id)

    # Optional auto-summarize before turn
    try:
        await agent.maybe_summarize(session)
    except Exception as e:  # noqa: BLE001
        log.debug("maybe_summarize failed: %s", e)

    try:
        result = await agent.run_turn(session=session, user_text=prompt, progress_cb=progress_cb)
    except Exception as e:  # noqa: BLE001
        log.exception("agent.run_turn crashed")
        session.status = "error"
        agent_session.save(session)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=progress.message_id,
            text=f"❌ Agent crash: {e}",
        )
        audit.write(
            user_id=user_id, username=user.username if user else None,
            action="claude_crash", result="error", error=str(e),
            extra={"session_id": session.id},
        )
        return

    agent_session.save(session)
    await _finalize_turn(update, context, chat_id, progress.message_id, session, result)


async def _finalize_turn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    progress_msg_id: int,
    session: agent_session.AgentSession,
    result: agent.AgentTurnResult,
) -> None:
    user = update.effective_user
    if result.status == "needs_approval" and result.pending_approval:
        pa = result.pending_approval
        text = (
            f"⏳ *Wymagana zgoda*\n\n"
            f"Tool: `{pa['tool_name']}`\n"
            f"Powód: {pa['reason']}\n\n"
            f"Odpowiedz `/yes`, `/no [powód]` albo `/edit <nowa instrukcja>`."
        )
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id, text=text, parse_mode="Markdown",
            )
        except BadRequest:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        audit.write(
            user_id=user.id if user else None, username=user.username if user else None,
            action="claude_awaiting_approval", args=pa["tool_name"],
            extra={"session_id": session.id, "reason": pa["reason"]},
        )
        return

    if result.status == "error":
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id,
                text=f"❌ Agent error: {result.error}",
            )
        except BadRequest:
            pass
        audit.write(
            user_id=user.id if user else None, username=user.username if user else None,
            action="claude_error", result="error", error=result.error or "?",
            extra={"session_id": session.id},
        )
        return

    # Completed
    cost_line = (
        f"\n\n💰 ${result.cost_usd:.4f} · {result.tokens_in}+{result.tokens_out} tok · "
        f"{len(result.tools_used)} tools"
    )
    final_text = (result.text or "(brak treści tekstowej)") + cost_line
    # Try to update the progress message with the final summary first
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=progress_msg_id,
            text=final_text[:3900],
        )
        if len(final_text) > 3900:
            await _send_final(update, final_text[3900:])
    except BadRequest:
        await _send_final(update, final_text)

    audit.write(
        user_id=user.id if user else None, username=user.username if user else None,
        action="claude_completed", args=f"iter={result.iterations}",
        extra={
            "session_id": session.id,
            "tools": result.tools_used[:20],
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
        },
    )


# ─── /claude_new ───────────────────────────────────────────────────────────────

async def handle_claude_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_claude(update, context, force_new=True)


# ─── /claude_status ────────────────────────────────────────────────────────────

async def handle_claude_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sess = agent_session.load_active(chat_id)
    if not sess:
        await update.message.reply_text(
            "Brak aktywnej sesji /claude.\nZacznij nową: /claude <prompt>"
        )
        return
    last_tool = sess.tool_calls[-1] if sess.tool_calls else None
    last_label = (
        f"{last_tool['name']} ({'ok' if last_tool.get('ok') else 'fail'})"
        if last_tool else "brak"
    )
    backend = agent_session.backend_label()
    pending = ""
    if sess.pending_approval:
        pending = f"\n⏳ Pending: `{sess.pending_approval['tool_name']}` — {sess.pending_approval['reason']}"
    msg = (
        f"📊 Sesja `{sess.id[:8]}`\n"
        f"Tytuł: {sess.title}\n"
        f"Status: *{sess.status}*\n"
        f"Tury historii: {len(sess.history)}\n"
        f"Tokeny: {sess.tokens_in}+{sess.tokens_out} ({sess.tokens_in + sess.tokens_out} łącznie)\n"
        f"Koszt: ${sess.cost_usd:.4f}\n"
        f"Tool calls: {len(sess.tool_calls)} (ostatni: {last_label})\n"
        f"Model: {sess.model}\n"
        f"Storage: {backend}{pending}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── /claude_history ───────────────────────────────────────────────────────────

async def handle_claude_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sessions = agent_session.list_chat_sessions(chat_id, limit=10)
    if not sessions:
        await update.message.reply_text("Brak zapisanych sesji /claude.")
        return
    lines = ["🗂️ Ostatnie sesje:\n"]
    for s in sessions:
        title = (s.get("title") or "(brak)")[:50]
        lines.append(
            f"`{(s.get('id') or '')[:8]}` · {s.get('status', '?')} · {title}\n"
            f"  {s.get('tokens_in', 0)}+{s.get('tokens_out', 0)} tok · ${s.get('cost_usd', 0):.4f} · {s.get('created_at', '')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /claude_pause ─────────────────────────────────────────────────────────────

async def handle_claude_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sess = agent_session.load_active(chat_id)
    if not sess:
        await update.message.reply_text("Brak aktywnej sesji.")
        return
    sess.status = "paused"
    agent_session.save(sess)
    await update.message.reply_text(f"⏸️ Sesja `{sess.id[:8]}` zapauzowana. /claude_resume aby wrócić.")


# ─── /claude_resume ────────────────────────────────────────────────────────────

async def handle_claude_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sess = agent_session.load_active(chat_id)
    if not sess:
        await update.message.reply_text("Brak sesji do wznowienia.")
        return
    if sess.status == "awaiting_approval":
        pa = sess.pending_approval or {}
        await update.message.reply_text(
            f"⏳ Sesja `{sess.id[:8]}` czeka na decyzję:\n"
            f"`{pa.get('tool_name', '?')}` — {pa.get('reason', '?')}\n"
            "Odpowiedz `/yes`, `/no` albo `/edit <...>`."
        )
        return
    sess.status = "active"
    agent_session.save(sess)
    await update.message.reply_text(
        f"▶️ Sesja `{sess.id[:8]}` wznowiona. Wyślij `/claude <kolejne zadanie>`."
    )


# ─── /claude_cost ──────────────────────────────────────────────────────────────

async def handle_claude_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sessions = agent_session.list_chat_sessions(chat_id, limit=50)
    if not sessions:
        await update.message.reply_text("Brak sesji.")
        return
    total_cost = sum(float(s.get("cost_usd", 0)) for s in sessions)
    total_in = sum(int(s.get("tokens_in", 0)) for s in sessions)
    total_out = sum(int(s.get("tokens_out", 0)) for s in sessions)
    msg = (
        f"💰 Koszt /claude (ostatnie {len(sessions)} sesji)\n"
        f"Total: *${total_cost:.4f}*\n"
        f"Tokeny: {total_in}+{total_out} = {total_in + total_out:,}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── /yes /no /edit (approval responses) ───────────────────────────────────────

async def _handle_approval_response(
    update: Update, context: ContextTypes.DEFAULT_TYPE, decision: str
) -> None:
    if not _is_authorized(update):
        await _deny(update)
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user = update.effective_user
    sess = agent_session.load_active(chat_id)
    if not sess or sess.status != "awaiting_approval":
        await update.message.reply_text(
            "Brak akcji oczekującej na decyzję. /claude_status pokaże stan."
        )
        return

    audit.write(
        user_id=user.id if user else None,
        username=user.username if user else None,
        action=f"claude_approval_{decision.split(':')[0]}",
        args=sess.pending_approval.get("tool_name", "?") if sess.pending_approval else "?",
        extra={"session_id": sess.id},
    )

    progress = await update.message.reply_text(
        f"▶️ Kontynuuję sesję `{sess.id[:8]}` (decyzja: {decision.split(':')[0]})..."
    )
    progress_cb = _make_progress_cb(context, chat_id, progress.message_id)

    try:
        result = await agent.continue_with_approval(sess, decision, progress_cb=progress_cb)
    except Exception as e:  # noqa: BLE001
        log.exception("continue_with_approval crashed")
        sess.status = "error"
        agent_session.save(sess)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=progress.message_id,
            text=f"❌ Agent crash: {e}",
        )
        return

    agent_session.save(sess)
    await _finalize_turn(update, context, chat_id, progress.message_id, sess, result)


async def handle_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_approval_response(update, context, "yes")


async def handle_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = " ".join(context.args).strip() if context.args else ""
    decision = f"no:{args}" if args else "no"
    await _handle_approval_response(update, context, decision)


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = " ".join(context.args).strip() if context.args else ""
    if not args:
        await update.message.reply_text(
            "Użycie: /edit <nowa instrukcja dla agenta>\n"
            "Anuluje oczekujący tool_use i wstrzykuje nową dyrektywę."
        )
        return
    await _handle_approval_response(update, context, f"edit:{args}")


# ─── Restart resume notifier (called from post_init) ───────────────────────────

async def notify_restart_resume(application) -> None:
    """On bot startup: ping each chat with active claude session."""
    sessions = agent_session.list_all_active()
    if not sessions:
        return
    log.info("Notifying %d chats about restart-resumed claude sessions", len(sessions))
    for sess in sessions:
        try:
            last_tool = sess.tool_calls[-1]["name"] if sess.tool_calls else "brak"
            await application.bot.send_message(
                chat_id=sess.chat_id,
                text=(
                    f"🔄 Bot zrestartowany. Sesja `{sess.id[:8]}` wznowiona.\n"
                    f"Status: {sess.status} · Ostatnia akcja: {last_tool}\n"
                    f"/claude_status — pełne info, /claude <prompt> — kontynuuj."
                ),
                parse_mode="Markdown",
            )
        except TelegramError as e:
            log.warning("restart resume notify failed chat=%s: %s", sess.chat_id, e)


# ─── Continuation: plain text + voice (without explicit /claude prefix) ────────
#
# When a user has an active /claude session and sends a message WITHOUT the
# /claude command prefix, we treat it as a continuation of that session. Voice
# messages are transcribed via Whisper first.
#
# These helpers RETURN True when they handled the message, so the existing
# msg_voice handler can fall through to its normal "Whisper → HOS upload" flow.

async def _run_continuation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    *,
    audit_action: str,
    intro_emoji: str,
    intro_label: str,
) -> None:
    """Shared engine for voice/text continuations — mirrors handle_claude body."""
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = user.id if user else 0
    sess = agent_session.load_active(chat_id)
    if sess is None or sess.status not in ("active", "paused"):
        return  # nothing to continue (should have been guarded by caller)

    if sess.status == "paused":
        sess.status = "active"  # implicit resume on continuation

    audit.write(
        user_id=user_id, username=user.username if user else None,
        action=audit_action, args=prompt[:200],
        extra={"session_id": sess.id, "backend": agent_session.backend_label()},
    )

    progress = await update.message.reply_text(
        f"{intro_emoji} {intro_label}\n🧠 Sesja `{sess.id[:8]}` — agent myśli..."
    )
    sess.last_progress_message_id = progress.message_id
    progress_cb = _make_progress_cb(context, chat_id, progress.message_id)

    try:
        await agent.maybe_summarize(sess)
    except Exception as e:  # noqa: BLE001
        log.debug("maybe_summarize failed: %s", e)

    try:
        result = await agent.run_turn(session=sess, user_text=prompt, progress_cb=progress_cb)
    except Exception as e:  # noqa: BLE001
        log.exception("agent.run_turn crashed (continuation)")
        sess.status = "error"
        agent_session.save(sess)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=progress.message_id,
            text=f"❌ Agent crash: {e}",
        )
        return

    agent_session.save(sess)
    await _finalize_turn(update, context, chat_id, progress.message_id, sess, result)


async def maybe_continue_via_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Called from msg_voice before the existing Whisper→HOS flow.

    If chat has active/paused /claude session:
      - transcribe voice via Whisper
      - feed transcript to agent as next user turn
      - return True (caller skips default voice handling)
    If chat has awaiting_approval:
      - hint user to use /yes /no /edit, return True (intercepted)
    Otherwise:
      - return False (caller does default voice → HOS classification)
    """
    if not _is_authorized(update):
        return False
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sess = agent_session.load_active(chat_id)
    if sess is None:
        return False

    if sess.status == "awaiting_approval":
        await update.message.reply_text(
            "📌 Sesja `/claude` czeka na decyzję. Odpowiedz `/yes`, `/no [powód]` lub "
            "`/edit <nowa instrukcja>` (voice nie zinterpretuję jako decyzję).",
            parse_mode="Markdown",
        )
        return True

    if sess.status not in ("active", "paused"):
        return False

    voice = update.message.voice
    if not voice:
        return False

    status_msg = await update.message.reply_text(
        f"🎙 Voice ({voice.duration or 0}s) → transkrypcja przez Whisper..."
    )

    # Lazy imports — exact same path as the existing handle_voice
    from .services.whisper_local import transcribe

    try:
        tg_file = await update.message.get_bot().get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        tr = await transcribe(audio_bytes, source_extension=".ogg")
    except Exception as e:  # noqa: BLE001
        log.exception("voice continuation: transcribe failed")
        await status_msg.edit_text(f"❌ Whisper fail: {e}")
        return True

    if not tr.success or not tr.text.strip():
        await status_msg.edit_text(
            f"⚠️ Whisper fail: {tr.error or 'pusta transkrypcja'}.\n"
            "Voice NIE zostało wysłane do HOS (przerwana sesja /claude continuation)."
        )
        return True

    transcript = tr.text.strip()
    preview = transcript[:160] + ("…" if len(transcript) > 160 else "")
    try:
        await status_msg.edit_text(f"🎙 Transkrypcja: „{preview}\"")
    except BadRequest:
        pass

    audit.write(
        user_id=update.effective_user.id if update.effective_user else None,
        username=update.effective_user.username if update.effective_user else None,
        action="claude_voice_continue",
        args=preview,
        extra={"session_id": sess.id, "duration_sec": voice.duration},
    )

    await _run_continuation(
        update, context, transcript,
        audit_action="claude_voice_continue_dispatch",
        intro_emoji="🎙",
        intro_label=f"Voice → kontynuacja: „{preview}\"",
    )
    return True


async def maybe_continue_via_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for plain text messages (no command prefix). Silently ignored unless
    chat has active/paused /claude session.
    """
    if not _is_authorized(update):
        return
    msg = update.message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id if update.effective_chat else 0
    sess = agent_session.load_active(chat_id)
    if sess is None:
        return  # no session — do nothing (user just chatting at bot)

    if sess.status == "awaiting_approval":
        await msg.reply_text(
            "📌 Sesja `/claude` czeka na decyzję. Użyj `/yes`, `/no [powód]` albo "
            "`/edit <nowa instrukcja>`.",
            parse_mode="Markdown",
        )
        return

    if sess.status not in ("active", "paused"):
        return

    await _run_continuation(
        update, context, msg.text.strip(),
        audit_action="claude_text_continue",
        intro_emoji="💬",
        intro_label="(kontynuacja sesji)",
    )
