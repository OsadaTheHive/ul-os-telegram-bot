"""
Perplexity Sonar Deep Research client — Sprint 1.7.

API: https://api.perplexity.ai/chat/completions (POST)
Model: sonar-deep-research (lub sonar-pro fallback)

Flow:
  /research <prompt>
    → POST do Perplexity z model=sonar-deep-research
    → response.choices[0].message.content (markdown z citations)
    → citations w response.citations (list[url])
    → upload markdown jako .md do HOS inbox/ → Worker przerobi do Directus + Vault

Koszt: ~$0.005 per query (sonar-deep-research), ~$0.001 per query (sonar-pro)
Dokumentacja: https://docs.perplexity.ai/api-reference/chat-completions-post
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.services.hos_uploader import upload_telegram_file, UploadResult

log = logging.getLogger(__name__)

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"


@dataclass
class ResearchResult:
    success: bool
    markdown: str = ""
    citations: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model_used: str = ""
    error: Optional[str] = None


# Perplexity Sonar pricing 2026-05 (per 1M tokens):
# sonar-deep-research: $5 input, $25 output, +$0.005 per search
# sonar-pro: $3 input, $15 output, +$0.005 per search
# sonar: $1 input, $1 output
def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    pricing = {
        "sonar-deep-research": (5.0, 25.0),
        "sonar-pro": (3.0, 15.0),
        "sonar": (1.0, 1.0),
    }
    in_per_m, out_per_m = pricing.get(model, (3.0, 15.0))
    return (in_tok * in_per_m + out_tok * out_per_m) / 1_000_000 + 0.005


def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] or "research"


async def research(prompt: str, model: Optional[str] = None) -> ResearchResult:
    """
    Wyślij prompt do Perplexity Deep Research, zwróć markdown + citations.
    """
    if not settings.perplexity_api_key:
        return ResearchResult(success=False, error="PERPLEXITY_API_KEY nie skonfigurowane")

    use_model = model or settings.perplexity_model or "sonar-deep-research"

    payload = {
        "model": use_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Jesteś analitykiem rynku energii odnawialnej (PV, BESS, magazyny energii, "
                    "stacje ładowania). Odpowiadasz po polsku, strukturyzowany markdown z nagłówkami. "
                    "Zawsze cytuj źródła. Pisz konkretnie, bez wstępu."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "return_citations": True,
        "return_images": False,
        "temperature": 0.2,
        "max_tokens": 4000,
    }

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                PERPLEXITY_URL,
                headers={
                    "Authorization": f"Bearer {settings.perplexity_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code != 200:
                return ResearchResult(
                    success=False,
                    error=f"Perplexity HTTP {r.status_code}: {r.text[:300]}",
                )
            data = r.json()
    except httpx.TimeoutException:
        return ResearchResult(success=False, error="Perplexity timeout (180s)")
    except Exception as e:
        log.exception("perplexity request error")
        return ResearchResult(success=False, error=f"Perplexity request error: {e}")

    try:
        markdown = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return ResearchResult(success=False, error=f"Perplexity bad response: {data}")

    citations = data.get("citations", []) or []
    usage = data.get("usage", {})
    in_tok = int(usage.get("prompt_tokens", 0))
    out_tok = int(usage.get("completion_tokens", 0))

    # Doczep citations jako sekcja na końcu markdown
    if citations:
        markdown += "\n\n## Źródła\n\n"
        for i, c in enumerate(citations, start=1):
            markdown += f"{i}. {c}\n"

    return ResearchResult(
        success=True,
        markdown=markdown,
        citations=citations,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=_estimate_cost(use_model, in_tok, out_tok),
        model_used=use_model,
    )


async def upload_to_inbox(
    markdown: str,
    *,
    prompt: str,
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
) -> UploadResult:
    """
    Upload wynik Perplexity jako .md do HOS inbox/.
    Worker (mode=hos) wykryje, klasyfikator określi brand/type.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(prompt, max_len=60)
    filename = f"perplexity_{date}_{slug}.md"

    # Wrap content z frontmatter — pomoze klasyfikatorowi
    full_md = (
        f"---\n"
        f"type: research_analiza\n"
        f"source: perplexity\n"
        f"prompt: {prompt[:200]}\n"
        f"date: {date}\n"
        f"---\n\n"
        f"# Perplexity Deep Research\n\n"
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
            "ulos-source": "perplexity",
            "ulos-prompt": prompt[:200],
        },
    )
