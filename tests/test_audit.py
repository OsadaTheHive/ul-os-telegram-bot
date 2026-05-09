"""Smoke testy audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_audit(monkeypatch, tmp_path):
    """Replace AUDIT_FILE z tymczasowym, by nie spamować prod logu."""
    from app import audit
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_FILE", audit_file)
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    return audit_file


def test_write_creates_jsonl(tmp_audit):
    from app.audit import write
    write(user_id=123, username="alice", action="produkt", args="Deye 5kWh", result="ok")
    assert tmp_audit.exists()
    line = tmp_audit.read_text(encoding="utf-8").strip()
    event = json.loads(line)
    assert event["user_id"] == 123
    assert event["username"] == "alice"
    assert event["action"] == "produkt"
    assert event["args"] == "Deye 5kWh"
    assert event["result"] == "ok"
    assert "iso" in event
    assert "ts" in event


def test_write_with_error(tmp_audit):
    from app.audit import write
    write(
        user_id=42,
        username="hacker",
        action="document",
        result="error",
        error="anthropic_quota_exceeded",
        extra={"file_size_mb": 12.5},
    )
    line = tmp_audit.read_text().strip()
    event = json.loads(line)
    assert event["result"] == "error"
    assert event["error"] == "anthropic_quota_exceeded"
    assert event["extra"]["file_size_mb"] == 12.5


def test_write_handles_missing_user(tmp_audit):
    """System events bez user_id."""
    from app.audit import write
    write(user_id=None, username=None, action="system_startup", result="ok")
    event = json.loads(tmp_audit.read_text().strip())
    assert event["user_id"] is None
    assert event["username"] is None


def test_multiple_writes_appendsa(tmp_audit):
    from app.audit import write
    for i in range(5):
        write(user_id=i, username=f"u{i}", action="test", result="ok")
    lines = tmp_audit.read_text().strip().split("\n")
    assert len(lines) == 5


def test_stats(tmp_audit):
    from app.audit import write, stats
    for i in range(3):
        write(user_id=i, username=f"u{i}", action="test", result="ok")
    s = stats()
    assert s["events"] == 3
    assert s["size_bytes"] > 0


def test_write_does_not_crash_on_io_error(tmp_audit, monkeypatch):
    """Audit nigdy nie ma crashować bota - tylko warning log."""
    from app import audit
    monkeypatch.setattr(audit, "AUDIT_FILE", "/nonexistent/dir/file.jsonl")
    # Powinno NIE rzucić wyjątku
    audit.write(user_id=1, username="u", action="test", result="ok")
