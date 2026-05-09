"""Smoke testy circuit breaker."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.breaker import CircuitBreaker, CircuitBreakerError, CircuitState


async def _failing():
    raise ValueError("fail")


async def _success():
    return "ok"


def test_starts_closed():
    cb = CircuitBreaker("test")
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_success_keeps_closed():
    cb = CircuitBreaker("test")
    for _ in range(5):
        result = await cb.call_async(_success)
        assert result == "ok"
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_opens_after_threshold_failures():
    cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=10)
    for _ in range(3):
        with pytest.raises(ValueError):
            await cb.call_async(_failing)
    assert cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_open_rejects_immediately():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=10)
    for _ in range(2):
        with pytest.raises(ValueError):
            await cb.call_async(_failing)

    # Now circuit is OPEN - should reject without calling
    with pytest.raises(CircuitBreakerError):
        await cb.call_async(_success)


@pytest.mark.asyncio
async def test_half_open_after_recovery_timeout():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
    for _ in range(2):
        with pytest.raises(ValueError):
            await cb.call_async(_failing)
    assert cb.state == CircuitState.OPEN

    # Wait for recovery
    await asyncio.sleep(0.15)

    # Next call should be HALF_OPEN test
    result = await cb.call_async(_success)
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED  # recovered


@pytest.mark.asyncio
async def test_half_open_failure_returns_to_open():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
    for _ in range(2):
        with pytest.raises(ValueError):
            await cb.call_async(_failing)
    await asyncio.sleep(0.15)

    # HALF_OPEN test fails -> back to OPEN
    with pytest.raises(ValueError):
        await cb.call_async(_failing)
    assert cb.state == CircuitState.OPEN


def test_stats_returns_state_info():
    cb = CircuitBreaker("test", failure_threshold=3)
    stats = cb.stats()
    assert stats["name"] == "test"
    assert stats["state"] == "closed"
    assert stats["failures"] == 0
    assert stats["threshold"] == 3
    assert stats["seconds_until_half_open"] is None


@pytest.mark.asyncio
async def test_stats_after_open():
    cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=60)
    for _ in range(2):
        try:
            await cb.call_async(_failing)
        except ValueError:
            pass
    stats = cb.stats()
    assert stats["state"] == "open"
    assert stats["failures"] == 2
    assert stats["seconds_until_half_open"] is not None
    assert stats["seconds_until_half_open"] <= 60


def test_global_breakers_exist():
    """Predefiniowane breakers dla zewnetrznych serwisow."""
    from app.breaker import breakers, get
    for name in ("directus", "mcp", "anthropic"):
        assert name in breakers
        assert get(name).name == name
