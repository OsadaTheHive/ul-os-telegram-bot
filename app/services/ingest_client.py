"""
Klient Ingest API workera UL OS (POST /ingest/audio, POST /ingest/file).

Telegram-wrzutnia — Faza 2 toru SYNC (MASTERPLAN v2.3, Vault
"00 — META/UL_OS/MASTERPLAN_ULOS_2026-07-21.md").

Konfiguracja BEZ nowych sekretów: baza i token wyprowadzane z istniejących
PIPELINE_HEALTH_URL / PIPELINE_HEALTH_TOKEN (monitor.py używa ich od 2026-07-20;
to ten sam INGEST_TOKEN workera, a URL = .../ingest/health-checks). Gdyby
endpointy się kiedyś rozjechały — nadpisywalne przez INGEST_URL / INGEST_TOKEN.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# whisper na CPU workera: długie nagranie potrafi mielić się minutami —
# transkrypcja dzieje się SYNCHRONICZNIE w request/response /ingest/audio.
_AUDIO_TIMEOUT = httpx.Timeout(600.0, connect=15.0)
_TEXT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


def base_url() -> str:
    """Baza tras ingest (kończy się na /ingest) albo pusty string gdy brak konfiguracji."""
    if settings.ingest_url:
        return settings.ingest_url.rstrip("/")
    u = (settings.pipeline_health_url or "").rstrip("/")
    if u.endswith("/health-checks"):
        u = u[: -len("/health-checks")]
    if u.endswith("/ingest"):
        return u
    # Nieoczekiwany kształt PIPELINE_HEALTH_URL — nie zgadujemy tras na ślepo.
    return ""


def token() -> str:
    return settings.ingest_token or settings.pipeline_health_token


def configured() -> bool:
    return bool(base_url() and token())


@dataclass
class IngestResult:
    success: bool
    knowledge_id: str | None = None
    created: bool = False
    transcribed: bool = False
    deduplicated: bool = False
    original_saved: bool = False
    error: str | None = None


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {token()}"}


async def ingest_audio(
    data: bytes,
    *,
    filename: str,
    mime_type: str | None = None,
    host: str = "telegram-wrzutnia",
) -> IngestResult:
    """Surowe bajty audio → POST /ingest/audio (transkrypcja + S3 original po stronie workera)."""
    if not configured():
        return IngestResult(
            success=False,
            error="ingest nie skonfigurowany (brak PIPELINE_HEALTH_* ani INGEST_URL/TOKEN)",
        )
    headers = _auth_headers()
    headers["X-Filename"] = filename[:200]
    headers["X-Host"] = host[:64]
    # Content-Type NIE może być application/json — express.json na trasie /ingest
    # połknąłby body zanim dojdzie do express.raw w /ingest/audio.
    if mime_type and "json" not in mime_type.lower():
        headers["Content-Type"] = mime_type
    else:
        headers["Content-Type"] = "application/octet-stream"
    try:
        async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT) as client:
            r = await client.post(f"{base_url()}/audio", content=data, headers=headers)
        if r.status_code != 200:
            return IngestResult(success=False, error=f"HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        if not j.get("ok"):
            return IngestResult(success=False, error=str(j)[:200])
        return IngestResult(
            success=True,
            knowledge_id=j.get("knowledge_id"),
            created=bool(j.get("created")),
            transcribed=bool(j.get("transcribed")),
            deduplicated=bool(j.get("deduplicated")),
            original_saved=bool(j.get("original_saved")),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("ingest_audio fail (%s)", filename)
        return IngestResult(success=False, error=f"{e.__class__.__name__}: {e}")


async def ingest_text(
    text: str,
    *,
    title_hint: str = "forward",
    host: str = "telegram",
) -> IngestResult:
    """Tekst (np. forward z Telegrama) → POST /ingest/file — od razu `unclassified`."""
    if not configured():
        return IngestResult(
            success=False,
            error="ingest nie skonfigurowany (brak PIPELINE_HEALTH_* ani INGEST_URL/TOKEN)",
        )
    body_text = text.strip()
    if not body_text:
        return IngestResult(success=False, error="pusty tekst")
    digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    safe_hint = (
        "".join(c for c in title_hint if c.isalnum() or c in "-_ ")[:40].strip().replace(" ", "-")
        or "forward"
    )
    filename = f"telegram_{safe_hint}_{digest[:12]}.md"
    payload = {
        "content_hash": digest,
        "host": host,
        # path unikalny per treść — dedup i ON CONFLICT(host,path) idempotentne.
        "path": f"telegram:{digest}",
        "filename": filename,
        "mime_type": "text/markdown",
        "size_bytes": len(body_text.encode("utf-8")),
        "source": "telegram-wrzutnia",
        "text": body_text,
    }
    try:
        async with httpx.AsyncClient(timeout=_TEXT_TIMEOUT) as client:
            r = await client.post(f"{base_url()}/file", json=payload, headers=_auth_headers())
        if r.status_code != 200:
            return IngestResult(success=False, error=f"HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        if not j.get("ok"):
            return IngestResult(success=False, error=str(j)[:200])
        return IngestResult(
            success=True,
            knowledge_id=j.get("knowledge_id"),
            created=bool(j.get("created")),
            deduplicated=bool(j.get("deduplicated")),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("ingest_text fail")
        return IngestResult(success=False, error=f"{e.__class__.__name__}: {e}")
