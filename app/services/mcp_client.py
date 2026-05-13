"""
MCP (Model Context Protocol) client - low-level JSON-RPC over Streamable HTTP.

Komunikuje sie z UL OS MCP server (mcp.bidbee.pl) ktory wystawia 5 tools:
  - vault_search    grep-style search po Markdown w HiveLive_Vault
  - vault_read      odczyt pojedynczego pliku
  - recent_changes  lista ostatnich commitow w Vault
  - directus_query  query po kolekcjach Directus
  - vault_write     zapis pliku + auto-commit + push (UWAGA: side-effect!)

MCP transport: Streamable HTTP per spec (Accept: application/json, text/event-stream).
Server zwraca SSE events `event: message\ndata: {jsonrpc:..}`.

Sesja:
  1. POST /mcp z method=initialize -> server zwraca sessionId w mcp-session-id header
  2. POST /mcp z method=notifications/initialized -> server ack
  3. POST /mcp z method=tools/list -> lista tools
  4. POST /mcp z method=tools/call -> wywolanie tool

Per FILAR 4 z planu autonomii (AI Quality) - bot moze szukac w Vault dla Huberta.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class MCPError(Exception):
    """MCP protocol error."""


def _parse_sse_data(text: str) -> dict[str, Any] | None:
    """Parse Server-Sent Event 'data: {json}' line - zwraca pierwszy JSON w stream."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    return None


class MCPClient:
    """Async MCP client - per-call session (initialize + use + close).

    Aktualnie create new session per request (overhead ~200ms initialize).
    Dla bota OK - rzadko bedzie wolany, koszt akceptowalny.
    Dla Worker production - przepisac na persistent session pool.
    """

    def __init__(self, base_url: str, bearer_token: str):
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer_token
        self.endpoint = f"{self.base_url}/mcp"
        self._next_id = 0

    def _get_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        notification: bool = False,
    ) -> tuple[dict | None, str | None]:
        """Send single JSON-RPC request. Returns (parsed_response, session_id)."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        if not notification:
            payload["id"] = self._get_id()

        headers = {
            "Authorization": f"Bearer {self.bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        resp = await client.post(self.endpoint, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()

        # Server zwraca session_id w response header tylko przy initialize
        new_session_id = resp.headers.get("mcp-session-id") or session_id

        if notification:
            return None, new_session_id

        # Streamable HTTP - SSE format
        body = resp.text
        if body.startswith("event:") or "data: " in body:
            parsed = _parse_sse_data(body)
        else:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                raise MCPError(f"Cannot parse MCP response: {body[:200]}")

        if parsed and "error" in parsed:
            err = parsed["error"]
            raise MCPError(f"MCP error {err.get('code', '?')}: {err.get('message', err)}")

        return parsed, new_session_id

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
        """High-level: initialize + call tool + return result.

        Returns content of result.content[] (list of text blocks).
        """
        async with httpx.AsyncClient() as client:
            # 1. Initialize
            init_resp, session_id = await self._send(
                client,
                method="initialize",
                params={
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "ul-os-telegram-bot", "version": "1.0.0"},
                },
            )
            if not session_id:
                # Niektore MCP servers nie wymagaja session_id - try without
                log.debug("No session_id from server - proceeding without")

            # 2. Send notifications/initialized
            await self._send(
                client,
                method="notifications/initialized",
                session_id=session_id,
                notification=True,
            )

            # 3. tools/call
            call_resp, _ = await self._send(
                client,
                method="tools/call",
                params={"name": tool_name, "arguments": arguments or {}},
                session_id=session_id,
            )

            if not call_resp or "result" not in call_resp:
                raise MCPError(f"Unexpected tools/call response: {call_resp}")

            return call_resp["result"]

    async def list_tools(self) -> list[dict]:
        """List dostepnych tools (z opisami)."""
        async with httpx.AsyncClient() as client:
            init_resp, session_id = await self._send(
                client,
                method="initialize",
                params={
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "ul-os-telegram-bot", "version": "1.0.0"},
                },
            )
            await self._send(
                client,
                method="notifications/initialized",
                session_id=session_id,
                notification=True,
            )
            tools_resp, _ = await self._send(
                client,
                method="tools/list",
                session_id=session_id,
            )
            return tools_resp.get("result", {}).get("tools", []) if tools_resp else []


# Singleton (lazy)
_client: MCPClient | None = None


def get_client() -> MCPClient | None:
    global _client
    if _client is not None:
        return _client
    if not settings.mcp_bearer_token:
        return None
    _client = MCPClient(settings.mcp_base_url, settings.mcp_bearer_token)
    return _client


def extract_text_content(result: dict) -> str:
    """MCP tools return result.content[] = [{type:'text', text:'...'}].

    Concat wszystkich text blocks.
    """
    content = result.get("content", [])
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts)


async def mcp_status() -> dict:
    """High-level health check dla /status agregatu.

    Returns:
        {"ok": True, "tools_count": N} jezeli MCP server odpowiada,
        {"ok": False, "error": "..."} w przeciwnym razie.
    """
    client = get_client()
    if client is None:
        return {"ok": False, "error": "MCP client not configured"}
    try:
        tools = await client.list_tools()
        return {"ok": True, "tools_count": len(tools)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
