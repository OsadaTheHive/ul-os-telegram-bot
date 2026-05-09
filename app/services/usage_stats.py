"""
Usage statistics - liczy koszty i wolumen z lokalnego audit.jsonl + Directus.

Anthropic Usage API wymaga organization_id (admin scope) - placeholder do
uzupelnienia gdy Hubert dorzuci ANTHROPIC_ORG_ID + ANTHROPIC_ADMIN_KEY.

Tymczasowo - estymacja kosztow z naszego audit log:
- /document/photo/voice ingest = po 1 klasyfikacja Haiku (~$0.0001) +
  ~1 multimodal Haiku call jezeli foto (~$0.003)
- Real usage z Directus tabela `ai_usage_log` (gdy bedzie wdrozona przez Worker)
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

AUDIT_FILE = Path(__file__).parent.parent.parent / "logs" / "audit.jsonl"

# Anthropic pricing (May 2026, Haiku 4.5)
HAIKU_INPUT_PER_M = 1.0      # $/1M tokens
HAIKU_OUTPUT_PER_M = 5.0
SONNET_INPUT_PER_M = 3.0
SONNET_OUTPUT_PER_M = 15.0

# Estymata tokenow per dokument (z dokumentu architektury)
EST_TOKENS_PER_DOC_INPUT = 8000  # truncate w klasyfikatorze
EST_TOKENS_PER_DOC_OUTPUT = 500  # struktura + classification
EST_COST_PER_CLASSIFICATION = (
    EST_TOKENS_PER_DOC_INPUT / 1_000_000 * HAIKU_INPUT_PER_M
    + EST_TOKENS_PER_DOC_OUTPUT / 1_000_000 * HAIKU_OUTPUT_PER_M
)  # ~$0.0105 per doc

EST_COST_PER_MULTIMODAL = 0.003  # 1 image (PDF page) ~$0.003 z roadmap


def _read_audit_lines(since_ts: float) -> list[dict[str, Any]]:
    """Czyta audit.jsonl od podanego timestamp."""
    if not AUDIT_FILE.exists():
        return []
    events = []
    try:
        with open(AUDIT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("ts", 0) >= since_ts:
                        events.append(e)
                except json.JSONDecodeError:
                    continue
    except OSError as ex:
        log.warning("audit read failed: %s", ex)
    return events


def stats_local(window_hours: int = 24) -> dict[str, Any]:
    """Lokalne statystyki z audit.jsonl - co bot przeszedl ostatnio."""
    since = time.time() - window_hours * 3600
    events = _read_audit_lines(since)

    by_action = defaultdict(int)
    by_result = defaultdict(int)
    by_user = defaultdict(int)
    rate_limited = 0

    for e in events:
        by_action[e.get("action", "?")] += 1
        by_result[e.get("result", "?")] += 1
        if e.get("username"):
            by_user[e["username"]] += 1
        if e.get("result") == "rate_limited":
            rate_limited += 1

    # Estymata kosztow (file ingest)
    file_ingests = (
        by_action.get("document", 0)
        + by_action.get("photo", 0)
        + by_action.get("voice", 0)
    )
    est_classification_cost = file_ingests * EST_COST_PER_CLASSIFICATION
    est_multimodal_cost = by_action.get("photo", 0) * EST_COST_PER_MULTIMODAL

    return {
        "window_hours": window_hours,
        "total_events": len(events),
        "by_action": dict(by_action),
        "by_result": dict(by_result),
        "by_user": dict(by_user),
        "rate_limited": rate_limited,
        "file_ingests": file_ingests,
        "est_cost_usd": round(est_classification_cost + est_multimodal_cost, 4),
        "audit_file_size_bytes": os.path.getsize(AUDIT_FILE) if AUDIT_FILE.exists() else 0,
    }


async def stats_directus(window_hours: int = 24) -> dict[str, Any]:
    """Statystyki z Directus knowledge_items - real ingest counts.

    Zwraca counts po brand + total + recent.
    """
    if not settings.directus_token:
        return {"error": "DIRECTUS_TOKEN nieustawiony"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Total + per brand
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "groupBy[]": "brand",
                    "aggregate[count]": "id",
                    "limit": -1,
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
            data = r.json().get("data", [])
            by_brand = {}
            for row in data:
                count_field = row.get("count", {})
                if isinstance(count_field, dict):
                    cnt = count_field.get("id", 0)
                else:
                    cnt = count_field or 0
                by_brand[row.get("brand", "?")] = cnt
            total = sum(by_brand.values())

            # Recent (window)
            since_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_hours * 3600)
            )
            r2 = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "filter[date_created][_gte]": since_iso,
                    "aggregate[count]": "id",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
            recent_data = r2.json().get("data", [])
            recent = 0
            if recent_data:
                count_field = recent_data[0].get("count", {})
                if isinstance(count_field, dict):
                    recent = count_field.get("id", 0)
                else:
                    recent = count_field or 0

            return {
                "total": total,
                "by_brand": by_brand,
                "recent_count": recent,
                "window_hours": window_hours,
            }
    except Exception as e:
        log.exception("directus stats failed")
        return {"error": str(e)}


async def stats_mcp() -> dict[str, Any]:
    """MCP server: vault info + tools count."""
    if not settings.mcp_bearer_token:
        return {"error": "MCP_BEARER_TOKEN nieustawiony"}

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{settings.mcp_base_url}/health",
                headers={"Authorization": f"Bearer {settings.mcp_bearer_token}"},
            )
            if r.status_code != 200:
                return {"error": f"MCP zwrocil {r.status_code}"}
            return r.json()
    except Exception as e:
        return {"error": str(e)}
