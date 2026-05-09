"""
Idempotency cache dla file ingest - chroni przed duplikatami.

Per FILAR 1 S-1 z planu autonomii:
  Każdy plik dostaje deterministyczny ID = sha256(content).
  Worker przed ingest sprawdza Directus knowledge_items.content_hash.
  Bot przy file ingest - sprawdza local cache + Directus (gdy bedzie wpiety).

Aktualnie (bez Worker w Coolify) - lokalny LRU cache w bocie:
  - sha256 hash (file_id z Telegrama lub content if dostepny)
  - 1000 ostatnich plikow
  - Zapobiega duplikatowemu /document message gdy Hubert/Grzegorz wysyla
    ten sam plik 2x w 10 min
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from threading import Lock

log = logging.getLogger(__name__)


class IdempotencyCache:
    """LRU cache idempotency keys (in-memory, thread-safe).

    Resetuje sie przy restart bota. Dla persistent cache - uzyj Redis
    (Coolify ma Redis natywnie dla projektu hive-live).
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 3600):
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = Lock()

    def _evict_expired(self):
        """Remove entries older than TTL."""
        now = time.monotonic()
        expired = [k for k, ts in self._cache.items() if now - ts > self._ttl]
        for k in expired:
            del self._cache[k]

    def check_and_mark(self, key: str) -> bool:
        """Returns True if key is NEW (proceed), False if duplicate."""
        with self._lock:
            self._evict_expired()
            if key in self._cache:
                # Move to end (LRU touch) and return False
                self._cache.move_to_end(key)
                return False

            # New key - mark
            self._cache[key] = time.monotonic()
            # Evict oldest if over capacity
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            return True

    def stats(self) -> dict:
        with self._lock:
            return {"size": len(self._cache), "max_size": self._max_size, "ttl_seconds": self._ttl}


# Singleton
cache = IdempotencyCache(max_size=1000, ttl_seconds=3600)


def telegram_file_key(file_id: str, file_size: int | None) -> str:
    """Generate idempotency key for Telegram file."""
    return hashlib.sha256(f"tg:{file_id}:{file_size or 0}".encode()).hexdigest()[:16]
