"""/komplet — montaż kompletu TPO przez workera. Tryby:
  /komplet              → lista numerów jako przyciski (klikasz)
  /komplet 214          → jeden
  /komplet 214-230      → zakres
  /komplet wszystkie    → wszystkie numery z dokumentami
Przyciski: callback rejestrowany w locie (bez zmian w main.py).
"""
from __future__ import annotations

import os
import re

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

KOMPLET_URL = os.getenv("KOMPLET_URL", "http://ul-os-worker:8080")

_SLOT_PL = {"kwit": "kwit wagowy", "cmr": "CMR", "zgloszenie": "zgłoszenie/Aneks", "art15e": "Art. 15e"}
_SLOT_ORDER = ["kwit", "cmr", "zgloszenie", "art15e"]

_cb_registered = False


async def _get(path: str, params: dict, timeout: float = 120) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{KOMPLET_URL}{path}", params=params)
    return r.json()


def _fmt_single(nr: int, data: dict) -> str:
    if not data.get("ok"):
        return f"❌ Komplet Nr {nr}: {data.get('error') or 'brak dokumentów'}"
    slots = data.get("slots", {})
    have = [_SLOT_PL[k] for k in _SLOT_ORDER if slots.get(k)]
    missing = [_SLOT_PL.get(k, k) for k in data.get("missing", [])]
    head = "✅" if not missing else "⚠️"
    lines = [f"{head} Komplet Nr {nr} — {data.get('pages')} str."]
    if have:
        lines.append("Zawiera: " + ", ".join(have))
    if missing:
        lines.append("Brakuje: " + ", ".join(missing))
    if data.get("emailed"):
        lines.append("📧 Wysłano do Andrzeja")
    if data.get("link"):
        lines.append(data["link"])
    return "\n".join(lines)


def _fmt_many(data: dict) -> str:
    results = data.get("results", [])
    ok = [r for r in results if r.get("ok")]
    complete = [r for r in ok if r.get("complete")]
    part = len(ok) - len(complete)
    lines = [f"Złożono {len(ok)}/{len(results)} kompletów."]
    lines.append(f"✅ kompletnych (4/4): {len(complete)}")
    if part:
        lines.append(f"⚠️ niepełnych: {part}")
    for r in complete[:15]:
        lines.append(f"• Nr {r['nr']}: {r.get('link', '')}")
    if len(complete) > 15:
        lines.append(f"… i {len(complete) - 15} więcej (w Drive KOMPLETY)")
    return "\n".join(lines)


async def cmd_komplet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .main import authorized_or_ignore

    if not await authorized_or_ignore(update, context):
        return

    global _cb_registered
    if not _cb_registered:
        context.application.add_handler(CallbackQueryHandler(_cb_komplet, pattern=r"^kmp:"))
        _cb_registered = True

    args = context.args or []
    if not args:
        await _show_menu(update, context)
        return

    a = "".join(args).lower().replace(" ", "")
    if a in ("wszystkie", "all", "*"):
        await _run(update, "⏳ Składam wszystkie gotowe…", {"all": "1"}, many=True, timeout=300)
        return
    m = re.fullmatch(r"(\d{1,3})-(\d{1,3})", a)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if abs(hi - lo) + 1 > 40:
            await update.message.reply_text("Zakres max 40 naraz. Podziel na mniejsze.")
            return
        await _run(update, f"⏳ Składam komplety {lo}–{hi}…", {"from": lo, "to": hi}, many=True, timeout=300)
        return
    if a.isdigit():
        nr = int(a)
        await _run(update, f"⏳ Składam komplet Nr {nr}…", {"nr": nr}, many=False, nr=nr)
        return
    await update.message.reply_text(
        "Użycie:\n"
        "/komplet — lista numerów do kliknięcia\n"
        "/komplet 214 — jeden\n"
        "/komplet 214-230 — zakres\n"
        "/komplet wszystkie"
    )


async def _run(update: Update, wait_text: str, params: dict, many: bool, nr: int = 0, timeout: float = 120):
    msg = await update.message.reply_text(wait_text)
    try:
        data = await _get("/komplet", params, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"❌ Błąd połączenia z workerem: {e}")
        return
    await msg.edit_text(_fmt_many(data) if many else _fmt_single(nr, data))


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Szukam dostępnych numerów…")
    try:
        data = await _get("/komplet/list", {}, timeout=90)
        items = data.get("items", [])
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"❌ Nie mogę pobrać listy: {e}")
        return
    if not items:
        await msg.edit_text("Brak numerów z dokumentami w office@. Wpisz np. /komplet 214 ręcznie.")
        return
    rows, row = [], []
    for it in items[:60]:
        nr = it["nr"]
        mark = "✅" if it.get("complete") else "⚠️"
        row.append(InlineKeyboardButton(f"{mark} {nr}", callback_data=f"kmp:{nr}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("📦 Wszystkie gotowe", callback_data="kmp:all")])
    await msg.edit_text(
        "Wybierz numer (✅ kompletny 4/4, ⚠️ niepełny):",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _cb_komplet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .main import is_authorized

    q = update.callback_query
    await q.answer()
    if not is_authorized(update):
        await q.edit_message_text("Brak dostępu.")
        return
    val = q.data.split(":", 1)[1]
    if val == "all":
        await q.edit_message_text("⏳ Składam wszystkie gotowe…")
        try:
            data = await _get("/komplet", {"all": "1"}, timeout=300)
        except Exception as e:  # noqa: BLE001
            await q.edit_message_text(f"❌ {e}")
            return
        await q.edit_message_text(_fmt_many(data))
        return
    nr = int(val)
    await q.edit_message_text(f"⏳ Składam komplet Nr {nr}…")
    try:
        data = await _get("/komplet", {"nr": nr}, timeout=120)
    except Exception as e:  # noqa: BLE001
        await q.edit_message_text(f"❌ {e}")
        return
    await q.edit_message_text(_fmt_single(nr, data))
