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
    """Voice memo (OGG opus) -> HOS inbox/ -> Worker (Whisper transkrypcja w roadmapie)."""
    voice = update.message.voice
    log.info("voice received: duration=%ss size=%s", voice.duration, voice.file_size)
    await _forward_to_worker(
        update,
        file_id=voice.file_id,
        file_size=voice.file_size or 0,
        filename=f"telegram_voice_{voice.file_unique_id}_{voice.duration}s.ogg",
        mime_type=voice.mime_type or "audio/ogg",
        ack_prefix="🎙️  Voice:",
    )


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
    """Lista failed items czekajacych na review/retry."""
    from .services import dlq

    result = await dlq.list_dlq_items(limit=10)

    msg = "Dead Letter Queue (failed ingests)\n\n"
    msg += f"Status: {result['status']}\n\n"
    msg += result.get("message", "")

    if result["total"] > 0:
        msg += f"\n\nItems ({result['total']}):\n"
        for item in result["items"][:10]:
            msg += f"  • {item.get('hash', '?')[:12]}... - {item.get('error', '?')}\n"

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
