"""/komplet <Nr> — wyzwala montaż kompletu TPO w workerze (HTTP) i zwraca wynik.

Worker robi całą robotę: zbiera dokumenty z office@ Gmail, składa PDF w kolejności,
wrzuca do Drive KOMPLETY i wysyła mailem do Andrzeja. Bot tylko wyzwala i pokazuje wynik.
"""
from __future__ import annotations

import os

import httpx
from telegram import Update
from telegram.ext import ContextTypes

KOMPLET_URL = os.getenv("KOMPLET_URL", "http://ul-os-worker:8080")

_SLOT_PL = {
    "kwit": "kwit wagowy",
    "cmr": "CMR",
    "zgloszenie": "zgłoszenie/Aneks",
    "art15e": "Art. 15e",
}
_SLOT_ORDER = ["kwit", "cmr", "zgloszenie", "art15e"]


async def cmd_komplet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # deferred import — unika cyklu (main.py importuje ten moduł)
    from .main import authorized_or_ignore

    if not await authorized_or_ignore(update, context):
        return

    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Użycie: /komplet <Nr>\nnp. /komplet 214")
        return

    nr = int(args[0])
    notif = args[1] if len(args) > 1 else None

    status = await update.message.reply_text(f"⏳ Składam komplet Nr {nr}…")

    params: dict[str, object] = {"nr": nr}
    if notif:
        params["notif"] = notif

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(f"{KOMPLET_URL}/komplet", params=params)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"❌ Błąd połączenia z workerem: {e}")
        return

    if not data.get("ok"):
        err = data.get("error") or "brak dokumentów do złożenia"
        await status.edit_text(f"❌ Komplet Nr {nr}: {err}")
        return

    slots = data.get("slots", {})
    have = [_SLOT_PL[k] for k in _SLOT_ORDER if slots.get(k)]
    missing = [_SLOT_PL.get(k, k) for k in data.get("missing", [])]

    lines = [f"✅ Komplet Nr {nr} — {data.get('pages')} str."]
    if have:
        lines.append("Zawiera: " + ", ".join(have))
    if missing:
        lines.append("⚠️ Brakuje: " + ", ".join(missing))
    if data.get("emailed"):
        lines.append("📧 Wysłano na skrzynkę Andrzeja")
    if data.get("link"):
        lines.append(data["link"])

    await status.edit_text("\n".join(lines))
