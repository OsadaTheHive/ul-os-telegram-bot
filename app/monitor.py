"""
Background monitoring job - sprawdza health Directus + MCP + Vault co X minut.

Jezeli cos pad - alert do wszystkich w ADMIN_CHAT_IDS przez bota.

Uruchamia sie automatycznie z post_init w main.py:
    app.job_queue.run_repeating(monitor.tick, interval=300, first=60)

Failure threshold (anty-flapping):
    - Pierwsza failed check NIE alertuje (pewnie chwilowy timeout)
    - Druga failed check (= 10 min downtime) → alert
    - Po recovery → "✓ przywrocone"

State per komponent w in-memory dict (resetuje sie przy restart bota).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from telegram.ext import ContextTypes

from . import audit
from .config import settings

log = logging.getLogger(__name__)


# State per komponent: {"directus": {"failures": 0, "last_alert_ts": 0}, ...}
_state: dict[str, dict[str, Any]] = {}

# Po ilu kolejnych pad wysyłamy alert (anty-flapping)
ALERT_AFTER_FAILURES = 2

# Cooldown między alertami tej samej awarii (sekundy)
ALERT_COOLDOWN = 1800  # 30 min


def _bump_failure(component: str) -> int:
    """Inkrementuj licznik failures, zwroc nowa wartosc."""
    s = _state.setdefault(component, {"failures": 0, "last_alert_ts": 0})
    s["failures"] += 1
    return s["failures"]


def _reset_failure(component: str) -> int:
    """Reset licznika, zwroc PREVIOUS wartosc (czy bylo padded)."""
    s = _state.setdefault(component, {"failures": 0, "last_alert_ts": 0})
    prev = s["failures"]
    s["failures"] = 0
    return prev


def _can_alert(component: str) -> bool:
    """Czy mozemy juz alertowac (nie spam)."""
    s = _state.setdefault(component, {"failures": 0, "last_alert_ts": 0})
    return time.time() - s["last_alert_ts"] >= ALERT_COOLDOWN


def _mark_alerted(component: str):
    _state.setdefault(component, {"failures": 0, "last_alert_ts": 0})
    _state[component]["last_alert_ts"] = time.time()


async def _check_directus(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        r = await client.get(f"{settings.directus_url}/server/health", timeout=8)
        if r.status_code == 200:
            return True, "Directus OK"
        return False, f"Directus zwrocil {r.status_code}"
    except Exception as e:
        return False, f"Directus exception: {e.__class__.__name__}"


async def _check_mcp(client: httpx.AsyncClient) -> tuple[bool, str]:
    if not settings.mcp_bearer_token:
        return True, "MCP skipped (no token)"
    try:
        r = await client.get(
            f"{settings.mcp_base_url}/health",
            headers={"Authorization": f"Bearer {settings.mcp_bearer_token}"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            return True, f"MCP OK ({data.get('tools_count', '?')} tools)"
        return False, f"MCP zwrocil {r.status_code}"
    except Exception as e:
        return False, f"MCP exception: {e.__class__.__name__}"


async def tick(context: ContextTypes.DEFAULT_TYPE):
    """Job entry point - wywołane co 5 min przez JobQueue."""
    log.info("Monitor tick: checking health...")

    async with httpx.AsyncClient() as client:
        checks = {
            "directus": await _check_directus(client),
            "mcp": await _check_mcp(client),
        }

    # Per komponent: jezeli pad i przekroczył threshold → alert
    for component, (ok, msg) in checks.items():
        if ok:
            prev_failures = _reset_failure(component)
            if prev_failures >= ALERT_AFTER_FAILURES:
                # Recovery alert
                await _alert(context, f"✅ {component.upper()} przywrocone: {msg}")
                audit.write(
                    user_id=None,
                    username="monitor",
                    action="recovery",
                    extra={"component": component, "failures_before_recovery": prev_failures},
                )
        else:
            failures = _bump_failure(component)
            log.warning("Monitor: %s failed (#%d): %s", component, failures, msg)
            if failures >= ALERT_AFTER_FAILURES and _can_alert(component):
                await _alert(
                    context,
                    f"🔴 {component.upper()} pad: {msg}\n"
                    f"(po {failures} kolejnych failed check'ach, ~{(failures * 5)} min downtime)",
                )
                _mark_alerted(component)
                audit.write(
                    user_id=None,
                    username="monitor",
                    action="alert_sent",
                    result="error",
                    error=msg,
                    extra={"component": component, "failures": failures},
                )


async def _alert(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Wyślij alert do wszystkich w whitelist."""
    for chat_id in settings.admin_user_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"[UL OS Monitor]\n{message}",
            )
        except Exception as e:
            log.error("Alert send failed for chat_id=%s: %s", chat_id, e)
