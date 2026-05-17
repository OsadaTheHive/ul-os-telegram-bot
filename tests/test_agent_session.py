"""
Unit tests for agent_session — round-trip save/load via LOCAL fallback.

S3 path is not tested (would need moto / real S3) — covered manually in smoke test.
Local fallback is tested by clearing S3 env vars in the settings before each test.
"""

from __future__ import annotations

import shutil

import pytest

from app.services import agent_session


@pytest.fixture(autouse=True)
def _force_local_backend(monkeypatch, tmp_path):
    """Force local backend by clearing S3 creds; redirect LOCAL_DIR to tmp_path."""
    monkeypatch.setattr(agent_session.settings, "s3_endpoint", "", raising=False)
    monkeypatch.setattr(agent_session.settings, "s3_access_key_id", "", raising=False)
    monkeypatch.setattr(agent_session.settings, "s3_secret_access_key", "", raising=False)
    monkeypatch.setattr(agent_session, "LOCAL_DIR", tmp_path / "claude-sessions")
    yield
    if (tmp_path / "claude-sessions").exists():
        shutil.rmtree(tmp_path / "claude-sessions", ignore_errors=True)


def test_backend_label_is_local():
    assert agent_session.backend_label() == "local"


def test_new_session_defaults():
    sess = agent_session.new_session(
        chat_id=111, user_id=222, model="claude-sonnet-4-test",
        title_seed="zaktualizuj /prototypy",
    )
    assert sess.chat_id == 111
    assert sess.user_id == 222
    assert sess.status == "active"
    assert sess.model == "claude-sonnet-4-test"
    assert sess.title == "zaktualizuj /prototypy"
    assert sess.history == []
    assert sess.cost_usd == 0.0
    assert sess.pending_approval is None


def test_title_truncated_to_60_chars():
    long_seed = "x" * 200
    sess = agent_session.new_session(chat_id=1, user_id=1, model="m", title_seed=long_seed)
    assert len(sess.title) == 60


def test_title_takes_first_line_only():
    sess = agent_session.new_session(
        chat_id=1, user_id=1, model="m", title_seed="pierwsza linia\ndruga linia"
    )
    assert sess.title == "pierwsza linia"


def test_save_then_load_roundtrip():
    sess = agent_session.new_session(chat_id=999, user_id=1, model="m", title_seed="x")
    sess.history.append({"role": "user", "content": "test"})
    sess.tokens_in = 100
    sess.cost_usd = 0.0042
    agent_session.save(sess)

    loaded = agent_session.load(999, sess.id)
    assert loaded is not None
    assert loaded.id == sess.id
    assert loaded.chat_id == 999
    assert loaded.history == [{"role": "user", "content": "test"}]
    assert loaded.tokens_in == 100
    assert loaded.cost_usd == pytest.approx(0.0042)


def test_load_active_returns_active_session():
    sess = agent_session.new_session(chat_id=42, user_id=1, model="m", title_seed="x")
    agent_session.save(sess)
    active = agent_session.load_active(42)
    assert active is not None
    assert active.id == sess.id


def test_load_active_skips_completed():
    sess = agent_session.new_session(chat_id=43, user_id=1, model="m", title_seed="x")
    sess.status = "completed"
    agent_session.save(sess)
    # completed status does NOT write _active pointer (per save() logic)
    assert agent_session.load_active(43) is None


def test_load_active_returns_awaiting_approval():
    sess = agent_session.new_session(chat_id=44, user_id=1, model="m", title_seed="x")
    sess.status = "awaiting_approval"
    sess.pending_approval = {
        "tool_name": "github_pr_merge",
        "tool_use_id": "toolu_abc",
        "tool_input": {"pr_number": 1},
        "reason": "test",
        "requested_at": agent_session._iso(),
    }
    agent_session.save(sess)
    loaded = agent_session.load_active(44)
    assert loaded is not None
    assert loaded.status == "awaiting_approval"
    assert loaded.pending_approval["tool_name"] == "github_pr_merge"


def test_clear_active_pointer():
    sess = agent_session.new_session(chat_id=45, user_id=1, model="m", title_seed="x")
    agent_session.save(sess)
    assert agent_session.load_active(45) is not None
    agent_session.clear_active_pointer(45)
    assert agent_session.load_active(45) is None
    # Underlying session file still exists for /claude_history
    assert agent_session.load(45, sess.id) is not None


def test_list_chat_sessions_returns_newest_first():
    for i in range(3):
        s = agent_session.new_session(chat_id=50, user_id=1, model="m", title_seed=f"task {i}")
        agent_session.save(s)
    listing = agent_session.list_chat_sessions(50, limit=10)
    assert len(listing) == 3
    # created_at strings sort lexicographically (ISO-8601 UTC)
    assert listing == sorted(listing, key=lambda x: x["created_at"], reverse=True)


def test_load_nonexistent_returns_none():
    assert agent_session.load(99999, "no-such-id") is None
    assert agent_session.load_active(99999) is None
