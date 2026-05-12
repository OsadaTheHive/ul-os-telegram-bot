"""
Handlery komend i wiadomosci. Trzymane osobno od main.py dla czytelnosci.
"""

from __future__ import annotations

import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from .config import settings

log = logging.getLogger(__name__)


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User spoza whitelisty - log i polite NO."""
    user = update.effective_user
    log.warning(
        "Unauthorized access: user_id=%s username=@%s name=%s",
        user.id if user else "?",
        user.username if user else "?",
        user.full_name if user else "?",
    )
    if update.message:
        await update.message.reply_text(
            "Ten bot jest prywatny dla ekosystemu HiveLive.\n"
            f"Twoj user_id: {user.id if user else '?'}\n"
            "Skontaktuj sie z h.gorecki@bidbee.pl jesli potrzebujesz dostepu."
        )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log.info("/start from user_id=%s @%s", user.id, user.username)
    await update.message.reply_text(
        f"Czesc {user.first_name}!\n\n"
        "UL OS Bot dziala. Dostep zweryfikowany.\n\n"
        "Co potrafie:\n"
        " - przyjac plik (PDF, DOCX, XLSX, PPTX, ZIP, foto) -> Worker UL OS klasyfikuje\n"
        " - przyjac voice memo -> Whisper transkrybuje (Q3 2026)\n"
        " - /szukaj <query> - semantic search po Vault (Q3 2026)\n"
        " - /produkt <nazwa> - dane produktu BEEzzy z bazy\n"
        " - /ostatnie - ostatnio dodane dokumenty\n"
        " - /health - status systemu (Worker, Directus, B2)\n"
        " - /ulos_status - koszty API, ile docs\n\n"
        "Wpisz /help dla pelnej listy."
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Komendy UL OS Bot:\n\n"
        "/start - powitanie\n"
        "/help - ta lista\n"
        "/health - status systemu\n"
        "/szukaj <query> - wyszukaj w bazie wiedzy\n"
        "/produkt <nazwa> - info o produkcie BEEzzy\n"
        "/ostatnie - ostatnio dodane dokumenty\n"
        "/ulos_status - statystyki kosztow i dokumentow\n\n"
        "Mozesz tez po prostu wyslac:\n"
        " - dokument (PDF/DOCX/...) -> trafi do INBOX, Worker przetworzy\n"
        " - foto -> klasyfikator + multimodal AI\n"
        " - voice memo -> transkrypcja (Q3 2026)\n"
    )


async def handle_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sprawdz Worker + Directus + MCP + B2 (jak juz beda)."""
    from . import breaker as bk
    msg = "Status UL OS:\n"

    # Circuit breakers state (jezeli ktorys OPEN - widac od razu)
    breakers_status = bk.all_stats()
    open_circuits = [n for n, s in breakers_status.items() if s["state"] == "open"]
    if open_circuits:
        msg += f" ⚠ Circuit OPEN: {', '.join(open_circuits)}\n"

    # Directus check
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.directus_url}/server/health")
            if r.status_code == 200:
                msg += " * Directus: OK\n"
            else:
                msg += f" * Directus: {r.status_code}\n"
    except Exception as e:
        msg += f" * Directus: ERROR ({e.__class__.__name__})\n"

    # MCP server check (NEW - 2026-05-09)
    if settings.mcp_base_url and settings.mcp_bearer_token:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{settings.mcp_base_url}/health",
                    headers={"Authorization": f"Bearer {settings.mcp_bearer_token}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    tools = data.get("tools_count", "?")
                    last_pull = data.get("vault_last_pulled", "?")
                    if isinstance(last_pull, str) and len(last_pull) >= 19:
                        last_pull = last_pull[:19].replace("T", " ")
                    msg += f" * MCP server: OK ({tools} tools, vault pull {last_pull})\n"
                else:
                    msg += f" * MCP server: {r.status_code}\n"
        except Exception as e:
            msg += f" * MCP server: ERROR ({e.__class__.__name__})\n"
    else:
        msg += " * MCP server: brak konfiguracji\n"

    # Worker check (jezeli adres skonfigurowany)
    if settings.worker_url and "localhost" not in settings.worker_url:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{settings.worker_url}/health")
                if r.status_code == 200:
                    msg += " * Worker: OK\n"
                else:
                    msg += f" * Worker: {r.status_code}\n"
        except Exception:
            msg += " * Worker: nieosiagalny\n"
    else:
        msg += " * Worker: nie skonfigurowany (Tier 0 milestone)\n"

    # Hetzner Object Storage check (per ADR-001, supersedes B2 plan)
    if settings.s3_access_key_id:
        msg += f" * Hetzner Object Storage: skonfigurowany (bucket: {settings.s3_bucket})\n"
    else:
        msg += " * Hetzner Object Storage: brak kluczy (Tier 0 milestone)\n"

    await update.message.reply_text(msg)


async def handle_mcp_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Szczegolowy status MCP server - vault info, tools, repo."""
    if not settings.mcp_bearer_token:
        await update.message.reply_text(
            "MCP nie skonfigurowany. Brak MCP_BEARER_TOKEN w env."
        )
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.mcp_base_url}/health",
                headers={"Authorization": f"Bearer {settings.mcp_bearer_token}"},
            )
            if r.status_code != 200:
                await update.message.reply_text(
                    f"MCP server zwrocil {r.status_code}: {r.text[:200]}"
                )
                return

            data = r.json()
            msg = f"UL OS MCP Server ({settings.mcp_base_url})\n\n"
            msg += f"Status: {data.get('status', '?')}\n"
            msg += f"Tools wystawione: {data.get('tools_count', '?')}\n"
            last_pull = data.get("vault_last_pulled", "")
            if last_pull:
                msg += f"Vault last pull: {last_pull[:19].replace('T', ' ')} UTC\n"
            msg += "\n"
            msg += "Vault repo: OsadaTheHive/HiveLive_Vault\n"
            msg += "Tenant: hivelive_ecosystem\n"
            msg += "\n"
            msg += "Aby uzyc tools (semantic search, vault query):\n"
            msg += "  /mcp_szukaj <query>"
            await update.message.reply_text(msg)
    except Exception as e:
        log.exception("mcp_status failed")
        await update.message.reply_text(f"MCP error: {e.__class__.__name__}: {e}")


async def handle_mcp_szukaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search w Vault przez MCP server vault_search tool (real client od 2026-05-09)."""
    from .services import mcp_client
    from . import breaker as bk

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "Uzycie: /mcp_szukaj <query>\n"
            "Przyklad: /mcp_szukaj BEEzzy strategia sprzedazy"
        )
        return

    client = mcp_client.get_client()
    if not client:
        await update.message.reply_text("MCP nie skonfigurowany (brak MCP_BEARER_TOKEN).")
        return

    try:
        # Wrap w circuit breaker - jezeli MCP pad N razy, automatic fail-fast
        result = await bk.get("mcp").call_async(
            client.call_tool, "vault_search", {"query": query, "limit": 10}
        )
        text = mcp_client.extract_text_content(result)
        if not text:
            await update.message.reply_text(f'Brak wynikow dla: "{query}"')
            return

        # Telegram message limit 4096 chars
        msg = f'🔍 vault_search: "{query}"\n\n{text}'
        if len(msg) > 3800:
            msg = msg[:3800] + "\n\n... (truncated, użyj /mcp_szukaj precyzyjniej)"
        await update.message.reply_text(msg)
    except mcp_client.MCPError as e:
        await update.message.reply_text(f"MCP error: {e}")
    except bk.CircuitBreakerError as e:
        await update.message.reply_text(f"⚡ MCP circuit breaker OPEN: {e}\nPoczekaj minutę.")
    except Exception as e:
        log.exception("mcp_szukaj failed")
        await update.message.reply_text(f"Blad: {e.__class__.__name__}: {e}")


async def handle_mcp_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista tools wystawianych przez MCP server."""
    from .services import mcp_client
    from . import breaker as bk

    client = mcp_client.get_client()
    if not client:
        await update.message.reply_text("MCP nie skonfigurowany.")
        return

    try:
        tools = await bk.get("mcp").call_async(client.list_tools)
        if not tools:
            await update.message.reply_text("Brak tools w MCP server.")
            return

        msg = f"🧰 MCP tools ({len(tools)}):\n\n"
        for t in tools:
            name = t.get("name", "?")
            desc = t.get("description", "")
            msg += f"• {name}\n"
            if desc:
                # First line of description
                first_line = desc.split("\n")[0][:120]
                msg += f"  {first_line}\n"
            msg += "\n"
        await update.message.reply_text(msg)
    except mcp_client.MCPError as e:
        await update.message.reply_text(f"MCP error: {e}")
    except bk.CircuitBreakerError as e:
        await update.message.reply_text(f"⚡ MCP circuit breaker: {e}")


async def handle_szukaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple full-text search w knowledge_items po title + content_text + summary."""
    from . import breaker as bk

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "Uzycie: /szukaj <query>\n"
            "Przyklad: /szukaj magazyn energii LFP 5kWh\n\n"
            "Tip: dla semantic search uzyj /mcp_szukaj <query>"
        )
        return

    if not settings.directus_token:
        await update.message.reply_text("Directus token brak.")
        return

    try:
        async def directus_search():
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{settings.directus_url}/items/knowledge_items",
                    params={
                        "filter[_or][0][title][_icontains]": query,
                        "filter[_or][1][content_text][_icontains]": query,
                        "filter[_or][2][summary][_icontains]": query,
                        "limit": 10,
                        "sort": "-date_created",
                        "fields": "id,title,brand,type,date_created,kontrahent",
                    },
                    headers={"Authorization": f"Bearer {settings.directus_token}"},
                )
                r.raise_for_status()
                return r.json()

        # Circuit breaker dla Directus
        data = await bk.get("directus").call_async(directus_search)
        items = data.get("data", [])

        if not items:
            await update.message.reply_text(f'Brak wynikow dla: "{query}"')
            return

        msg = f'🔍 Wyniki ({len(items)}) dla: "{query}"\n\n'
        for item in items:
            title = item.get("title", "?")
            brand = item.get("brand", "?")
            doc_type = item.get("type", "?")
            date = (item.get("date_created") or "?")[:10]
            kontrahent = item.get("kontrahent")

            msg += f"• [{brand}/{doc_type}] {title}\n"
            msg += f"  {date}"
            if kontrahent:
                msg += f" · {kontrahent}"
            msg += "\n\n"

        if len(msg) > 3800:
            msg = msg[:3800] + "\n... (truncated)"
        await update.message.reply_text(msg)

    except bk.CircuitBreakerError as e:
        await update.message.reply_text(f"⚡ Directus circuit breaker: {e}")
    except Exception as e:
        log.exception("szukaj failed")
        await update.message.reply_text(f"Blad: {e.__class__.__name__}: {e}")


async def handle_breakers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostyka - state circuit breakers."""
    from . import breaker as bk

    stats = bk.all_stats()
    msg = "⚡ Circuit breakers:\n\n"
    state_emoji = {"closed": "🟢", "half_open": "🟡", "open": "🔴"}

    for name, s in stats.items():
        emoji = state_emoji.get(s["state"], "?")
        msg += f"{emoji} {name}: {s['state']}\n"
        msg += f"   failures: {s['failures']}/{s['threshold']}\n"
        if s.get("seconds_until_half_open"):
            msg += f"   recovery in: {s['seconds_until_half_open']:.0f}s\n"
        msg += "\n"

    await update.message.reply_text(msg)


async def handle_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnostyka - rate limit status per user."""
    from .limiter import limiter, LIMITS

    user = update.effective_user
    if not user:
        await update.message.reply_text("Brak user context.")
        return

    msg = f"⏱ Rate limits dla @{user.username or user.first_name}:\n\n"
    for key, (limit, window) in LIMITS.items():
        remaining = limiter.remaining(user.id, key, limit=limit, window=window)
        msg += f"  /{key}: {remaining}/{limit} pozostalo (w {int(window)}s)\n"

    msg += f"\nTotal buckets: {limiter.stats()['total_buckets']}"
    await update.message.reply_text(msg)


async def handle_produkt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nazwa = " ".join(context.args) if context.args else ""
    if not nazwa:
        await update.message.reply_text(
            "Uzycie: /produkt <nazwa>\n"
            "Przyklad: /produkt PowerHill 261kWh"
        )
        return

    # Search w beezzy_products przez Directus
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{settings.directus_url}/items/beezzy_products",
                params={
                    "filter[_or][0][title][_icontains]": nazwa,
                    "filter[_or][1][model][_icontains]": nazwa,
                    "filter[is_duplicate][_neq]": "true",
                    "limit": 5,
                    "fields": "id,title,manufacturer,model,capacity_kwh,power_w,price_retail_pln,description_short",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"} if settings.directus_token else {},
            )
            if r.status_code != 200:
                await update.message.reply_text(f"Directus zwrocil {r.status_code}")
                return
            data = r.json().get("data", [])
            if not data:
                await update.message.reply_text(f'Nie znalazlem produktu: "{nazwa}"')
                return

            msg = f'Znalazlem {len(data)} produktow:\n\n'
            for p in data:
                msg += f"* {p.get('title')}\n"
                if p.get("manufacturer"):
                    msg += f"  Producent: {p['manufacturer']}"
                    if p.get("model"):
                        msg += f" / {p['model']}"
                    msg += "\n"
                if p.get("capacity_kwh"):
                    msg += f"  Pojemnosc: {p['capacity_kwh']} kWh\n"
                if p.get("power_w"):
                    msg += f"  Moc: {p['power_w']} W\n"
                if p.get("price_retail_pln"):
                    msg += f"  Cena: {p['price_retail_pln']} PLN\n"
                if p.get("description_short"):
                    msg += f"  {p['description_short'][:150]}\n"
                msg += "\n"
            await update.message.reply_text(msg)
    except Exception as e:
        log.exception("produkt query failed")
        await update.message.reply_text(f"Blad zapytania: {e}")


async def handle_ostatnie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ostatnio dodane knowledge_items."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "limit": 10,
                    "sort": "-date_created",
                    "fields": "title,brand,type,date_created",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"} if settings.directus_token else {},
            )
            data = r.json().get("data", [])
            if not data:
                await update.message.reply_text("Brak dokumentow w bazie.")
                return
            msg = "10 ostatnio dodanych dokumentow:\n\n"
            for d in data:
                msg += f"* [{d.get('brand', '?')}/{d.get('type', '?')}] {d.get('title', '?')}\n"
                if d.get("date_created"):
                    msg += f"  {d['date_created'][:10]}\n"
            await update.message.reply_text(msg)
    except Exception as e:
        log.exception("ostatnie failed")
        await update.message.reply_text(f"Blad: {e}")


async def handle_ulos_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liczba dokumentow per marka, koszty API (TODO)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "groupBy[]": "brand",
                    "aggregate[count]": "id",
                    "limit": -1,
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"} if settings.directus_token else {},
            )
            data = r.json().get("data", [])
            msg = "UL OS - statystyki:\n\n"
            total = 0
            for row in data:
                count = row.get("count", {}).get("id", 0) if isinstance(row.get("count"), dict) else row.get("count", 0)
                msg += f"* {row.get('brand', '?')}: {count}\n"
                total += count
            msg += f"\nLacznie: {total} dokumentow\n"
            msg += "\nKoszty API: WIP (Q2 - integracja z Anthropic usage)"
            await update.message.reply_text(msg)
    except Exception as e:
        log.exception("ulos_status failed")
        await update.message.reply_text(f"Blad: {e}")


async def _forward_to_worker(
    update: Update,
    *,
    file_id: str,
    file_size: int,
    filename: str,
    mime_type: str | None,
    ack_prefix: str,
) -> None:
    """
    Wspolne: pobiera plik z Telegrama, wgrywa do HOS inbox/, replikuje ack.
    Worker UL OS (mode=hos) wykryje plik w max 30s.
    """
    from .idempotency import cache as idem_cache, telegram_file_key
    from .services.hos_uploader import upload_telegram_file

    # Idempotency check
    key = telegram_file_key(file_id, file_size)
    if not idem_cache.check_and_mark(key):
        await update.message.reply_text(
            f"♻️ Plik {filename} juz wyslany w ostatniej godzinie - skip dedup."
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None

    status_msg = await update.message.reply_text(
        f"{ack_prefix} {filename}\n"
        f"Rozmiar: {file_size / 1024 / 1024:.2f} MB\n"
        f"⬆️  Wgrywam do HOS inbox/..."
    )

    try:
        tg_file = await update.message.get_bot().get_file(file_id)
        data = await tg_file.download_as_bytearray()
        result = await upload_telegram_file(
            data=bytes(data),
            filename=filename,
            mime_type=mime_type,
            telegram_user_id=user_id,
            telegram_username=username,
        )
        if result.success:
            await status_msg.edit_text(
                f"✅ {filename}\n"
                f"Wgrany do HOS inbox/\n"
                f"Klucz: {result.s3_key}\n"
                f"Rozmiar: {result.bytes_uploaded / 1024 / 1024:.2f} MB\n\n"
                f"⏱️ Worker UL OS przetworzy w ciagu max 30 sek "
                f"(klasyfikacja Anthropic → Directus + Vault + HOS attachment)."
            )
        else:
            await status_msg.edit_text(
                f"❌ {filename}\n"
                f"Blad uploadu do HOS: {result.error}\n\n"
                f"Sprawdz konfiguracje S3_* envs bota."
            )
    except Exception as e:
        log.exception("forward_to_worker fail filename=%s", filename)
        await status_msg.edit_text(f"❌ Blad pobierania/uploadu: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plik (PDF/DOCX/XLSX/CSV/ZIP) -> HOS inbox/ -> Worker UL OS w 30s."""
    doc = update.message.document
    log.info("document received: name=%s size=%s mime=%s", doc.file_name, doc.file_size, doc.mime_type)
    await _forward_to_worker(
        update,
        file_id=doc.file_id,
        file_size=doc.file_size or 0,
        filename=doc.file_name or f"document_{doc.file_id}.bin",
        mime_type=doc.mime_type,
        ack_prefix="📄 Dokument:",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zdjecie (najwiekszy rozmiar) -> HOS inbox/ -> Worker multimodal AI."""
    photo = update.message.photo[-1]  # najwiekszy rozmiar
    log.info("photo received: file_id=%s size=%s", photo.file_id, photo.file_size)
    await _forward_to_worker(
        update,
        file_id=photo.file_id,
        file_size=photo.file_size or 0,
        filename=f"telegram_photo_{photo.file_unique_id}.jpg",
        mime_type="image/jpeg",
        ack_prefix="🖼️  Foto:",
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Voice memo (OGG opus) -> Whisper local transkrypcja -> markdown -> HOS inbox/ -> Worker.

    Sprint 1.8: lokalna transkrypcja przez whisper.cpp (ggml-large.bin z Aiko).
    Zero kosztu OpenAI API. Worker dostaje gotowy markdown z transkrypcją do klasyfikacji.
    """
    from .idempotency import cache as idem_cache, telegram_file_key
    from .services.whisper_local import transcribe
    from .services.hos_uploader import upload_telegram_file

    voice = update.message.voice
    log.info("voice received: duration=%ss size=%s", voice.duration, voice.file_size)

    # Idempotency check
    key = telegram_file_key(voice.file_id, voice.file_size or 0)
    if not idem_cache.check_and_mark(key):
        await update.message.reply_text("♻️ Voice juz wyslany w ostatniej godzinie - skip dedup.")
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user else None
    duration = voice.duration or 0

    status_msg = await update.message.reply_text(
        f"🎙️  Voice ({duration}s, {(voice.file_size or 0)/1024:.0f} KB)\n"
        f"⬇️  Pobieram z Telegrama...\n"
        f"🔊 Transkrybuję przez Whisper (local ggml-large)..."
    )

    try:
        # 1. Download bytes
        tg_file = await update.message.get_bot().get_file(voice.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())

        # 2. Whisper local transcription
        await status_msg.edit_text(
            f"🎙️  Voice ({duration}s)\n"
            f"🔊 Whisper transkrybuje... (zwykle 2-15s)"
        )
        tr = await transcribe(audio_bytes, source_extension=".ogg")

        if not tr.success:
            # Fallback: upload tylko OGG (bez transkrypcji) — Worker zapisze jako attachment
            await status_msg.edit_text(
                f"⚠️  Whisper fail: {tr.error}\n"
                f"⬆️  Upload OGG bez transkrypcji..."
            )
            await _forward_to_worker(
                update,
                file_id=voice.file_id,
                file_size=voice.file_size or 0,
                filename=f"telegram_voice_{voice.file_unique_id}_{duration}s.ogg",
                mime_type=voice.mime_type or "audio/ogg",
                ack_prefix="🎙️  Voice (raw):",
            )
            return

        # 3. Build markdown z frontmatter
        from datetime import datetime, timezone as _tz
        date = datetime.now(_tz.utc).strftime("%Y-%m-%d")
        ts = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
        preview = tr.text[:200]

        markdown = (
            f"---\n"
            f"type: notatka_voice\n"
            f"source: telegram_voice\n"
            f"date: {date}\n"
            f"tg_user: {username or user_id}\n"
            f"duration_sec: {duration}\n"
            f"transcribed_by: whisper.cpp ({tr.model_used})\n"
            f"language: pl\n"
            f"---\n\n"
            f"# Voice memo — {ts}\n\n"
            f"**Nadawca:** {username or f'user_{user_id}'}\n"
            f"**Czas trwania:** {duration} sekund\n"
            f"**Model transkrypcji:** {tr.model_used}\n\n"
            f"## Transkrypcja\n\n"
            f"{tr.text}\n"
        )

        # 4. Upload markdown do HOS inbox/ (worker przeczyta i sklasyfikuje)
        await status_msg.edit_text(
            f"🎙️  Voice ({duration}s) → transkrypcja gotowa\n\n"
            f"📝 \"{preview}{'...' if len(tr.text) > 200 else ''}\"\n\n"
            f"⬆️  Upload markdown do HOS..."
        )

        upload = await upload_telegram_file(
            data=markdown.encode("utf-8"),
            filename=f"voice_{voice.file_unique_id}_{duration}s_transkrypcja.md",
            mime_type="text/markdown",
            telegram_user_id=user_id,
            telegram_username=username,
            extra_metadata={
                "ulos-source": "telegram_voice",
                "ulos-duration-sec": str(duration),
                "ulos-whisper-model": tr.model_used,
            },
        )

        if upload.success:
            await status_msg.edit_text(
                f"✅ Voice ({duration}s) → transkrypcja + upload OK\n\n"
                f"📝 \"{preview}{'...' if len(tr.text) > 200 else ''}\"\n\n"
                f"🗂️ HOS: {upload.s3_key}\n"
                f"⏱️ Worker przetworzy w ~30s (klasyfikator → Directus + Vault)\n"
                f"🎯 Whisper: {tr.model_used}, znaków: {len(tr.text)}"
            )
        else:
            await status_msg.edit_text(
                f"⚠️  Transkrypcja OK ale upload fail: {upload.error}\n\n"
                f"Transkrypcja (zachowaj recznie):\n{tr.text[:1500]}"
            )

    except Exception as e:
        log.exception("voice handler failed")
        await status_msg.edit_text(f"❌ Voice handler crash: {e}")


# ============================================================
# /koszty - estymata kosztow API z lokalnego audit + Directus
# ============================================================

async def handle_koszty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estymata kosztow API + statystyki uzycia."""
    from .services import usage_stats

    # Window: 24h default, mozna /koszty 7 → 7 dni
    window_hours = 24
    if context.args:
        try:
            days = int(context.args[0])
            window_hours = max(1, min(days * 24, 24 * 30))  # cap 30 dni
        except ValueError:
            pass

    local = usage_stats.stats_local(window_hours=window_hours)
    directus = await usage_stats.stats_directus(window_hours=window_hours)

    msg = f"Koszty UL OS (ostatnie {window_hours}h)\n\n"

    # Bot stats (lokalnie z audit.jsonl)
    msg += "📊 Bot Telegram\n"
    msg += f"  • Total events: {local['total_events']}\n"
    if local["file_ingests"] > 0:
        msg += f"  • File ingests: {local['file_ingests']} (estymata $${local['est_cost_usd']:.4f})\n"
    if local["rate_limited"] > 0:
        msg += f"  • ⚠ Rate-limited: {local['rate_limited']}\n"
    msg += f"  • Audit log: {local['audit_file_size_bytes'] / 1024:.1f} KB\n\n"

    # Directus stats (real classifier work)
    if "error" not in directus:
        msg += "🗂 Directus knowledge_items\n"
        msg += f"  • Total: {directus['total']}\n"
        msg += f"  • Recent ({window_hours}h): {directus['recent_count']}\n"
        for brand, cnt in sorted(directus["by_brand"].items(), key=lambda x: -x[1]):
            msg += f"    - {brand}: {cnt}\n"
    else:
        msg += f"⚠ Directus stats: {directus['error']}\n"

    msg += "\n💰 Anthropic API (real cost):\n"
    msg += "  Wymaga ANTHROPIC_ADMIN_KEY + org_id (Hubert).\n"
    msg += "  Tymczasowo estymata z bot ingest count powyzej.\n\n"

    msg += f"Pricing: Haiku 4.5 = $1/$5 per 1M tokens (in/out)\n"
    msg += f"Estymata per doc: $${usage_stats.EST_COST_PER_CLASSIFICATION:.4f}"

    await update.message.reply_text(msg)


# ============================================================
# /dlq - lista failed ingests (Dead Letter Queue)
# ============================================================

async def handle_dlq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lista / retry failed items w HOS DLQ (inbox-failed/).

    Sprint 1.5 + retry:
      /dlq                        — top 10 failed (default)
      /dlq 20                     — top 20 failed
      /dlq retry <key>            — przerzuć konkretny key z inbox-failed/ do inbox/
      /dlq retry all              — przerzuć wszystkie (max 50)
      /dlq retry 2026-05-11       — przerzuć tylko z konkretnej daty

    Worker (mode=hos) podniesie retry-pliki z prefixem `retry-<ts>-<filename>`
    i spróbuje przetworzyć ponownie. Idempotent — kasuje źródło po sukcesie copy.
    """
    from .services import dlq

    # Subcomenda /dlq retry ...
    if context.args and context.args[0].lower() == "retry":
        await _handle_dlq_retry(update, context, context.args[1:])
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    result = await dlq.list_dlq_items(limit=limit)

    status = result["status"]
    if status == "not_configured":
        await update.message.reply_text(f"⚠️ {result.get('message','DLQ not configured')}")
        return
    if status == "error":
        await update.message.reply_text(f"❌ {result.get('message','DLQ error')}")
        return
    if status == "empty":
        await update.message.reply_text(f"✅ {result.get('message','DLQ pusty')}")
        return

    items = result["items"]
    total = result["total"]
    lines = [
        f"🪦 Dead Letter Queue ({total} items, pokaz top {len(items)}):",
        "",
    ]

    for it in items:
        # it to dataclass DLQItem
        size_mb = it.size / 1024 / 1024
        ts = it.last_modified.strftime("%Y-%m-%d %H:%M") if it.last_modified else "?"
        err = (it.error_message or "(brak metadata x-worker-error)")[:120]
        lines.append(
            f"• [{ts}] {it.filename}\n"
            f"  rozm: {size_mb:.2f} MB · dir: inbox-failed/{it.date}/\n"
            f"  err: {err}\n"
        )

    msg = "\n".join(lines)
    # Telegram limit 4096 chars na message
    if len(msg) > 3900:
        msg = msg[:3900] + "\n…(obciete)"
    await update.message.reply_text(msg)


# ============================================================
# /digest - manualne wyzwolenie daily summary
# ============================================================

async def handle_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manualny daily digest - wszystko co sie dzialo."""
    from .services import usage_stats

    local = usage_stats.stats_local(window_hours=24)
    directus = await usage_stats.stats_directus(window_hours=24)
    mcp = await usage_stats.stats_mcp()

    msg = "📊 UL OS Daily Digest\n\n"

    # Ingest summary
    if "error" not in directus:
        msg += f"📥 Ostatnie 24h: {directus['recent_count']} nowych dokumentow\n"
        msg += f"📊 Total w bazie: {directus['total']}\n"
        for brand, cnt in sorted(directus["by_brand"].items(), key=lambda x: -x[1]):
            msg += f"  • {brand}: {cnt}\n"

    msg += f"\n🤖 Bot activity (24h):\n"
    msg += f"  • Total events: {local['total_events']}\n"
    msg += f"  • Estymata kosztow: $${local['est_cost_usd']:.4f}\n"
    if local["rate_limited"] > 0:
        msg += f"  • ⚠ Rate-limited: {local['rate_limited']}\n"

    if "error" not in mcp:
        msg += f"\n🧠 MCP Server:\n"
        msg += f"  • Status: {mcp.get('status', '?')}\n"
        msg += f"  • Tools: {mcp.get('tools_count', '?')}\n"
        last_pull = mcp.get("vault_last_pulled", "")
        if last_pull:
            msg += f"  • Vault last pull: {last_pull[:19].replace('T', ' ')} UTC\n"

    msg += "\n⚠ Alerts: brak\n"
    msg += "\n(W produkcji - Worker DLQ + Anthropic real cost dolozymy w Sprint 1)"

    await update.message.reply_text(msg)


# ============================================================
# /audit - przeglad ostatnich akcji (admin debug)
# ============================================================

async def handle_digest_auto(context: ContextTypes.DEFAULT_TYPE):
    """Auto-trigger daily digest (z JobQueue, 09:00 UTC).

    Wysyla do wszystkich admin_user_ids ten sam content co /digest.
    """
    from .services import usage_stats
    from .config import settings

    local = usage_stats.stats_local(window_hours=24)
    directus = await usage_stats.stats_directus(window_hours=24)
    mcp = await usage_stats.stats_mcp()

    msg = "📊 UL OS Daily Digest (auto, 09:00)\n\n"

    if "error" not in directus:
        msg += f"📥 Ostatnie 24h: {directus['recent_count']} nowych dokumentow\n"
        msg += f"📊 Total: {directus['total']}\n"

    msg += f"\n🤖 Bot (24h): {local['total_events']} events, "
    msg += f"~${local['est_cost_usd']:.4f} estymata\n"

    if "error" not in mcp:
        msg += f"🧠 MCP: {mcp.get('status', '?')}, {mcp.get('tools_count', '?')} tools\n"

    if local["rate_limited"] > 0:
        msg += f"\n⚠ Rate-limited: {local['rate_limited']}\n"

    # Send do każdego admin
    for chat_id in settings.admin_user_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            log.error("Daily digest send failed for %s: %s", chat_id, e)


async def handle_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Przeglad ostatnich 20 akcji z audit.jsonl."""
    from . import audit as audit_mod

    # Read last 20 lines z audit.jsonl
    if not audit_mod.AUDIT_FILE.exists():
        await update.message.reply_text("Audit log pusty.")
        return

    lines = []
    try:
        with open(audit_mod.AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                lines.append(line.strip())
        lines = lines[-20:]  # last 20
    except OSError as e:
        await update.message.reply_text(f"Audit read error: {e}")
        return

    if not lines:
        await update.message.reply_text("Audit log pusty.")
        return

    import json
    msg = f"Audit log (ostatnie {len(lines)} akcji):\n\n"
    for line in lines:
        try:
            e = json.loads(line)
            ts = e.get("iso", "?")[:19].replace("T", " ")
            user = e.get("username") or "system"
            action = e.get("action", "?")
            result = e.get("result", "?")
            error = e.get("error")
            args = e.get("args")

            line_msg = f"[{ts}] @{user} /{action}={result}"
            if args:
                line_msg += f" ({args[:30]})"
            if error and result != "ok":
                line_msg += f" ⚠ {error[:40]}"
            msg += line_msg + "\n"
        except json.JSONDecodeError:
            continue

    # Telegram message limit: 4096 chars
    if len(msg) > 3500:
        msg = msg[:3500] + "\n... (truncated)"

    await update.message.reply_text(msg)


# ============================================================
# /status - agregat: bot + Directus + MCP + DLQ + queue + Vault
# Sprint 1.6 (ADR Hubert)
# ============================================================

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Composite status - zlozenie /health + /breakers + DLQ count + queue count."""
    from .breaker import breakers
    import time

    lines = ["📊 UL OS — status zbiorczy"]
    lines.append("")

    # 1. Bot uptime
    if context.application.bot_data.get("started_at"):
        up = int(time.time() - context.application.bot_data["started_at"])
        hrs = up // 3600
        mins = (up % 3600) // 60
        lines.append(f"🤖 Bot: up {hrs}h {mins}m, whitelist={len(settings.admin_user_ids)}")
    else:
        lines.append(f"🤖 Bot: aktywny, whitelist={len(settings.admin_user_ids)}")

    # 2. Circuit breakers
    breaker_summary = []
    for name, br in breakers.items():
        state = br.get_state().state.value
        emoji = "🟢" if state == "closed" else ("🟡" if state == "half_open" else "🔴")
        breaker_summary.append(f"{emoji}{name}")
    lines.append("⚡ Breakers: " + " ".join(breaker_summary))

    # 3. Directus quick check
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.directus_url}/server/health")
            if r.status_code == 200:
                lines.append("🗄️  Directus: OK")
            else:
                lines.append(f"🗄️  Directus: HTTP {r.status_code}")
    except Exception as e:
        lines.append(f"🗄️  Directus: ⚠ {str(e)[:40]}")

    # 4. MCP server quick check
    if settings.mcp_base_url and settings.mcp_bearer_token:
        try:
            from .services.mcp_client import mcp_status
            mcp_info = await mcp_status()
            if mcp_info.get("ok"):
                lines.append(f"🧠 MCP: OK ({mcp_info.get('tools_count', '?')} tools)")
            else:
                lines.append(f"🧠 MCP: ⚠ {mcp_info.get('error', '?')[:40]}")
        except Exception as e:
            lines.append(f"🧠 MCP: ⚠ {str(e)[:40]}")

    # 5. HOS queue + DLQ counts
    if settings.s3_endpoint and settings.s3_access_key_id:
        try:
            from .services.notifier import _count_objects_sync
            import asyncio as _asyncio
            inbox = await _asyncio.to_thread(_count_objects_sync, "inbox/")
            failed = await _asyncio.to_thread(_count_objects_sync, "inbox-failed/")
            processed_today_prefix = f"inbox-processed/{__import__('datetime').date.today().isoformat()}/"
            processed = await _asyncio.to_thread(_count_objects_sync, processed_today_prefix)
            lines.append(
                f"📦 HOS: inbox={inbox} | processed today={processed} | DLQ={failed}"
            )
        except Exception as e:
            lines.append(f"📦 HOS: ⚠ {str(e)[:40]}")

    # 6. Vault HEAD (jezeli moglbym z MCP, na razie pomijam — robotem narzedzie)
    # TODO: dodac vault_status z MCP gdy jest endpoint

    # 7. Ostatni audit event (kto co kiedy)
    try:
        import json as _json
        from pathlib import Path as _Path
        audit_path = _Path("logs/audit.jsonl")
        if audit_path.exists():
            with open(audit_path, "rb") as f:
                f.seek(0, 2)  # end
                size = f.tell()
                f.seek(max(0, size - 4096))  # ostatnie ~4KB
                tail = f.read().decode("utf-8", errors="ignore")
                last_lines = [l for l in tail.split("\n") if l.strip()][-1:]
                if last_lines:
                    try:
                        e = _json.loads(last_lines[-1])
                        ts = e.get("iso", "?")[11:16]
                        lines.append(
                            f"📝 Last audit: {ts} @{e.get('username','?')} /{e.get('action','?')}={e.get('result','?')}"
                        )
                    except _json.JSONDecodeError:
                        pass
    except Exception:
        pass

    lines.append("")
    lines.append("📚 Więcej: /health /breakers /dlq /audit /koszty /ulos_status")

    await update.message.reply_text("\n".join(lines))


# ============================================================
# /alerts - manualny check proactive notifier
# Sprint 1.11 manual check
# ============================================================

async def handle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manualnie sprawdz wszystkie alerty (DLQ, queue, _NEEDS_REVIEW, deadlines)."""
    from .services import notifier
    results = await notifier.manual_run()

    lines = ["🔔 Alerts — manual check"]
    lines.append("")
    lines.append(f"🪦 DLQ: {results['dlq']}")
    lines.append("")
    lines.append(f"⏳ Queue: {results['queue']}")
    lines.append("")
    lines.append(f"📋 _NEEDS_REVIEW: {results['needs_review']}")
    lines.append("")

    deadlines = results.get("deadlines", [])
    if deadlines and deadlines != ["OK"]:
        lines.append(f"⏰ Deadlines:")
        for d in deadlines:
            lines.append(f"  {d}")
    else:
        lines.append("⏰ Deadlines: OK (brak deadlinow <7 dni)")

    msg = "\n".join(lines)
    if len(msg) > 3900:
        msg = msg[:3900] + "\n…(obciete)"
    await update.message.reply_text(msg)


# ============================================================
# /generate <vault-path-or-slug> - DOCX z Vault markdown
# Sprint 1.10
# ============================================================

async def handle_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Generuje DOCX z markdown w Vault.

    Użycie: /generate <vault-path>
    np.: /generate 50 — BIDBEE/_INBOX/2026-05-11_brief-bike-box-v6.md
    np.: /generate brief-bike-box-v6  (auto-search w Vault przez MCP)
    """
    if not context.args:
        await update.message.reply_text(
            "Uzycie: /generate <vault-path-or-slug>\n\n"
            "Przyklad:\n"
            "  /generate 50 — BIDBEE/_INBOX/2026-05-11_brief-bike-box-v6.md\n"
            "  /generate brief-bike-box-v6 (auto-search)"
        )
        return

    from .services import generator
    query = " ".join(context.args).strip()

    progress = await update.message.reply_text(f"📄 Generuje DOCX dla: {query}\nProszę poczekać...")

    try:
        result = await generator.generate_docx_from_vault(query)
        if result.success:
            await progress.edit_text(
                f"✅ DOCX wygenerowany!\n\n"
                f"Plik: {result.filename}\n"
                f"Rozmiar: {result.size_bytes / 1024:.1f} KB\n"
                f"Vault source: {result.source_vault_path}\n\n"
                f"📥 Pobierz: {result.download_url}"
            )
        else:
            await progress.edit_text(
                f"❌ Generowanie nie powiodlo sie\n"
                f"Blad: {result.error}"
            )
    except Exception as e:
        log.exception("generate failed")
        await progress.edit_text(f"❌ Blad generatora: {e}")


# ============================================================
# /research <prompt> - Perplexity Deep Research → Vault
# Sprint 1.7
# ============================================================

async def handle_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Wysyla prompt do Perplexity Sonar Deep Research, zapisuje wynik do HOS inbox/
    -> worker przerobi i wpisze do Directus + Vault.

    Uzycie: /research <prompt>
    """
    if not context.args:
        await update.message.reply_text(
            "Uzycie: /research <prompt>\n\n"
            "Przyklady:\n"
            "  /research stan rynku BESS w Polsce 2026\n"
            "  /research konkurenci BEEzzy 250kWh BESS"
        )
        return

    # Sprint 1.7 v2: domyślnie Anthropic web_search (klucz mamy aktywny),
    # fallback Perplexity jeśli RESEARCH_PROVIDER=perplexity + PERPLEXITY_API_KEY.
    if not (settings.anthropic_api_key or settings.perplexity_api_key):
        await update.message.reply_text(
            "⚠️ Brak ANTHROPIC_API_KEY (default) ani PERPLEXITY_API_KEY (fallback).\n\n"
            "Aby aktywować /research:\n"
            "• Default (Anthropic web_search): wystarczy ANTHROPIC_API_KEY w .env\n"
            "• Alternatywa Perplexity: PERPLEXITY_API_KEY + RESEARCH_PROVIDER=perplexity"
        )
        return

    from .services import research
    prompt = " ".join(context.args).strip()

    progress = await update.message.reply_text(
        f"🔍 Research:\n  {prompt[:120]}{'...' if len(prompt)>120 else ''}\n\n"
        "Provider: Claude + web_search + vault_search\n"
        "Proszę czekać (zwykle 20-60s)..."
    )

    try:
        result = await research.research(prompt)
        if result.success:
            user = update.effective_user
            uploaded = await research.upload_to_inbox(
                result.markdown,
                prompt=prompt,
                provider=result.provider,
                telegram_user_id=user.id if user else 0,
                telegram_username=user.username if user else None,
            )
            web_str = f"🌐 web_search: {result.web_search_count}" if result.web_search_count else ""
            await progress.edit_text(
                f"✅ Research gotowy! ({result.provider})\n\n"
                f"📋 Prompt: {prompt[:100]}{'...' if len(prompt)>100 else ''}\n"
                f"📊 Tokens: {result.input_tokens} in / {result.output_tokens} out\n"
                f"{web_str}\n"
                f"💰 Cost: ~${result.cost_usd:.4f}\n"
                f"📚 Citations: {len(result.citations)}\n\n"
                f"🗂️ HOS: {uploaded.s3_key}\n"
                f"⏱️ Worker przetworzy w ~30s i wpisze do Directus + Vault"
            )
        else:
            await progress.edit_text(f"❌ Research fail: {result.error}")
    except Exception as e:
        log.exception("research failed")
        await progress.edit_text(f"❌ Blad: {e}")


# ============================================================
# /ask <pytanie> - Conversational Claude z dostepem do Vault
# Sprint 1.9
# ============================================================

async def handle_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Pytanie do Claude z dostepem do Vault (przez MCP tools).
    Multi-turn: zapamietuje kontekst per user.

    Uzycie: /ask <pytanie>
           /ask reset  (czyszczenie kontekstu)
    """
    if not context.args:
        await update.message.reply_text(
            "Uzycie: /ask <pytanie>\n"
            "       /ask reset (czysci historie rozmowy)\n\n"
            "Claude ma dostep do Vault (vault_search, vault_read) i Directus."
        )
        return

    if not settings.anthropic_api_key:
        await update.message.reply_text(
            "⚠️ Anthropic API key nie skonfigurowany.\n\n"
            "Aby aktywować Sprint 1.9 Conversational Claude:\n"
            "1. Załóż konto na https://console.anthropic.com\n"
            "2. Utwórz API key\n"
            "3. Wpisz do .env: ANTHROPIC_API_KEY=sk-ant-...\n"
            "4. Restart bota"
        )
        return

    from .services import conversational
    user = update.effective_user
    user_id = user.id if user else 0
    prompt = " ".join(context.args).strip()

    if prompt.lower() == "reset":
        conversational.reset_context(user_id)
        await update.message.reply_text("🔄 Kontekst rozmowy wyczyszczony.")
        return

    progress = await update.message.reply_text("🧠 Claude myśli...")

    try:
        response = await conversational.ask(user_id, prompt)
        if response.success:
            full = response.text
            if response.tool_calls:
                full += f"\n\n🔧 Tools użyte: {', '.join(response.tool_calls)}"
            full += f"\n\n💰 ~${response.cost_usd:.4f} ({response.input_tokens}+{response.output_tokens} tok)"

            if len(full) > 3900:
                # Telegram split na multiple messages
                await progress.edit_text(full[:3900] + "\n…(1/2)")
                for chunk_start in range(3900, len(full), 3900):
                    chunk = full[chunk_start:chunk_start + 3900]
                    await update.message.reply_text(chunk)
            else:
                await progress.edit_text(full)
        else:
            await progress.edit_text(f"❌ Claude error: {response.error}")
    except Exception as e:
        log.exception("ask failed")
        await progress.edit_text(f"❌ Blad: {e}")


# ============================================================
# /dlq retry — przerzuć failed z powrotem do inbox/
# Sprint 1.5+ operational
# ============================================================

async def _handle_dlq_retry(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
    """
    Subcomenda dla /dlq retry. Args:
      ['<inbox-failed/...>']  — retry konkretnego key
      ['all']                  — retry wszystkich (max 50)
      ['all', 'YYYY-MM-DD']    — retry tylko z konkretnej daty
      ['YYYY-MM-DD']           — retry z konkretnej daty (alias)
    """
    from .services import dlq

    if not args:
        await update.message.reply_text(
            "Uzycie:\n"
            "  /dlq retry <inbox-failed/.../file>  — przerzuc konkretny\n"
            "  /dlq retry all                       — przerzuc wszystkie (max 50)\n"
            "  /dlq retry 2026-05-11                — przerzuc tylko z daty"
        )
        return

    arg = args[0]
    # Pattern: inbox-failed/...
    if arg.startswith("inbox-failed/"):
        progress = await update.message.reply_text(f"⏳ Retry: {arg[:80]}...")
        result = await dlq.retry_dlq_item(arg)
        if result.success:
            await progress.edit_text(
                f"✅ Retry OK\n\n"
                f"From: {result.moved_from}\n"
                f"To:   {result.moved_to}\n\n"
                f"⏱️  Worker przerobi w ~30s"
            )
        else:
            await progress.edit_text(f"❌ Retry fail: {result.error}")
        return

    # Pattern: 'all' lub 'YYYY-MM-DD'
    date_filter = None
    if arg.lower() != "all":
        # Sprawdź czy format daty
        import re
        if re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            date_filter = arg
        else:
            await update.message.reply_text(
                f"Nieznany argument: {arg}\n"
                "Uzyj 'all', 'YYYY-MM-DD' lub pelny klucz inbox-failed/..."
            )
            return

    # Args[1] może być date po 'all'
    if arg.lower() == "all" and len(args) > 1:
        import re
        if re.match(r"^\d{4}-\d{2}-\d{2}$", args[1]):
            date_filter = args[1]

    progress = await update.message.reply_text(
        f"⏳ Bulk retry: filter={date_filter or 'all'}, max=50..."
    )
    result = await dlq.retry_all_dlq(date_filter=date_filter, max_items=50)

    msg = (
        f"📋 Bulk retry done\n\n"
        f"Filter: {result.get('filter', '?')}\n"
        f"✅ Przerzucone: {result.get('moved', 0)}\n"
        f"❌ Bledy: {result.get('errors', 0)}\n\n"
    )
    log_items = result.get("log", [])
    if log_items:
        msg += "Detale:\n"
        for line in log_items[:15]:
            msg += f"  {line[:80]}\n"
        if len(log_items) > 15:
            msg += f"  …(+{len(log_items)-15} more)\n"

    if len(msg) > 3900:
        msg = msg[:3900] + "\n…"
    await progress.edit_text(msg)


# ============================================================
# /upload-stats — statystyki upload per user (z audit log + S3)
# Sprint 2 — operacyjne
# ============================================================

async def handle_upload_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Statystyki upload: dziś / 7d / 30d, per user, breakdown po typie pliku.

    Z audit.jsonl (lokalny) + S3 inbox-processed/.

    Argumenty: /upload-stats [days]  default 7
    """
    from datetime import datetime, timedelta, timezone as _tz
    import json as _json
    from pathlib import Path as _Path

    days = 7
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 90))
        except ValueError:
            pass

    now = datetime.now(_tz.utc)
    cutoff = now - timedelta(days=days)

    # 1. Audit log analiza
    audit_path = _Path("logs/audit.jsonl")
    if not audit_path.exists():
        await update.message.reply_text("Brak audit.jsonl")
        return

    by_user: dict[str, dict[str, int]] = {}
    by_type: dict[str, int] = {}
    total = 0
    by_day: dict[str, int] = {}

    try:
        with open(audit_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = _json.loads(line)
                except _json.JSONDecodeError:
                    continue

                action = e.get("action", "")
                # Tylko zdarzenia upload (handle_document/photo/voice)
                if action not in ("document", "photo", "voice"):
                    continue

                iso = e.get("iso", "")
                try:
                    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < cutoff:
                    continue

                user = e.get("username") or f"user_{e.get('user_id', '?')}"
                if user not in by_user:
                    by_user[user] = {"total": 0, "document": 0, "photo": 0, "voice": 0}
                by_user[user]["total"] += 1
                by_user[user][action] = by_user[user].get(action, 0) + 1

                by_type[action] = by_type.get(action, 0) + 1
                total += 1

                day = ts.strftime("%Y-%m-%d")
                by_day[day] = by_day.get(day, 0) + 1
    except Exception as e:
        await update.message.reply_text(f"❌ Audit parse fail: {e}")
        return

    if total == 0:
        await update.message.reply_text(
            f"📊 Upload stats — ostatnie {days} dni\n\nBrak uploadów."
        )
        return

    lines = [f"📊 Upload stats — ostatnie {days} dni"]
    lines.append("")
    lines.append(f"Razem: {total} uploads")
    lines.append("")

    # By type
    lines.append("Po typie:")
    for t in ["document", "photo", "voice"]:
        count = by_type.get(t, 0)
        if count:
            emoji = {"document": "📄", "photo": "🖼️", "voice": "🎙️"}[t]
            lines.append(f"  {emoji} {t}: {count}")
    lines.append("")

    # By user
    lines.append("Po użytkowniku:")
    for u, stats in sorted(by_user.items(), key=lambda x: -x[1]["total"]):
        lines.append(
            f"  @{u}: {stats['total']} "
            f"(doc {stats.get('document', 0)} / foto {stats.get('photo', 0)} / voice {stats.get('voice', 0)})"
        )
    lines.append("")

    # By day (last 7 dni)
    if days <= 14:
        lines.append("Po dniu:")
        for day in sorted(by_day.keys(), reverse=True):
            bar = "█" * min(by_day[day], 20)
            lines.append(f"  {day}: {by_day[day]:3d} {bar}")

    await update.message.reply_text("\n".join(lines))
