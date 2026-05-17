"""
Minimal ElevenLabs TTS service.

Uzycie:
    from app.services.tts import synthesize
    audio_bytes = await synthesize("Cześć Hubert!")
    if audio_bytes:
        # send as voice/audio via Telegram

Voice: Adam (pNInz6obpgDQGcFmaJgB) — eleven_multilingual_v2, polski meski.
Model: eleven_multilingual_v2
Format: mp3_44100_128
"""

from __future__ import annotations

import logging

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# ElevenLabs Adam — najlepszy ogolny meski glos PL w multilingual_v2
_VOICE_ID = "pNInz6obpgDQGcFmaJgB"
_MODEL_ID = "eleven_multilingual_v2"
_API_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{_VOICE_ID}"

# Max znakow do syntezy (ElevenLabs free: 10k/mies; przycinamy dlugie odp)
_MAX_CHARS = 900


async def synthesize(text: str) -> bytes | None:
    """
    Zamien tekst na audio MP3 (bytes) przez ElevenLabs.
    Zwraca None jesli brak klucza API lub blad HTTP.
    Przycinamy tekst do _MAX_CHARS zeby nie przepalac credits.
    """
    api_key = settings.elevenlabs_api_key
    if not api_key:
        log.debug("TTS: brak ELEVENLABS_API_KEY — pomijam synteze")
        return None

    # Ogranicz dlugosc + usun Markdown
    clean = _strip_markdown(text[:_MAX_CHARS])
    if not clean.strip():
        return None

    payload = {
        "text": clean,
        "model_id": _MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                _API_URL,
                json=payload,
                headers=headers,
                params={"output_format": "mp3_44100_128"},
            )
            if r.status_code == 200:
                log.info("TTS: synteza OK, %d bajtow MP3", len(r.content))
                return r.content
            else:
                log.warning("TTS: ElevenLabs HTTP %d: %s", r.status_code, r.text[:200])
                return None
    except Exception as e:  # noqa: BLE001
        log.warning("TTS: blad HTTP: %s", e)
        return None


def _strip_markdown(text: str) -> str:
    """Usun podstawowe formatowanie Markdown zeby TTS nie czytalo gwiazdek."""
    import re
    # bold/italic
    text = re.sub(r"[*_]{1,3}", "", text)
    # inline code
    text = re.sub(r"`{1,3}[^`]*`{1,3}", lambda m: m.group(0).strip("`"), text)
    # headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # links
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # bullet points
    text = re.sub(r"^[\-\*\+]\s+", "", text, flags=re.MULTILINE)
    # extra whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
