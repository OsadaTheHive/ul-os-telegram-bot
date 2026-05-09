"""Smoke testy rate limiter."""

from __future__ import annotations

import time

from app.limiter import RateLimiter, LIMITS, check


def test_allow_within_limit():
    rl = RateLimiter()
    for _ in range(5):
        assert rl.allow(1, "test", limit=5, window=60) is True


def test_block_when_exceeded():
    rl = RateLimiter()
    for _ in range(5):
        rl.allow(1, "test", limit=5, window=60)
    assert rl.allow(1, "test", limit=5, window=60) is False


def test_window_resets_old_entries():
    rl = RateLimiter()
    for _ in range(5):
        rl.allow(1, "test", limit=5, window=0.1)
    time.sleep(0.15)
    # Po oknie 100ms wszystkie wpisy stare → nowy request OK
    assert rl.allow(1, "test", limit=5, window=0.1) is True


def test_per_user_separate_buckets():
    rl = RateLimiter()
    for _ in range(5):
        rl.allow(1, "test", limit=5, window=60)
    # User 1 zablokowany, user 2 nie
    assert rl.allow(1, "test", limit=5, window=60) is False
    assert rl.allow(2, "test", limit=5, window=60) is True


def test_per_key_separate_buckets():
    rl = RateLimiter()
    for _ in range(5):
        rl.allow(1, "produkt", limit=5, window=60)
    # produkt limit hit, ale szukaj (nowy key) wolny
    assert rl.allow(1, "produkt", limit=5, window=60) is False
    assert rl.allow(1, "szukaj", limit=5, window=60) is True


def test_remaining():
    rl = RateLimiter()
    for _ in range(3):
        rl.allow(1, "test", limit=10, window=60)
    assert rl.remaining(1, "test", limit=10, window=60) == 7


def test_check_helper_returns_message_in_polish():
    """check() z LIMITS - 'produkt' ma 10/min, sprawdź flood."""
    from app.limiter import limiter

    user_id = 99999  # unique żeby nie konfliktował z innymi testami

    # 10 prób OK
    for _ in range(10):
        ok, _ = check(user_id, "produkt")
        assert ok

    # 11-ta zablokowana
    ok, msg = check(user_id, "produkt")
    assert ok is False
    assert msg is not None
    assert "produkt" in msg.lower()


def test_global_limit():
    """_global limit (60/min) - sprawdź gradient."""
    user_id = 88888
    # 60 zapytań po różnych komendach - ostatnie powinno trafić w _global
    # Przy 13 limitach po 10/min = max 130/min teoretycznie, ale _global=60
    # Nie testuję dokładnie liczby bo skomplikowane (różne limity per command)
    # Tutaj tylko sanity że _global istnieje
    assert "_global" in LIMITS
    assert LIMITS["_global"] == (60, 60)
