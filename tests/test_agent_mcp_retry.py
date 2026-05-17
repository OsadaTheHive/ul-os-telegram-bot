"""
Unit tests for _dispatch_mcp_tool retry/backoff.

Covers:
  - Permanent errors (bad input, 4xx-style MCPError) → fail immediately, no retry
  - Transient HTTP errors (connection error, timeout, 5xx) → retry with backoff
  - All retries exhausted → return final error payload with last_error
  - No MCP client configured → immediate "MCP client not configured" error
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.services import agent
from app.services.mcp_client import MCPError


class _FakeMCP:
    """Stub matching MCPClient.call_tool surface."""

    def __init__(self, side_effects: list):
        # side_effects: list of values to raise (Exceptions) or return (dicts)
        self._effects = list(side_effects)
        self.calls = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        self.calls += 1
        if not self._effects:
            raise RuntimeError("FakeMCP ran out of side_effects")
        eff = self._effects.pop(0)
        if isinstance(eff, BaseException):
            raise eff
        return eff


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip real backoff during tests."""
    async def _instant(_):
        return None
    monkeypatch.setattr(agent.asyncio, "sleep", _instant)


async def _patch_client(monkeypatch, fake: _FakeMCP | None):
    monkeypatch.setattr(agent, "get_client", lambda: fake)


@pytest.mark.asyncio
async def test_no_client_configured_returns_error(monkeypatch):
    await _patch_client(monkeypatch, None)
    result = await agent._dispatch_mcp_tool("vault_search", {"query": "x"})
    assert json.loads(result) == {"error": "MCP client not configured"}


@pytest.mark.asyncio
async def test_happy_path_single_call(monkeypatch):
    fake = _FakeMCP(side_effects=[{"content": [{"type": "text", "text": "OK"}]}])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("vault_search", {"query": "x"})
    assert result == "OK"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_permanent_mcp_error_fails_immediately(monkeypatch):
    # MCPError without 5xx markers = treat as permanent (bad input from agent)
    fake = _FakeMCP(side_effects=[MCPError("MCP error -32602: Invalid params")])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("vault_read", {"path": "/bad"})
    parsed = json.loads(result)
    assert "MCP error" in parsed["error"]
    assert "Invalid params" in parsed["error"]
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_transient_5xx_then_success(monkeypatch):
    transient = MCPError("upstream 503 service unavailable")
    fake = _FakeMCP(side_effects=[
        transient,
        transient,
        {"content": [{"type": "text", "text": "RECOVERED"}]},
    ])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("github_repo_get", {"repo": "x"})
    assert result == "RECOVERED"
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_connection_error_retries_then_succeeds(monkeypatch):
    fake = _FakeMCP(side_effects=[
        httpx.ConnectError("ECONNREFUSED"),
        {"content": [{"type": "text", "text": "OK after retry"}]},
    ])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("coolify_app_get", {"uuid": "x"})
    assert result == "OK after retry"
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_timeout_retries(monkeypatch):
    fake = _FakeMCP(side_effects=[
        httpx.ReadTimeout("read timeout"),
        httpx.ReadTimeout("read timeout"),
        {"content": [{"type": "text", "text": "FINALLY"}]},
    ])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("e2b_run_code", {"sandbox_id": "x", "code": "ls"})
    assert result == "FINALLY"
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_all_retries_exhausted_returns_last_error(monkeypatch):
    transient = httpx.ConnectError("ECONNREFUSED")
    fake = _FakeMCP(side_effects=[transient, transient, transient])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("vault_search", {"query": "x"})
    parsed = json.loads(result)
    assert "MCP unavailable" in parsed["error"]
    assert "ECONNREFUSED" in parsed["last_error"]
    assert fake.calls == 3  # 3 attempts total: immediate + 1s + 3s


@pytest.mark.asyncio
async def test_text_capped_at_8000_chars(monkeypatch):
    huge = "x" * 20_000
    fake = _FakeMCP(side_effects=[{"content": [{"type": "text", "text": huge}]}])
    await _patch_client(monkeypatch, fake)
    result = await agent._dispatch_mcp_tool("vault_read", {"path": "/x"})
    assert len(result) == 8000


@pytest.mark.asyncio
async def test_is_transient_classification():
    assert agent._is_transient_http_error(httpx.ConnectError("x"))
    assert agent._is_transient_http_error(httpx.ReadTimeout("x"))
    assert agent._is_transient_http_error(MCPError("upstream 502 bad gateway"))
    assert agent._is_transient_http_error(MCPError("Service unavailable 503"))
    # MCPError without 5xx-ish wording is treated as permanent
    assert not agent._is_transient_http_error(MCPError("Invalid params -32602"))
    assert not agent._is_transient_http_error(ValueError("not http related"))
