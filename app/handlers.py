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
    """Sprawdz Worker + Directus + B2 (jak juz beda)."""
    msg = "Status UL OS:\n"

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

    # B2 check (placeholder)
    if settings.b2_application_key_id:
        msg += " * Backblaze B2: skonfigurowany\n"
    else:
        msg += " * Backblaze B2: brak kluczy (Tier 0 milestone)\n"

    await update.message.reply_text(msg)


async def handle_szukaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "Uzycie: /szukaj <query>\n"
            "Przyklad: /szukaj magazyn energii LFP 5kWh"
        )
        return
    await update.message.reply_text(
        f'Szukam: "{query}"\n\n'
        "(WIP - semantic search bedzie dostepny po wlaczeniu pgvector w Q3 2026)"
    )


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


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plik wyslany do bota -> ack + (TODO) forward do Workera."""
    doc = update.message.document
    log.info("document received: name=%s size=%s mime=%s", doc.file_name, doc.file_size, doc.mime_type)
    await update.message.reply_text(
        f"Otrzymalem: {doc.file_name}\n"
        f"Rozmiar: {doc.file_size / 1024 / 1024:.2f} MB\n"
        f"Typ: {doc.mime_type}\n\n"
        "(WIP - po Tier 0 plik trafi do INBOX i Worker UL OS go przetworzy)"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]  # najwiekszy rozmiar
    log.info("photo received: file_id=%s size=%s", photo.file_id, photo.file_size)
    await update.message.reply_text(
        f"Otrzymalem foto ({photo.file_size / 1024:.0f} KB).\n\n"
        "(WIP - multimodal AI opisze foto + klasyfikator po Tier 0)"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    log.info("voice received: duration=%ss size=%s", voice.duration, voice.file_size)
    await update.message.reply_text(
        f"Otrzymalem voice memo ({voice.duration}s).\n\n"
        "(WIP - Whisper transkrypcja w Q3 2026 wg roadmapy)"
    )
