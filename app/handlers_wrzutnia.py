"""
Telegram-wrzutnia — uniwersalny kanał ingest (Faza 2 toru SYNC, MASTERPLAN v2.3).

Głosówka / plik audio / video-note / forward tekstu wysłane do bota lądują
w bazie ULOS przez Ingest API workera (VPS, zawsze włączony):
  • audio → POST /ingest/audio — transkrypcja lokalnym whisperem NA WORKERZE,
    oryginał do S3 originals/, rekord w knowledge_items; dalej samo:
    klasyfikator + embedding + voice-intent-sweep.
  • tekst → POST /ingest/file — od razu `unclassified` → klasyfikator.

Dlaczego nie whisper_local: binarka whisper-cli + model ggml-large z Aiko
istnieją tylko na jednym konkretnym Macu — w kontenerze Docker ta ścieżka
zawsze padała i voice szło do HOS surowe, bez transkrypcji. Worker ma
własny whisper.cpp + ffmpeg wbudowane w obraz (Sprint v3.1).

Dokumenty i zdjęcia NIE przechodzą tędy — mają sprawdzoną ścieżkę HOS inbox/
(handlers.handle_document / handle_photo → hos-poller ekstrahuje treść na VPS).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import settings
from .services import ingest_client

log = logging.getLogger(__name__)

# Bot API pozwala botom pobierać pliki do 20 MB (limit platformy, nie nasz).
TELEGRAM_BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024


async def _directus_preview(knowledge_id: str | None) -> str | None:
    """Best-effort podgląd transkrypcji (content_text) po ingest — czysta kosmetyka."""
    if not (knowledge_id and settings.directus_url and settings.directus_token):
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items/{knowledge_id}",
                params={"fields": "content_text"},
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
        if r.status_code != 200:
            return None
        text = ((r.json().get("data") or {}).get("content_text") or "").strip()
        return text[:300] if text else None
    except Exception:  # noqa: BLE001
        return None


async def _ingest_audio_flow(
    update: Update,
    *,
    file_id: str,
    file_size: int,
    filename: str,
    mime_type: str | None,
    duration: int,
    kind_label: str,
) -> None:
    """Wspólny przebieg: idempotencja → download z TG → /ingest/audio → odpowiedź."""
    from .idempotency import cache as idem_cache, telegram_file_key

    msg = update.message
    user = update.effective_user

    if not ingest_client.configured():
        # Brak konfiguracji ingest — ścieżka przetrwania: surowy plik do HOS inbox/
        # (stara droga; plik nie ginie, tylko bez transkrypcji od ręki).
        from .handlers import _forward_to_worker

        await _forward_to_worker(
            update,
            file_id=file_id,
            file_size=file_size,
            filename=filename,
            mime_type=mime_type,
            ack_prefix=f"{kind_label} (raw — ingest nie skonfigurowany):",
        )
        return

    key = telegram_file_key(file_id, file_size)
    if not idem_cache.check_and_mark(key):
        await msg.reply_text(f"♻️ {filename} już wysłany w ostatniej godzinie — skip dedup.")
        return

    if file_size and file_size > TELEGRAM_BOT_DOWNLOAD_LIMIT:
        await msg.reply_text(
            f"⚠️ {filename}: {file_size / 1024 / 1024:.1f} MB > 20 MB "
            "(limit pobierania Bot API — platforma, nie my).\n"
            "Dłuższe nagrania → folder Dyktafonu na Macu (Mac-most → whisper na Sparku)."
        )
        return

    dur = f" {duration}s," if duration else ""
    status_msg = await msg.reply_text(
        f"{kind_label}{dur} {file_size / 1024:.0f} KB\n"
        "⬆️ Ingest → worker (whisper na VPS)… przy dłuższych nagraniach to potrwa."
    )

    try:
        tg_file = await msg.get_bot().get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:  # noqa: BLE001
        log.exception("wrzutnia: download z Telegrama padł (%s)", filename)
        await status_msg.edit_text(f"❌ Pobieranie z Telegrama: {e}")
        return

    res = await ingest_client.ingest_audio(data, filename=filename, mime_type=mime_type)

    if not res.success:
        # Fallback przetrwania: surowe audio do HOS inbox/ — plik nie ginie.
        log.warning("wrzutnia: ingest_audio fail (%s): %s — fallback HOS raw", filename, res.error)
        try:
            await status_msg.edit_text(
                f"⚠️ Ingest padł: {res.error}\n⬆️ Fallback: surowy plik do HOS inbox/…"
            )
        except Exception:  # noqa: BLE001
            pass
        from .services.hos_uploader import upload_telegram_file

        up = await upload_telegram_file(
            data=data,
            filename=filename,
            mime_type=mime_type,
            telegram_user_id=user.id if user else 0,
            telegram_username=user.username if user else None,
        )
        if up.success:
            await status_msg.edit_text(
                f"⚠️ Ingest padł ({res.error}),\n"
                f"✅ ale surowy plik poszedł do HOS: {up.s3_key}"
            )
        else:
            await status_msg.edit_text(
                f"❌ Ingest padł ({res.error}) i HOS też ({up.error}). Plik NIE zapisany."
            )
        return

    if res.deduplicated:
        await status_msg.edit_text(
            f"♻️ {filename} — już był w bazie (dedup SHA-256).\n🗂 ID: {res.knowledge_id}"
        )
        return

    preview = await _directus_preview(res.knowledge_id) if res.transcribed else None
    lines = [f"✅ {filename} → baza ULOS"]
    if res.transcribed and preview:
        ellipsis = "…" if len(preview) >= 300 else ""
        lines.append(f"📝 „{preview}{ellipsis}”")
    elif res.transcribed:
        lines.append("📝 Transkrypcja zapisana (podgląd niedostępny).")
    else:
        lines.append("⏳ Transkrypcja nie wyszła od ręki — rekord czeka (pending_transcription_s5).")
    original = "S3 ✅" if res.original_saved else "S3 —"
    lines.append(f"🗂 ID: {res.knowledge_id} · oryginał: {original}")
    lines.append("Dalej samo: klasyfikator → embedding → voice-intent.")
    await status_msg.edit_text("\n".join(lines))


async def handle_voice_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice message (OGG opus) → /ingest/audio na workerze."""
    voice = update.message.voice
    if not voice:
        return
    duration = voice.duration or 0
    log.info("wrzutnia voice: duration=%ss size=%s", duration, voice.file_size)
    await _ingest_audio_flow(
        update,
        file_id=voice.file_id,
        file_size=voice.file_size or 0,
        filename=f"telegram_voice_{voice.file_unique_id}_{duration}s.ogg",
        mime_type=voice.mime_type or "audio/ogg",
        duration=duration,
        kind_label="🎙️ Voice",
    )


async def handle_audio_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plik audio (np. .m4a z Dyktafonu przesłany jako muzyka) lub video-note → /ingest/audio."""
    msg = update.message
    audio = msg.audio
    video_note = msg.video_note

    if audio is not None:
        duration = audio.duration or 0
        filename = audio.file_name or f"telegram_audio_{audio.file_unique_id}.m4a"
        log.info("wrzutnia audio: name=%s duration=%ss size=%s", filename, duration, audio.file_size)
        await _ingest_audio_flow(
            update,
            file_id=audio.file_id,
            file_size=audio.file_size or 0,
            filename=filename,
            mime_type=audio.mime_type or "audio/mpeg",
            duration=duration,
            kind_label="🎵 Audio",
        )
        return

    if video_note is not None:
        duration = video_note.duration or 0
        log.info("wrzutnia video_note: duration=%ss size=%s", duration, video_note.file_size)
        await _ingest_audio_flow(
            update,
            file_id=video_note.file_id,
            file_size=video_note.file_size or 0,
            filename=f"telegram_videonote_{video_note.file_unique_id}.mp4",
            mime_type="video/mp4",  # ffmpeg na workerze wyciągnie ścieżkę audio
            duration=duration,
            kind_label="📹 Video-note",
        )


async def handle_forwarded_text_ingest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Forward TEKSTU do bota → zapis do bazy ULOS.

    Gate’y (kolejność ważna):
      1) tylko wiadomości FORWARDOWANE — zwykłe pisanie do bota zostaje bez zmian
         (kontynuacja sesji /claude albo cisza, jak dotąd);
      2) jeśli czat ma JAKĄKOLWIEK aktywną sesję /claude, nic nie robimy —
         maybe_continue_via_text już obsłużył wiadomość (nie dublujemy).
    """
    msg = update.message
    if not msg or not msg.text:
        return

    forwarded = getattr(msg, "forward_origin", None) or getattr(msg, "forward_date", None)
    if not forwarded:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    try:
        from .services import agent_session

        if agent_session.load_active(chat_id) is not None:
            return
    except Exception:  # noqa: BLE001 — brak storage sesji nie może blokować wrzutni
        pass

    if not ingest_client.configured():
        await msg.reply_text(
            "⚠️ Ingest nie skonfigurowany (PIPELINE_HEALTH_URL/TOKEN lub INGEST_URL/TOKEN)."
        )
        return

    origin_label = ""
    fo = getattr(msg, "forward_origin", None)
    try:
        if fo is not None:
            sender = getattr(fo, "sender_user", None)
            chat = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
            hidden_name = getattr(fo, "sender_user_name", None)
            if sender is not None:
                origin_label = sender.full_name or (sender.username or "")
            elif chat is not None:
                origin_label = getattr(chat, "title", "") or getattr(chat, "username", "") or ""
            elif hidden_name:
                origin_label = hidden_name
    except Exception:  # noqa: BLE001
        origin_label = ""

    suffix = f" od {origin_label}" if origin_label else ""
    text = f"[Forward Telegram{suffix}]\n\n{msg.text.strip()}"

    res = await ingest_client.ingest_text(text, title_hint=(origin_label or "forward"))
    if res.success:
        tag = "♻️ już było w bazie" if res.deduplicated else "✅ zapisane do ULOS"
        await msg.reply_text(f"{tag} · 🗂 ID: {res.knowledge_id}")
    else:
        await msg.reply_text(f"❌ Ingest tekstu padł: {res.error}")
