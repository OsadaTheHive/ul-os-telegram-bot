"""Smoke testy dla services (usage_stats, dlq)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_audit(monkeypatch, tmp_path):
    """Replace AUDIT_FILE in services.usage_stats."""
    from app.services import usage_stats
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(usage_stats, "AUDIT_FILE", audit_file)
    return audit_file


def test_stats_local_empty_audit(tmp_audit):
    """Pusty audit.jsonl → stats zwraca 0 events."""
    from app.services.usage_stats import stats_local
    s = stats_local(window_hours=24)
    assert s["total_events"] == 0
    assert s["file_ingests"] == 0
    assert s["est_cost_usd"] == 0


def test_stats_local_with_events(tmp_audit):
    """Audit ze świeżymi events → liczy."""
    from app.services.usage_stats import stats_local
    now = time.time()
    events = [
        {"ts": now - 100, "username": "alice", "action": "produkt", "result": "ok"},
        {"ts": now - 50, "username": "alice", "action": "document", "result": "ok"},
        {"ts": now - 30, "username": "bob", "action": "rate_limit_hit", "result": "rate_limited"},
        {"ts": now - 10, "username": "alice", "action": "photo", "result": "ok"},
    ]
    with open(tmp_audit, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    s = stats_local(window_hours=24)
    assert s["total_events"] == 4
    assert s["by_action"]["produkt"] == 1
    assert s["by_action"]["document"] == 1
    assert s["by_action"]["photo"] == 1
    assert s["rate_limited"] == 1
    assert s["file_ingests"] == 2  # document + photo
    assert s["est_cost_usd"] > 0


def test_stats_local_window_filter(tmp_audit):
    """Stare events poza oknem → ignorowane."""
    from app.services.usage_stats import stats_local
    now = time.time()
    events = [
        {"ts": now - 25 * 3600, "action": "old", "result": "ok"},  # 25h temu
        {"ts": now - 10, "action": "fresh", "result": "ok"},
    ]
    with open(tmp_audit, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    s = stats_local(window_hours=24)
    assert s["total_events"] == 1  # tylko fresh
    assert "fresh" in s["by_action"]
    assert "old" not in s["by_action"]


def test_stats_local_handles_corrupted_lines(tmp_audit):
    """Corrupted JSON nie crashuje czytania."""
    from app.services.usage_stats import stats_local
    now = time.time()
    with open(tmp_audit, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "action": "ok", "result": "ok"}) + "\n")
        f.write("CORRUPTED LINE\n")
        f.write(json.dumps({"ts": now, "action": "ok2", "result": "ok"}) + "\n")

    s = stats_local(window_hours=24)
    assert s["total_events"] == 2  # corrupted line skipped


def test_dlq_not_configured():
    """Bez S3 keys → status 'not_configured'."""
    import asyncio
    from app.services import dlq

    async def go():
        return await dlq.list_dlq_items(limit=10)

    result = asyncio.run(go())
    assert result["status"] == "not_configured"
    assert result["total"] == 0
    assert "Worker DLQ" in result["message"]
