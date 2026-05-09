"""
Audit log - zapis kazdej komendy/akcji do JSONL.

Format: jeden JSON obiekt per linia, w `logs/audit.jsonl`.
Latwo parsowalne (np. `cat audit.jsonl | jq 'select(.user_id == 6908566796)'`).

W przyszlosci (Q3 2026) - persyst do Directus `panel_login_log` lub osobnej tabeli
audit_log per ADR-014 z mojego raportu z 9 maja AM.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

AUDIT_DIR = Path(__file__).parent.parent / "logs"
AUDIT_FILE = AUDIT_DIR / "audit.jsonl"


def _ensure_dir():
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def write(
    *,
    user_id: int | None,
    username: str | None,
    action: str,
    args: str | None = None,
    result: str = "ok",
    error: str | None = None,
    extra: dict[str, Any] | None = None,
):
    """Zapis pojedynczego event do audit.jsonl.

    Args:
        user_id: Telegram user_id (None dla system events)
        username: @username (do logow human-readable)
        action: nazwa komendy lub event (np. "produkt", "unauthorized", "rate_limit")
        args: argumenty komendy (np. "PowerHill 261kWh")
        result: "ok" / "denied" / "error"
        error: opis bledu jezeli result != "ok"
        extra: dodatkowe pola (np. doc_count, response_time_ms)
    """
    _ensure_dir()
    event = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user_id": user_id,
        "username": username,
        "action": action,
        "args": args,
        "result": result,
    }
    if error:
        event["error"] = error
    if extra:
        event["extra"] = extra

    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        # NIGDY nie crash bot'a z powodu logow - tylko warning
        log.warning("Audit write failed: %s", e)


def stats() -> dict:
    """Liczba eventow w audit.jsonl (dla /ulos_status)."""
    _ensure_dir()
    if not AUDIT_FILE.exists():
        return {"events": 0, "size_bytes": 0}
    return {
        "events": sum(1 for _ in open(AUDIT_FILE, encoding="utf-8")),
        "size_bytes": os.path.getsize(AUDIT_FILE),
    }
