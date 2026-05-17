"""
Voice mode state — per-chat flag dla TTS replies.

Minimal in-memory state (resetuje sie po restart bota — to OK dla MVP,
trwale storage moze byc dodane w v2 jesli okaze sie potrzebne).

Komendy:
    /voice_on        — wlacz TTS dla tej rozmowy
    /voice_off       — wylacz
    /voice_status    — sprawdz stan + dostepnosc klucza ELEVENLABS_API_KEY

API:
    is_voice_on(chat_id) -> bool
    set_voice(chat_id, on: bool) -> None
    send_with_tts(update, text) -> wysyla text i jesli voice_on, dodaje audio
"""

from __future__ import annotations

import logging

from telegram import Update

from .config import settings

log = logging.getLogger(__name__)

# In-memory state: chat_id -> voice_on bool
_VOICE_STATE: dict[int, bool] = {}


def is_voice_on(chat_id: int) -> bool:
    """Sprawdz czy voice mode aktywny dla danego chat_id."""
    return _VOICE_STATE.get(chat_id, False)


def set_voice(chat_id: int, on: bool) -> None:
    """Ustaw voice mode dla danego chat_id."""
    _VOICE_STATE[chat_id] = on
    log.info("voice_mode: chat_id=%s set to %s", chat_id, "on" if on else "off")


def all_active_chats() -> list[int]:
    """Lista chat_ids z aktywnym voice mode (dla debug)."""
    return [c for c, v in _VOICE_STATE.items() if v]


async def maybe_send_tts(update: Update, text: str) -> bool:
    """
    Jesli voice_on dla tego chat_id, syntetyzuj text przez ElevenLabs i wyslij jako voice.
    Non-blocking: bledy logowane, tekstowa odpowiedz juz wyslana osobno przez caller.

    Returns:
        True jesli TTS wyslane (lub probowane), False jesli voice_off / brak update.message.
    """
    chat = update.effective_chat
    if not chat or not update.message:
        return False
    if not is_voice_on(chat.id):
        return False

    # Lazy import zeby nie ladowac httpx jesli TTS nieaktywne
    from .services.tts import synthesize

    try:
        audio = await synthesize(text)
        if audio is None:
            log.debug("maybe_send_tts: synthesize returned None (no key or HTTP fail)")
            return False

        await update.message.reply_voice(voice=audio)
        log.info("voice_mode: TTS sent to chat_id=%s, %d bytes", chat.id, len(audio))
        return True
    except Exception:
        log.exception("voice_mode: send_voice failed")
        return False
