"""
Rate limiter (token bucket per user) - chroni przed flood.

Uzycie:
    if not limiter.allow(user_id, "produkt", limit=5, window=60):
        await update.message.reply_text("Za szybko, sprobuj za chwile.")
        return

Aktualnie in-memory (resetuje sie przy restartcie). Dla wielu instancji
uzyc Redis (`redis.incr` z TTL).
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from threading import Lock

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding window per (user, key) - thread-safe."""

    def __init__(self):
        self._buckets: dict[tuple[int, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, user_id: int, key: str, *, limit: int, window: float) -> bool:
        """Zwraca True jezeli mozna, False jezeli przekroczono limit.

        Args:
            user_id: Telegram user_id
            key: nazwa komendy / endpointu (np. "produkt", "global")
            limit: max requestow
            window: okno czasu w sekundach (np. 60)
        """
        now = time.monotonic()
        bucket_key = (user_id, key)
        with self._lock:
            bucket = self._buckets[bucket_key]
            # Wywal stare timestamps poza okno
            while bucket and bucket[0] < now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                log.warning(
                    "Rate limit hit: user=%s key=%s (%d/%d in last %ss)",
                    user_id,
                    key,
                    len(bucket),
                    limit,
                    window,
                )
                return False
            bucket.append(now)
            return True

    def remaining(self, user_id: int, key: str, *, limit: int, window: float) -> int:
        """Ile jeszcze wolno requestow w oknie."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets[(user_id, key)]
            while bucket and bucket[0] < now - window:
                bucket.popleft()
            return max(0, limit - len(bucket))

    def stats(self) -> dict:
        """Stats do /ulos_status albo /admin."""
        with self._lock:
            return {
                "total_buckets": len(self._buckets),
                "total_events": sum(len(b) for b in self._buckets.values()),
            }


# Singleton dla aplikacji
limiter = RateLimiter()


# Domyslne limity per komenda - mozna nadpisac z env w przyszlosci
LIMITS = {
    # Lekkie komendy: 30/min
    "start": (30, 60),
    "help": (30, 60),
    "health": (30, 60),
    # Cieżkie (Directus query): 10/min
    "produkt": (10, 60),
    "ostatnie": (10, 60),
    "ulos_status": (10, 60),
    "szukaj": (10, 60),
    # MCP queries: 5/min (chroni przed wyczerpaniem MCP rate limits)
    "mcp_status": (10, 60),
    "mcp_szukaj": (5, 60),
    # File ingest: 20/min dla zwyklych userow (admini bez limitu - patrz check()).
    # Podniesione z 3, bo wielostronicowe protokoly/komplety byly ucinane.
    "document": (20, 60),
    "photo": (20, 60),
    "voice": (20, 60),
    # Globalny limit: 120/min na usera (sumarycznie)
    "_global": (120, 60),
}


def _load_admin_ids() -> set[int]:
    """Telegram user_id administratorow z env ADMIN_CHAT_IDS (CSV). Bez limitu."""
    raw = os.environ.get("ADMIN_CHAT_IDS", "")
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


ADMIN_IDS = _load_admin_ids()


def check(user_id: int, key: str) -> tuple[bool, str | None]:
    """Helper - zwraca (allowed, error_message_pl).

    Administratorzy (ADMIN_CHAT_IDS) sa zwolnieni z limitow - moga wrzucac cale
    wielostronicowe protokoly/komplety bez gubienia stron.
    Dla pozostalych: sprawdza najpierw _global, potem per-key.
    """
    if user_id in ADMIN_IDS:
        return True, None

    g_limit, g_window = LIMITS["_global"]
    if not limiter.allow(user_id, "_global", limit=g_limit, window=g_window):
        return False, f"Za duzo zapytań (>{g_limit}/min). Sprobuj za chwile."

    if key in LIMITS:
        limit, window = LIMITS[key]
        if not limiter.allow(user_id, key, limit=limit, window=window):
            return False, f"Za szybko z /{key} (>{limit}/{int(window)}s). Poczekaj."

    return True, None
