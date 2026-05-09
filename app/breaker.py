"""
Circuit breaker dla zewnetrznych HTTP calls (Directus, MCP, Anthropic).

Stany:
  CLOSED   - normalny operating, requesty przechodza
  OPEN     - po N kolejnych failures, requesty natychmiast fail
  HALF_OPEN - po cooldown probuje 1 request, jezeli ok -> CLOSED

Per ADR z planu autonomii (FILAR 1 S-3): chroni przed cascade fail
gdy zewnetrzny serwis pad - bot nie wisi, daje gracefull error.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from threading import Lock
from typing import Any, Callable

log = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Raised gdy circuit jest OPEN i request odrzucony."""


class CircuitBreaker:
    """Per-resource circuit breaker."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0
        self._lock = Lock()

    def _can_transition_to_half_open(self) -> bool:
        if self._state != CircuitState.OPEN or self._opened_at is None:
            return False
        return time.monotonic() - self._opened_at >= self.recovery_timeout

    def _record_success(self):
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                log.info("Circuit %s: HALF_OPEN -> CLOSED (recovered)", self.name)
                self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._half_open_calls = 0

    def _record_failure(self, exc: Exception):
        with self._lock:
            self._failures += 1
            log.warning(
                "Circuit %s: failure %d/%d (%s: %s)",
                self.name,
                self._failures,
                self.failure_threshold,
                exc.__class__.__name__,
                str(exc)[:80],
            )

            if self._state == CircuitState.HALF_OPEN:
                # Test failed - back to OPEN
                log.warning("Circuit %s: HALF_OPEN -> OPEN (test failed)", self.name)
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_calls = 0
            elif self._failures >= self.failure_threshold:
                if self._state == CircuitState.CLOSED:
                    log.error(
                        "Circuit %s: CLOSED -> OPEN (%d failures, cooldown %.0fs)",
                        self.name,
                        self._failures,
                        self.recovery_timeout,
                    )
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()

    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """Wraps async function call - throws CircuitBreakerError if OPEN."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if self._can_transition_to_half_open():
                    log.info("Circuit %s: OPEN -> HALF_OPEN (testing recovery)", self.name)
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                else:
                    raise CircuitBreakerError(
                        f"Circuit {self.name} is OPEN (since {time.monotonic() - (self._opened_at or 0):.0f}s ago)"
                    )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    raise CircuitBreakerError(
                        f"Circuit {self.name} is HALF_OPEN (testing in progress)"
                    )
                self._half_open_calls += 1

        try:
            result = await func(*args, **kwargs)
            self._record_success()
            return result
        except Exception as e:
            self._record_failure(e)
            raise

    @property
    def state(self) -> CircuitState:
        return self._state

    def stats(self) -> dict[str, Any]:
        """Diagnostyka do /health endpoint."""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "failures": self._failures,
                "threshold": self.failure_threshold,
                "opened_at": self._opened_at,
                "seconds_until_half_open": (
                    max(0, self.recovery_timeout - (time.monotonic() - self._opened_at))
                    if self._state == CircuitState.OPEN and self._opened_at is not None
                    else None
                ),
            }


# Singletons per zewnetrzny serwis
breakers: dict[str, CircuitBreaker] = {
    "directus": CircuitBreaker("directus", failure_threshold=3, recovery_timeout=60),
    "mcp": CircuitBreaker("mcp", failure_threshold=3, recovery_timeout=60),
    "anthropic": CircuitBreaker("anthropic", failure_threshold=3, recovery_timeout=120),
}


def get(name: str) -> CircuitBreaker:
    """Pobierz breaker po nazwie - zwraca shared instance."""
    if name not in breakers:
        breakers[name] = CircuitBreaker(name)
    return breakers[name]


def all_stats() -> dict[str, dict]:
    return {name: cb.stats() for name, cb in breakers.items()}
