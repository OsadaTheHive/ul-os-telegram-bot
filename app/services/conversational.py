"""
Conversational Claude — /ask <pytanie> z dostepem do MCP tools (vault_search, vault_read,
directus_query, recent_changes). Sprint 1.9.

Persistent context per user (in-memory + opcjonalny dump JSON gdy bot restart).
Multi-turn: bot pamieta ostatnie N messages per user_id.

Anthropic SDK z native tool use:
  https://docs.anthropic.com/en/api/messages
  https://docs.anthropic.com/en/docs/build-with-claude/tool-use/overview

Tools wystawione przez MCP server (mcp.bidbee.pl) — wywolywane przez bot przez
JSON-RPC do MCP (services.mcp_client). Claude dostaje JSON schemas + bot je wykonuje.

Cost tracking: per-call estimate na podstawie modelu i tokens.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Per-user context (in-memory). Cap N ostatnich messages żeby nie wybuchnąć kontekstu.
MAX_HISTORY = 20

# Cena na 1M tokenów (2026-05, Anthropic)
_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-5": (15.0, 75.0),
}

# Persistent context file
CONTEXT_FILE = Path(__file__).parent.parent.parent / "logs" / "ask_context.json"


@dataclass
class AskResponse:
    success: bool
    text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None


# MCP tools — schemas Anthropic-compatible (Sprint 1.9 wystaramy najbasoniejszego setu)
MCP_TOOLS = [
    {
        "name": "vault_search",
        "description": "Szukaj plików w HiveLive_Vault (grep-style, full-text). Zwraca listę dopasowań z preview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tekst do wyszukania w Markdown."},
                "limit": {"type": "integer", "description": "Max wyników (default 10).", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "vault_read",
        "description": "Czyta zawartość konkretnego pliku z Vault (Markdown).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Ścieżka relatywna do Vault, np. '50 — BIDBEE/_INBOX/brief.md'."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "directus_query",
        "description": "Zapytanie do Directus knowledge_items / beezzy_products. Zwraca listę rekordów.",
        "input_schema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "enum": ["knowledge_items", "beezzy_products"]},
                "filter_field": {"type": "string", "description": "Pole filtra (np. 'brand', 'type', 'manufacturer')."},
                "filter_value": {"type": "string", "description": "Wartość filtra."},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["collection"],
        },
    },
]


_context_store: dict[int, list[dict[str, Any]]] = {}


def _load_persisted() -> None:
    global _context_store
    if not CONTEXT_FILE.exists():
        return
    try:
        raw = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
        _context_store = {int(k): v for k, v in raw.items() if isinstance(v, list)}
    except Exception as e:
        log.warning("ask context load fail: %s", e)


def _persist() -> None:
    try:
        CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONTEXT_FILE.write_text(json.dumps({str(k): v for k, v in _context_store.items()}), encoding="utf-8")
    except Exception as e:
        log.warning("ask context persist fail: %s", e)


def reset_context(user_id: int) -> None:
    _context_store.pop(user_id, None)
    _persist()


def _get_history(user_id: int) -> list[dict[str, Any]]:
    return _context_store.setdefault(user_id, [])


def _append_history(user_id: int, role: str, content: Any) -> None:
    hist = _get_history(user_id)
    hist.append({"role": role, "content": content})
    # Cap MAX_HISTORY (zostawiaj system + ostatnie N user/assistant)
    if len(hist) > MAX_HISTORY:
        # Usuń najstarsze pary (zachowaj parę user→assistant)
        del hist[: len(hist) - MAX_HISTORY]
    _persist()


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_per_m, out_per_m = _PRICING.get(model, (3.0, 15.0))
    return (in_tok * in_per_m + out_tok * out_per_m) / 1_000_000


async def _call_mcp_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    """
    Wywołuje tool — z fallback hierarchy:
      1. Bezpośrednie wywołanie Directus REST (najbardziej niezawodne)
      2. MCP server fallback (jeśli skonfigurowany)
    Zwraca JSON string z wynikiem (cap 4000 chars dla Anthropic context).
    """
    try:
        if tool_name == "vault_search":
            return await _direct_vault_search(
                query=tool_input.get("query", ""),
                limit=int(tool_input.get("limit", 10)),
            )
        if tool_name == "vault_read":
            return await _direct_vault_read(path=tool_input.get("path", ""))
        if tool_name == "directus_query":
            return await _direct_directus_query(
                collection=tool_input.get("collection", ""),
                filter_field=tool_input.get("filter_field"),
                filter_value=tool_input.get("filter_value"),
                limit=int(tool_input.get("limit", 10)),
            )
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    except Exception as e:
        log.exception("tool %s fail", tool_name)
        return json.dumps({"error": str(e)})


async def _direct_vault_search(query: str, limit: int = 10) -> str:
    """Vault search = Directus knowledge_items full-text na content_text + title."""
    if not query:
        return json.dumps({"error": "query required"})
    if not settings.directus_url or not settings.directus_token:
        return json.dumps({"error": "Directus not configured"})

    limit = max(1, min(limit, 30))
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{settings.directus_url}/items/knowledge_items",
            params={
                "fields": "id,title,vault_path,brand,type,project,date_created,summary",
                "filter[_or][0][title][_icontains]": query,
                "filter[_or][1][content_text][_icontains]": query,
                "filter[_or][2][vault_path][_icontains]": query,
                "filter[_or][3][summary][_icontains]": query,
                "limit": str(limit),
                "sort": "-date_created",
            },
            headers={"Authorization": f"Bearer {settings.directus_token}"},
        )
        if r.status_code != 200:
            return json.dumps({"error": f"Directus HTTP {r.status_code}"})
        items = r.json().get("data", [])

    out = {
        "tool": "vault_search",
        "query": query,
        "matches": len(items),
        "results": [
            {
                "id": it.get("id"),
                "title": it.get("title"),
                "vault_path": it.get("vault_path"),
                "brand": it.get("brand"),
                "type": it.get("type"),
                "project": it.get("project"),
                "summary": (it.get("summary") or "")[:300],
            }
            for it in items
        ],
    }
    return json.dumps(out, ensure_ascii=False)[:4000]


async def _direct_vault_read(path: str) -> str:
    """Vault read = Directus knowledge_items o vault_path = path → zwróć content_text."""
    if not path:
        return json.dumps({"error": "path required"})
    if not settings.directus_url or not settings.directus_token:
        return json.dumps({"error": "Directus not configured"})

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{settings.directus_url}/items/knowledge_items",
            params={
                "fields": "id,title,vault_path,brand,type,content_text,summary",
                "filter[vault_path][_eq]": path,
                "limit": "1",
            },
            headers={"Authorization": f"Bearer {settings.directus_token}"},
        )
        if r.status_code != 200:
            return json.dumps({"error": f"Directus HTTP {r.status_code}"})
        items = r.json().get("data", [])
        if not items:
            return json.dumps({"error": f"Not found: {path}"})
        it = items[0]
        return json.dumps(
            {
                "tool": "vault_read",
                "path": path,
                "title": it.get("title"),
                "brand": it.get("brand"),
                "type": it.get("type"),
                "summary": it.get("summary"),
                "content": (it.get("content_text") or "")[:3500],
            },
            ensure_ascii=False,
        )[:4000]


async def _direct_directus_query(
    collection: str,
    filter_field: Optional[str],
    filter_value: Optional[str],
    limit: int = 10,
) -> str:
    """Generic Directus query — knowledge_items, beezzy_products."""
    if collection not in {"knowledge_items", "beezzy_products"}:
        return json.dumps({"error": f"Collection nie wspierana: {collection}"})
    if not settings.directus_url or not settings.directus_token:
        return json.dumps({"error": "Directus not configured"})

    limit = max(1, min(limit, 30))
    params: dict[str, Any] = {"limit": str(limit), "sort": "-date_created"}

    if collection == "beezzy_products":
        params["fields"] = (
            "id,title,manufacturer,model,category,subcategory,power_w,capacity_kwh,"
            "voltage_v,efficiency_pct,description_short,price_retail_pln,hero_image_url"
        )
        params["filter[is_duplicate][_neq]"] = "true"
    else:
        params["fields"] = "id,title,brand,type,project,vault_path,summary,date_created"

    if filter_field and filter_value:
        # Whitelist safe filter keys żeby user nie zrobił SQL injection przez Claude
        safe_keys = {
            "knowledge_items": {"brand", "type", "project", "title", "vault_path"},
            "beezzy_products": {"manufacturer", "model", "category", "subcategory", "title"},
        }
        if filter_field in safe_keys.get(collection, set()):
            params[f"filter[{filter_field}][_icontains]"] = filter_value

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{settings.directus_url}/items/{collection}",
            params=params,
            headers={"Authorization": f"Bearer {settings.directus_token}"},
        )
        if r.status_code != 200:
            return json.dumps({"error": f"Directus HTTP {r.status_code}"})
        items = r.json().get("data", [])

    return json.dumps(
        {
            "tool": "directus_query",
            "collection": collection,
            "filter": {"field": filter_field, "value": filter_value} if filter_field else None,
            "count": len(items),
            "items": items,
        },
        ensure_ascii=False,
        default=str,
    )[:4000]


_load_persisted()


async def ask(user_id: int, prompt: str) -> AskResponse:
    """
    Wywoluje Claude z MCP tools. Multi-turn z persistent kontekstem per user.
    """
    if not settings.anthropic_api_key:
        return AskResponse(success=False, error="ANTHROPIC_API_KEY nie skonfigurowane")

    model = settings.anthropic_model or "claude-haiku-4-5"
    history = _get_history(user_id)

    # Append user message
    history_copy = list(history)
    history_copy.append({"role": "user", "content": prompt})

    system_prompt = (
        "Jesteś asystentem HiveLive ecosystem (ekosystem BEEzzy/bidBEE/BEEco/BEEZhub/HiveLive). "
        "Masz dostęp do Vault (HiveLive_Vault repo) przez tools vault_search/vault_read "
        "oraz Directus knowledge_items/beezzy_products przez directus_query.\n\n"
        "Odpowiadasz po polsku, krótko, konkretnie. Cytuj źródła (vault paths lub Directus IDs).\n"
        "Jeśli pytanie wymaga danych z Vault → użyj vault_search najpierw, potem vault_read.\n"
        "Jeśli pytanie o produkty BEEzzy → użyj directus_query collection=beezzy_products."
    )

    total_in = 0
    total_out = 0
    tools_called: list[str] = []
    final_text_parts: list[str] = []

    # Multi-step tool use loop — Claude może wywołać tool, dostać wynik, znowu wywołać itd.
    max_iterations = 8
    for iteration in range(max_iterations):
        payload = {
            "model": model,
            "max_tokens": 4000,
            "system": system_prompt,
            "tools": MCP_TOOLS,
            "messages": history_copy,
        }
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": settings.anthropic_api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if r.status_code != 200:
                    return AskResponse(
                        success=False,
                        error=f"Anthropic HTTP {r.status_code}: {r.text[:300]}",
                    )
                data = r.json()
        except httpx.TimeoutException:
            return AskResponse(success=False, error="Anthropic timeout 180s")
        except Exception as e:
            log.exception("anthropic request error")
            return AskResponse(success=False, error=str(e))

        usage = data.get("usage", {})
        total_in += int(usage.get("input_tokens", 0))
        total_out += int(usage.get("output_tokens", 0))

        content = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        # Zbierz tekst + tool_use blocks
        tool_uses: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "text":
                final_text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_uses.append(block)
                tools_called.append(block.get("name", "?"))

        # Append assistant message do historii
        history_copy.append({"role": "assistant", "content": content})

        if stop_reason == "tool_use" and tool_uses:
            # Wykonaj wszystkie tools i wstaw wyniki jako user message
            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                tool_id = tu.get("id", "")
                tool_name = tu.get("name", "")
                tool_input = tu.get("input", {})
                log.info("ask: calling tool %s with %s", tool_name, list(tool_input.keys()))
                result_str = await _call_mcp_tool(tool_name, tool_input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                })
            history_copy.append({"role": "user", "content": tool_results})
            # Continue loop — Claude dostaje tool results, generuje finalną odpowiedź
            continue

        # stop_reason = "end_turn" lub inne — zakończenie
        break

    final_text = "\n".join(t for t in final_text_parts if t.strip()) or "(brak odpowiedzi tekstowej)"

    # Persist history (z final assistant message)
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": final_text})
    _persist()

    return AskResponse(
        success=True,
        text=final_text,
        tool_calls=tools_called,
        input_tokens=total_in,
        output_tokens=total_out,
        cost_usd=_estimate_cost(model, total_in, total_out),
    )
