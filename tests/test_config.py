"""Test config parsera (CSV → frozenset)."""

from __future__ import annotations

import pytest


def test_admin_chat_ids_csv_string(monkeypatch):
    """ADMIN_CHAT_IDS=6908566796 → frozenset({6908566796})"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "6908566796")
    from app.config import Settings
    s = Settings(_env_file=None)
    assert 6908566796 in s.admin_user_ids


def test_admin_chat_ids_multiple(monkeypatch):
    """ADMIN_CHAT_IDS=id1,id2,id3"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "111,222,333")
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.admin_user_ids == frozenset({111, 222, 333})


def test_admin_chat_ids_empty(monkeypatch):
    """ADMIN_CHAT_IDS pusty → empty frozenset"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "")
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.admin_user_ids == frozenset()


def test_admin_chat_ids_with_spaces(monkeypatch):
    """ADMIN_CHAT_IDS='111, 222 , 333' → trim ok"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("ADMIN_CHAT_IDS", "111, 222 , 333")
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.admin_user_ids == frozenset({111, 222, 333})


def test_token_required(monkeypatch):
    """Bez TELEGRAM_BOT_TOKEN config się wywali."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # Usuń .env żeby pydantic-settings go nie znalazł (test isolation)
    from app.config import Settings
    with pytest.raises(Exception):
        # Bez env_file fallback i bez env var
        Settings(_env_file=None)
