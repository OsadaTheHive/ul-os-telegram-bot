"""Smoke testy idempotency cache."""

from __future__ import annotations

import time

from app.idempotency import IdempotencyCache, telegram_file_key


def test_first_check_returns_true():
    """New key -> True (proceed)."""
    c = IdempotencyCache(max_size=10)
    assert c.check_and_mark("key1") is True


def test_duplicate_check_returns_false():
    """Same key second time -> False (duplicate)."""
    c = IdempotencyCache(max_size=10)
    c.check_and_mark("key1")
    assert c.check_and_mark("key1") is False


def test_different_keys_independent():
    c = IdempotencyCache(max_size=10)
    assert c.check_and_mark("a") is True
    assert c.check_and_mark("b") is True
    assert c.check_and_mark("a") is False  # duplicate
    assert c.check_and_mark("b") is False  # duplicate


def test_lru_eviction():
    """Max size exceeded -> oldest removed."""
    c = IdempotencyCache(max_size=3)
    c.check_and_mark("a")
    c.check_and_mark("b")
    c.check_and_mark("c")
    c.check_and_mark("d")  # evicts "a"
    assert c.check_and_mark("a") is True  # treated as new


def test_ttl_expiry():
    """Old entries past TTL are cleaned."""
    c = IdempotencyCache(max_size=10, ttl_seconds=0.1)
    c.check_and_mark("key1")
    time.sleep(0.15)
    # After TTL, key1 should be cleaned and treated as new
    assert c.check_and_mark("key1") is True


def test_telegram_file_key_deterministic():
    """Same file_id + size → same key."""
    k1 = telegram_file_key("AgABAFile1", 1024)
    k2 = telegram_file_key("AgABAFile1", 1024)
    assert k1 == k2


def test_telegram_file_key_different_files():
    """Different files → different keys."""
    k1 = telegram_file_key("AgABAFile1", 1024)
    k2 = telegram_file_key("AgABAFile2", 1024)
    assert k1 != k2


def test_telegram_file_key_handles_none_size():
    """Some Telegram updates don't include file_size."""
    k = telegram_file_key("AgABAFile1", None)
    assert isinstance(k, str)
    assert len(k) == 16  # truncated sha256


def test_stats():
    c = IdempotencyCache(max_size=100, ttl_seconds=3600)
    c.check_and_mark("a")
    c.check_and_mark("b")
    s = c.stats()
    assert s["size"] == 2
    assert s["max_size"] == 100
    assert s["ttl_seconds"] == 3600
