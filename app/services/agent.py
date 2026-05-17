"""
/claude agent engine.

Flow (one user turn):
  1. Load or create session (per chat_id), append user message.
  2. Fetch MCP tools/list (cached ~5 min), translate to Anthropic input_schema.
  3. Call Anthropic Messages API with system prompt + history + tools + thinking.
  4. For each tool_use in response:
       - Tier 1 block? -> synthesize tool_result with error, persist, continue loop.
       - Approval required? -> persist session as `awaiting_approval`, stop loop,
         tell Telegram handler to ask Hubert. Engine returns "needs_approval".
       - Otherwise: dispatch to MCP via MCPClient.call_tool(), append tool_result.
       - Notify progress_cb after each tool with status string.
  5. Loop until stop_reason=end_turn or max_iterations.
  6. Persist session. Return AgentTurnResult.

Progress callback contract:
  await progress_cb(emoji, message)   # bot edits Telegram message; engine throttles internally

Approval continuation:
  When user says /yes — handler calls agent.continue_with_approval(session, decision="yes").
  Decision options: "yes" (proceed), "no" (cancel tool, agent gets refusal),
  "edit:<text>" (cancel + inject new user message).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import settings
from . import agent_session
from .agent_prompts import SYSTEM_PROMPT, is_tier1_block, needs_approval
from .mcp_client import MCPError, extract_text_content, get_client

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Cena per 1M tokens (2026-05, Anthropic Sonnet 4 standard)
_PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-6-20250929": (3.0, 15.0),
    "claude-opus-4-5": (15.0, 75.0),
}

# Tool list cache shared per-process (refreshed every TOOLS_TTL_S seconds)
_tools_cache: dict[str, Any] = {"ts": 0.0, "tools": []}
TOOLS_TTL_S = 300

# Progress callback type
ProgressCb = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentTurnResult:
    status: str  # "completed" | "needs_approval" | "error" | "paused"
    text: str = ""
    error: str | None = None
    pending_approval: dict[str, Any] | None = None
    iterations: int = 0
    tools_used: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


# ─── MCP tools discovery & translation ─────────────────────────────────────────

async def _fetch_tools_anthropic_format() -> list[dict[str, Any]]:
    """List MCP tools and convert to Anthropic input_schema format (cached)."""
    now = time.time()
    if _tools_cache["tools"] and (now - _tools_cache["ts"]) < TOOLS_TTL_S:
        return _tools_cache["tools"]

    mcp = get_client()
    if mcp is None:
        log.warning("MCP client not configured, agent will have ZERO tools")
        return []

    try:
        raw = await mcp.list_tools()
    except MCPError as e:
        log.warning("MCP tools/list failed: %s", e)
        return _tools_cache["tools"] or []

    anth_tools: list[dict[str, Any]] = []
    for t in raw:
        name = t.get("name")
        if not name:
            continue
        desc = t.get("description") or ""
        # MCP exposes JSON Schema in `inputSchema`. Anthropic expects `input_schema`.
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        anth_tools.append({
            "name": name,
            "description": desc[:1024],  # Anthropic 1024 char cap for descriptions
            "input_schema": schema,
        })
    _tools_cache["tools"] = anth_tools
    _tools_cache["ts"] = now
    log.info("Cached %d MCP tools for agent", len(anth_tools))
    return anth_tools


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_per_m, out_per_m = _PRICING.get(model, (3.0, 15.0))
    return (in_tok * in_per_m + out_tok * out_per_m) / 1_000_000


# ─── Progress throttling wrapper ───────────────────────────────────────────────

class _ProgressThrottle:
    """Drops progress updates faster than MIN_INTERVAL apart. Keeps the latest."""
    MIN_INTERVAL = 1.2  # seconds (Telegram rate limit ~1 msg/sec per chat)

    def __init__(self, cb: ProgressCb | None):
        self.cb = cb
        self._last_ts = 0.0
        self._task: asyncio.Task | None = None
        self._pending: tuple[str, str] | None = None

    async def emit(self, emoji: str, msg: str) -> None:
        if self.cb is None:
            return
        now = time.monotonic()
        if now - self._last_ts >= self.MIN_INTERVAL:
            self._last_ts = now
            try:
                await self.cb(emoji, msg)
            except Exception as e:
                log.debug("progress_cb threw: %s", e)
        else:
            # Schedule a flush after the cooldown so the user sees the latest state
            self._pending = (emoji, msg)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.MIN_INTERVAL)
        if self._pending is None or self.cb is None:
            return
        emoji, msg = self._pending
        self._pending = None
        self._last_ts = time.monotonic()
        try:
            await self.cb(emoji, msg)
        except Exception as e:
            log.debug("progress_cb (delayed) threw: %s", e)


# ─── Anthropic call ────────────────────────────────────────────────────────────

async def _anthropic_call(
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    api_key: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": settings.anthropic_agent_max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    if settings.anthropic_agent_thinking_budget > 0:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": settings.anthropic_agent_thinking_budget,
        }

    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Anthropic HTTP {r.status_code}: {r.text[:500]}")
        return r.json()


# ─── Tool dispatch via MCP ─────────────────────────────────────────────────────

# Retry policy for transient MCP failures (5xx, connection error, timeout).
# Hard errors (MCPError from server-side error response, 4xx) do NOT retry — they
# usually indicate bad input which would just fail again.
_MCP_RETRY_BACKOFF_S = (1.0, 3.0, 10.0)  # 3 attempts total: immediate, +1s, +3s, +10s


def _is_transient_http_error(exc: BaseException) -> bool:
    """Returns True if the exception is worth retrying."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600:
        return True
    # MCPError may wrap upstream 5xx — heuristic on message
    if isinstance(exc, MCPError):
        msg = str(exc).lower()
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg or "timeout" in msg:
            return True
    return False


async def _dispatch_mcp_tool(name: str, arguments: dict[str, Any]) -> str:
    """
    Call MCP tool with retry on transient errors. Returns text content (JSON-ish)
    capped to ~8k chars.

    Retry policy: up to 3 attempts on 5xx / connection / timeout errors with
    backoff 1s, 3s, 10s. Permanent errors (4xx, bad input) fail immediately.
    """
    mcp = get_client()
    if mcp is None:
        return json.dumps({"error": "MCP client not configured"})

    last_exc: BaseException | None = None
    for attempt, backoff in enumerate([0.0, *_MCP_RETRY_BACKOFF_S[:-1]]):
        if backoff > 0:
            log.info("MCP %s retry %d after %.1fs (last: %s)", name, attempt, backoff, last_exc)
            await asyncio.sleep(backoff)
        try:
            result = await mcp.call_tool(name, arguments)
            text = extract_text_content(result) or json.dumps(result, ensure_ascii=False, default=str)
            return text[:8000]
        except (MCPError, httpx.HTTPError) as e:
            last_exc = e
            if not _is_transient_http_error(e):
                # Hard error — don't retry
                if isinstance(e, MCPError):
                    return json.dumps({"error": f"MCP error: {e}"})
                return json.dumps({"error": f"HTTP error: {e}"})
            # transient → loop continues
        except Exception as e:  # noqa: BLE001 - catch-all for tool dispatch safety
            log.exception("MCP tool %s dispatch failed (attempt %d, non-retriable)", name, attempt + 1)
            return json.dumps({"error": f"dispatch failed: {e}"})

    # All retries exhausted
    log.warning("MCP %s exhausted retries (last: %s)", name, last_exc)
    return json.dumps({
        "error": f"MCP unavailable after {len(_MCP_RETRY_BACKOFF_S)} retries",
        "last_error": str(last_exc)[:200] if last_exc else "?",
    })


def _emoji_for_tool(name: str) -> str:
    if name.startswith("vault_"):
        return "🔍"
    if name.startswith("github_"):
        return "🐙"
    if name.startswith("e2b_"):
        return "📦"
    if name.startswith("coolify_"):
        return "🚀"
    if name.startswith("directus_"):
        return "🗄️"
    if name.startswith("gmail_"):
        return "✉️"
    if name.startswith("drive_") or name.startswith("sheets_"):
        return "📁"
    return "🔧"


def _short_tool_label(name: str, args: dict[str, Any]) -> str:
    """1-line human label for progress streaming."""
    hint = ""
    if name in ("vault_search", "vault_read", "directus_query"):
        hint = args.get("query") or args.get("path") or args.get("collection") or ""
    elif name.startswith("github_"):
        hint = args.get("repo") or args.get("branch") or ""
    elif name.startswith("coolify_"):
        hint = args.get("uuid") or args.get("name") or ""
    elif name.startswith("e2b_"):
        hint = args.get("sandbox_id") or args.get("template_id") or args.get("path") or ""
    elif name.startswith("gmail_"):
        hint = args.get("query") or args.get("to") or ""
    if hint:
        hint = f" {str(hint)[:40]}"
    return f"{name}{hint}"


# ─── Main turn driver ──────────────────────────────────────────────────────────

async def run_turn(
    *,
    session: agent_session.AgentSession,
    user_text: str | None,
    progress_cb: ProgressCb | None = None,
) -> AgentTurnResult:
    """
    Run one user turn through the agent loop.

    If user_text is provided, append it to history before calling the model.
    Returns a result; caller persists session.
    """
    if not settings.anthropic_api_key:
        return AgentTurnResult(status="error", error="ANTHROPIC_API_KEY nieskonfigurowany")

    api_key = settings.anthropic_api_key
    model = session.model or settings.anthropic_agent_model

    if user_text:
        session.history.append({"role": "user", "content": user_text})

    tools = await _fetch_tools_anthropic_format()
    throttle = _ProgressThrottle(progress_cb)
    tools_used: list[str] = []
    total_in = 0
    total_out = 0
    final_text_parts: list[str] = []

    max_iter = settings.anthropic_agent_max_iterations
    for iteration in range(max_iter):
        try:
            data = await _anthropic_call(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                messages=session.history,
                tools=tools,
                api_key=api_key,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Anthropic call failed")
            return AgentTurnResult(status="error", error=str(e), iterations=iteration)

        usage = data.get("usage", {}) or {}
        total_in += int(usage.get("input_tokens", 0))
        total_out += int(usage.get("output_tokens", 0))
        session.tokens_in += int(usage.get("input_tokens", 0))
        session.tokens_out += int(usage.get("output_tokens", 0))

        content = data.get("content", []) or []
        stop_reason = data.get("stop_reason", "")

        text_blocks: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                text_blocks.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_uses.append(block)

        # Persist assistant message verbatim (Anthropic requires entire content array, incl. thinking blocks)
        session.history.append({"role": "assistant", "content": content})

        # Stream visible text
        joined_text = "\n".join(t for t in text_blocks if t.strip())
        if joined_text:
            final_text_parts.append(joined_text)
            await throttle.emit("💬", joined_text[:200])

        if stop_reason != "tool_use" or not tool_uses:
            break

        # Process tool_uses
        tool_results: list[dict[str, Any]] = []
        approval_block: dict[str, Any] | None = None

        for tu in tool_uses:
            tool_id = tu.get("id", "")
            name = tu.get("name", "")
            args = tu.get("input", {}) or {}
            tools_used.append(name)

            # Tier 1 hard block
            blocked, reason = is_tier1_block(name, args)
            if blocked:
                err_payload = json.dumps({"error": "tier1_block", "reason": reason})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": err_payload,
                    "is_error": True,
                })
                session.tool_calls.append({
                    "name": name, "ts": time.time(), "ok": False, "reason": "tier1_block",
                })
                await throttle.emit("🚫", f"{name}: Tier 1 BLOCK")
                continue

            # Approval gate
            need, why = needs_approval(name, args)
            if need:
                approval_block = {
                    "tool_name": name,
                    "tool_use_id": tool_id,
                    "tool_input": args,
                    "reason": why,
                    "requested_at": agent_session._iso(),
                }
                # We stop here; we will NOT include this tool's result in history.
                # When user resumes, agent.continue_with_approval handles it.
                break

            await throttle.emit(_emoji_for_tool(name), _short_tool_label(name, args))
            t0 = time.time()
            result_text = await _dispatch_mcp_tool(name, args)
            ok = '"error"' not in result_text[:50]
            session.tool_calls.append({
                "name": name, "ts": t0, "ok": ok,
                "duration_ms": int((time.time() - t0) * 1000),
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result_text,
            })

        if approval_block:
            # Roll back the assistant message? NO — Anthropic needs continuity.
            # We persist the assistant turn but mark session awaiting_approval.
            # On /yes: dispatch tool, append tool_result for THIS tool_use_id only, continue loop.
            # On /no: dispatch a synthetic tool_result {error: "denied"}, continue.
            session.pending_approval = approval_block
            session.status = "awaiting_approval"
            session.cost_usd += _estimate_cost(model, total_in, total_out)
            return AgentTurnResult(
                status="needs_approval",
                pending_approval=approval_block,
                iterations=iteration + 1,
                tools_used=tools_used,
                tokens_in=total_in,
                tokens_out=total_out,
                cost_usd=_estimate_cost(model, total_in, total_out),
                text="\n".join(final_text_parts),
            )

        # Append tool_results as user turn for next iteration
        if tool_results:
            session.history.append({"role": "user", "content": tool_results})
            continue

        # No tools dispatched and stop_reason wasn't tool_use? Treat as end
        break

    final_text = "\n".join(t for t in final_text_parts if t.strip()) or "(brak treści)"
    session.status = "active"  # ready for next user message
    session.pending_approval = None
    session.cost_usd += _estimate_cost(model, total_in, total_out)
    return AgentTurnResult(
        status="completed",
        text=final_text,
        iterations=iteration + 1,
        tools_used=tools_used,
        tokens_in=total_in,
        tokens_out=total_out,
        cost_usd=_estimate_cost(model, total_in, total_out),
    )


async def continue_with_approval(
    session: agent_session.AgentSession,
    decision: str,
    *,
    progress_cb: ProgressCb | None = None,
) -> AgentTurnResult:
    """
    Continue agent loop after /yes /no /edit:<text>.

    Anthropic protocol requirement: the most recent assistant turn contains a tool_use
    block whose id is in session.pending_approval. We must reply with a user turn that
    starts with a tool_result for THAT id. After we satisfy that, we can include
    additional user text (edit:<text>) to inject a new directive.
    """
    if not session.pending_approval:
        return AgentTurnResult(status="error", error="Sesja nie czeka na zgodę.")

    pending = session.pending_approval
    tool_id = pending["tool_use_id"]
    name = pending["tool_name"]
    args = pending.get("tool_input", {}) or {}

    extra_user_text: str | None = None
    if decision == "yes":
        # Dispatch the tool as approved
        throttle = _ProgressThrottle(progress_cb)
        await throttle.emit(_emoji_for_tool(name), f"{name} (approved)")
        result_text = await _dispatch_mcp_tool(name, args)
        ok = '"error"' not in result_text[:50]
        session.tool_calls.append({
            "name": name, "ts": time.time(), "ok": ok, "approved": True,
        })
        tool_result_payload: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": result_text,
        }
    elif decision == "no" or decision.startswith("no:"):
        denial_note = decision.split(":", 1)[1].strip() if decision.startswith("no:") else ""
        denied_text = json.dumps({
            "error": "user_denied",
            "reason": denial_note or "Hubert powiedział /no — nie wykonuj tej akcji.",
        })
        session.tool_calls.append({"name": name, "ts": time.time(), "ok": False, "denied": True})
        tool_result_payload = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": denied_text,
            "is_error": True,
        }
    elif decision.startswith("edit:"):
        edit_text = decision[len("edit:"):].strip()
        denied_text = json.dumps({
            "error": "user_redirected",
            "reason": "Hubert użył /edit — patrz nowa instrukcja w kolejnej wiadomości.",
        })
        session.tool_calls.append({"name": name, "ts": time.time(), "ok": False, "edited": True})
        tool_result_payload = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": denied_text,
            "is_error": True,
        }
        extra_user_text = edit_text
    else:
        return AgentTurnResult(status="error", error=f"Nieznana decyzja: {decision}")

    # The user turn must START with the tool_result. Then optionally append edit text.
    user_content: list[dict[str, Any]] = [tool_result_payload]
    if extra_user_text:
        user_content.append({"type": "text", "text": extra_user_text})
    session.history.append({"role": "user", "content": user_content})
    session.pending_approval = None
    session.status = "active"

    # Now resume normal loop (no new user_text — already appended above)
    return await run_turn(session=session, user_text=None, progress_cb=progress_cb)


# ─── Summarization on huge sessions ────────────────────────────────────────────

async def maybe_summarize(session: agent_session.AgentSession) -> bool:
    """
    If session total tokens exceed threshold, replace first half of history with a
    short summary. Returns True if summarized.
    """
    threshold = settings.anthropic_agent_summary_threshold
    if (session.tokens_in + session.tokens_out) < threshold:
        return False
    if len(session.history) < 10:
        return False
    if not settings.anthropic_api_key:
        return False

    half = len(session.history) // 2
    # Compose summary prompt from first half
    head = session.history[:half]
    summary_payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1500,
        "system": "Podsumuj poniższą konwersację agent-user w 5-10 punktach po polsku. "
                  "Zachowaj: co już zostało zrobione, jakie pliki zmienione, jakie tool_use, "
                  "jakie decyzje. Nie dodawaj komentarzy własnych.",
        "messages": [{"role": "user", "content": json.dumps(head, default=str, ensure_ascii=False)[:120000]}],
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                json=summary_payload,
            )
            if r.status_code != 200:
                log.warning("summarize: anthropic HTTP %s", r.status_code)
                return False
            data = r.json()
            blocks = data.get("content", []) or []
            summary_text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except Exception as e:  # noqa: BLE001
        log.warning("summarize call failed: %s", e)
        return False

    if not summary_text.strip():
        return False

    new_history = [
        {"role": "user", "content": f"(SUMARYZACJA wcześniejszej rozmowy)\n\n{summary_text}"},
        {"role": "assistant", "content": "Rozumiem, kontynuuję na podstawie tego podsumowania."},
    ]
    session.history = new_history + session.history[half:]
    log.info("Session %s summarized (history %d -> %d turns)", session.id, half * 2, len(session.history))
    return True
