"""
Handlery TTS voice mode: /voice_on, /voice_off, /voice_status.

Voice mode aktywny per chat_id — gdy on, kazda odpowiedz claude'a (final result
przez handlers_claude._send_final) dostaje dodatkowo audio przez ElevenLabs TTS.

Voice mode NIE wplywa na inne komendy (np. /health, /szukaj) — tylko /claude.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import settings
from .voice_mode import is_voice_on, set_voice

log = logging.getLogger(__name__)


async def handle_voice_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wlacz TTS dla obecnej rozmowy."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id

    if not settings.elevenlabs_api_key:
        await update.message.reply_text(
            "⚠️ ELEVENLABS_API_KEY nie ustawiony — voice mode wlaczony, ale "
            "audio nie bedzie wysylane. Skontaktuj sie z administratorem."
        )
        set_voice(chat_id, True)
        return

    set_voice(chat_id, True)
    await update.message.reply_text(
        "🔊 Voice mode ON. Odpowiedzi /claude bede dostawal text + audio (TTS).\n"
        "Wylacz: /voice_off"
    )


async def handle_voice_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wylacz TTS dla obecnej rozmowy."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    set_voice(chat_id, False)
    await update.message.reply_text(
        "🔇 Voice mode OFF. Tylko tekstowe odpowiedzi.\n"
        "Wlacz: /voice_on"
    )


async def handle_voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pokaz stan voice mode + dostepnosc klucza."""
    if not update.effective_chat or not update.message:
        return

    chat_id = update.effective_chat.id
    on = is_voice_on(chat_id)
    has_key = bool(settings.elevenlabs_api_key)

    state_emoji = "🔊" if on else "🔇"
    state_text = "ON" if on else "OFF"
    key_text = "ustawiony" if has_key else "BRAK — admin musi ustawic ELEVENLABS_API_KEY"

    await update.message.reply_text(
        f"{state_emoji} Voice mode: **{state_text}**\n"
        f"ELEVENLABS_API_KEY: {key_text}\n\n"
        f"Komendy: /voice_on /voice_off",
        parse_mode="Markdown",
    )
