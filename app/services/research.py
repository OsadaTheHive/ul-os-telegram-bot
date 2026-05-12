"""
Research bot — Sprint 1.7 v2 (Anthropic web_search zamiast Perplexity).

Dlaczego Anthropic web_search:
- Klucz mamy aktywny (wspólny z /ask i Workerem klasyfikatorem)
- Built-in tool — server_tool_use, brak osobnego API
- Cytowanie źródeł natywnie (web_search_tool_result.content[].url)
- Tańsze niż Perplexity Sonar Deep Research ($0.014 vs $0.05+ per query)
- Można łączyć z vault_search (Claude sprawdzi też nasz Vault podczas research)

Flow:
  /research <prompt>
    → Claude haiku-4-5 z tools=[web_search, vault_search]
    → multi-step: search web → search Vault → kompiluje markdown z citations
    → upload markdown do HOS inbox/ → Worker przerobi do Directus + Vault

Backward compat: jeśli PERPLEXITY_API_KEY ustawione, można forsować Perplexity przez
env RESEARCH_PROVIDER=perplexity (default: anthropic).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.hos_uploader import upload_telegram_file, UploadResult

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class ResearchResult:
    success: bool
    markdown: str = ""
    citations: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_count: int = 0
    cost_usd: float = 0.0
    model_used: str = ""
    provider: str = ""
    error: Optional[str] = None


def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] or "research"


def _estimate_cost_anthropic(model: str, in_tok: int, out_tok: int, web_searches: int) -> float:
    """Claude pricing 2026-05 + web_search at $0.01 per query."""
    pricing = {
        "claude-haiku-4-5": (1.0, 5.0),
        "claude-sonnet-4-5": (3.0, 15.0),
        "claude-opus-4-5": (15.0, 75.0),
    }
    in_per_m, out_per_m = pricing.get(model, (1.0, 5.0))
    tokens_cost = (in_tok * in_per_m + out_tok * out_per_m) / 1_000_000
    web_cost = web_searches * 0.01  # $0.01 per web search request
    return tokens_cost + web_cost


SYSTEM_PROMPT = (
    "Jesteś analitykiem rynku energii odnawialnej (PV, BESS, magazyny energii, "
    "stacje ładowania, e-mobility) dla ekosystemu HiveLive (BEEzzy, bidBEE, BEEco, "
    "BEEZhub, HiveLive). Odpowiadasz po polsku, strukturyzowany markdown z nagłówkami.\n\n"
    "Korzystaj z dwóch źródeł:\n"
    "1) web_search — aktualne dane rynkowe, konkurencja, regulacje, technologie\n"
    "2) vault_search — sprawdź czy ekosystem HiveLive ma już dane na ten temat\n"
    "   (kolekcja knowledge_items: nasze raporty, analizy, briefy, karty katalogowe)\n\n"
    "Struktura odpowiedzi:\n"
    "- # Tytuł analizy\n"
    "- ## TL;DR (3-5 bullet points)\n"
    "- ## Kluczowe ustalenia (z cytatami)\n"
    "- ## Liczby i dane\n"
    "- ## Implikacje dla ekosystemu HiveLive (jeśli vault_search znalazł powiązania)\n"
    "- ## Źródła (lista URL)\n\n"
    "Cytuj źródła inline: [opis](URL). Pisz konkretnie, bez wstępu/wody."
)

# Tools — vault_search to direct Directus (jak w conversational.py)
ANTHROPIC_TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,  # max 5 web search calls per research → ~$0.05 web cost
    },
    {
        "name": "vault_search",
        "description": "Szukaj w bazie wiedzy HiveLive (Vault + Directus knowledge_items). Użyj gdy temat dotyczy naszych produktów BEEzzy/bidBEE/BEEco/BEEZhub lub naszych projektów (np. BikeBox, Carport, Carbox, BizHub, dotacje FENG/Konsorcja).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
]


async def _vault_search(query: str, limit: int = 5) -> str:
    """Same as conversational._direct_vault_search ale dedicated dla research."""
    from app.services.conversational import _direct_vault_search
    return await _direct_vault_search(query=query, limit=limit)


async def _anthropic_research(prompt: str) -> ResearchResult:
    """Wywołuje Claude z web_search + vault_search w multi-step loop."""
    model = settings.anthropic_model or "claude-haiku-4-5"
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    total_in = 0
    total_out = 0
    total_web_searches = 0
    citations: list[str] = []
    final_text_parts: list[str] = []

    max_iterations = 6
    for iteration in range(max_iterations):
        payload = {
            "model": model,
            "max_tokens": 4000,
            "system": SYSTEM_PROMPT,
            "tools": ANTHROPIC_TOOLS,
            "messages": messages,
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
                    return ResearchResult(
                        success=False,
                        error=f"Anthropic HTTP {r.status_code}: {r.text[:300]}",
                    )
                data = r.json()
        except httpx.TimeoutException:
            return ResearchResult(success=False, error="Anthropic timeout 180s")
        except Exception as e:
            log.exception("research request error")
            return ResearchResult(success=False, error=str(e))

        usage = data.get("usage", {})
        total_in += int(usage.get("input_tokens", 0))
        total_out += int(usage.get("output_tokens", 0))
        st_use = usage.get("server_tool_use", {}) or {}
        total_web_searches += int(st_use.get("web_search_requests", 0))

        content = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        # Zbierz tekst, citations, tool_use (vault_search to bot-side)
        bot_tool_uses: list[dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                final_text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                # vault_search to bot-side (my wywołujemy w pętli)
                bot_tool_uses.append(block)
            elif btype == "web_search_tool_result":
                # Anthropic server-side tool — wyciągnij URL-e
                for item in block.get("content", []) or []:
                    if isinstance(item, dict):
                        url = item.get("url", "")
                        if url and url not in citations:
                            citations.append(url)

        messages.append({"role": "assistant", "content": content})

        if stop_reason == "tool_use" and bot_tool_uses:
            # Wykonaj vault_search (server-side web_search już wykonany przez Anthropic)
            tool_results: list[dict[str, Any]] = []
            for tu in bot_tool_uses:
                tool_id = tu.get("id", "")
                tool_name = tu.get("name", "")
                tool_input = tu.get("input", {}) or {}
                if tool_name == "vault_search":
                    result_str = await _vault_search(
                        query=tool_input.get("query", ""),
                        limit=int(tool_input.get("limit", 5)),
                    )
                else:
                    result_str = json.dumps({"error": f"Unknown bot tool: {tool_name}"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_str,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        break  # end_turn / max_tokens / inne

    final_text = "\n".join(t for t in final_text_parts if t.strip())
    if not final_text:
        return ResearchResult(success=False, error="Anthropic zwrócił pusty content")

    return ResearchResult(
        success=True,
        markdown=final_text,
        citations=citations,
        input_tokens=total_in,
        output_tokens=total_out,
        web_search_count=total_web_searches,
        cost_usd=_estimate_cost_anthropic(model, total_in, total_out, total_web_searches),
        model_used=model,
        provider="anthropic",
    )


async def _perplexity_research(prompt: str) -> ResearchResult:
    """Fallback: jeśli PERPLEXITY_API_KEY ustawione, używaj Perplexity Sonar."""
    from app.services import perplexity
    r = await perplexity.research(prompt)
    return ResearchResult(
        success=r.success,
        markdown=r.markdown,
        citations=r.citations,
        input_tokens=r.input_tokens,
        output_tokens=r.output_tokens,
        cost_usd=r.cost_usd,
        model_used=r.model_used,
        provider="perplexity",
        error=r.error,
    )


async def research(prompt: str) -> ResearchResult:
    """
    Główna funkcja — wybiera provider:
      - default: Anthropic web_search (jeśli ANTHROPIC_API_KEY)
      - opcjonalnie: Perplexity (jeśli RESEARCH_PROVIDER=perplexity + PERPLEXITY_API_KEY)
    """
    import os
    provider = (os.environ.get("RESEARCH_PROVIDER") or "anthropic").lower()

    if provider == "perplexity" and settings.perplexity_api_key:
        return await _perplexity_research(prompt)

    if not settings.anthropic_api_key:
        if settings.perplexity_api_key:
            return await _perplexity_research(prompt)
        return ResearchResult(
            success=False,
            error="Ani ANTHROPIC_API_KEY ani PERPLEXITY_API_KEY nie skonfigurowane",
        )

    return await _anthropic_research(prompt)


async def upload_to_inbox(
    markdown: str,
    *,
    prompt: str,
    provider: str,
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
) -> UploadResult:
    """Upload wynik research jako .md do HOS inbox/ (Worker klasyfikator dostanie)."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(prompt, max_len=60)
    filename = f"research_{provider}_{date}_{slug}.md"

    full_md = (
        f"---\n"
        f"type: research_analiza\n"
        f"source: {provider}\n"
        f"prompt: {prompt[:200]}\n"
        f"date: {date}\n"
        f"---\n\n"
        f"# Research — {prompt[:80]}\n\n"
        f"**Provider:** {provider}\n"
        f"**Prompt:** {prompt}\n\n"
        f"---\n\n"
        f"{markdown}\n"
    )

    return await upload_telegram_file(
        data=full_md.encode("utf-8"),
        filename=filename,
        mime_type="text/markdown",
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        extra_metadata={
            "ulos-source": f"research_{provider}",
            "ulos-prompt": prompt[:200],
        },
    )
