"""
Proactive notifications — bot sam pisze do whitelist'y o ważnych rzeczach.

Sprint 1.11 (ADR Hubert): cron co 4h sprawdza:
  1) DLQ (s3://ul-os-storage/inbox-failed/) — czy są nowe failed od ostatniego ticka
  2) Worker queue (s3://ul-os-storage/inbox/) — czy >ALERT_QUEUE_THRESHOLD plików (default 50)
  3) Directus knowledge_items: items z status='pending_review' starsze niż ALERT_REVIEW_DAYS (7 dni)
  4) Grant deadlines: Vault search w 00 — META / 01 — DZIENNIK / 50 — BIDBEE
     po dokumentach z frontmatter `deadline_date` ≤ 7 dni
  5) Health checks (health_checks w ulos_knowledge, przez MCP directus_query): jakikolwiek
     check_name ze statusem 'fail' w ostatnich 20 minutach — dodane 2026-07-15 (Hubert:
     "chce wiedziec realnie co sie zesralo, a nie dowiadywac sie po dniu"). To jest most,
     ktorego brakowalo: worker (ul-os-worker/ul-os-mcp) juz pisze do health_checks/
     worker_alerts, ale nic wczesniej stad nie forwardowalo na Telegram.

Antiflapping: per-check cooldown (state w `logs/notifier_state.json`):
  - DLQ: alert TYLKO gdy count wzrósł od ostatniego ticka
  - Queue: alert raz na N min jeśli >threshold (default 30 min cooldown)
  - Review: alert raz dziennie (24h cooldown)
  - Deadlines: alert raz dziennie per deadline_id
  - Health: alert raz na COOLDOWN_HEALTH (default 20 min) PER check_name — nie per tick,
    żeby nie spamować tym samym FAIL co 4h jesli sie nie zmienil, ale tez nie co tick jesli
    tick byloby czestsze.

JobQueue (python-telegram-bot) wywołuje `notifier.tick(context)` co 4h.
Notyfikacja wysyłana do każdego user_id w ADMIN_CHAT_IDS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from telegram.ext import ContextTypes

from app.config import settings
from app.services.mcp_client import get_client as get_mcp_client, extract_text_content, MCPError

log = logging.getLogger(__name__)

# === Konfiguracja ===
ALERT_QUEUE_THRESHOLD = 50  # >50 plików w inbox/ = alert
ALERT_REVIEW_DAYS = 7  # _NEEDS_REVIEW starsze niż X dni
ALERT_DEADLINE_DAYS = 7  # grant deadlines w ciągu N dni
STATE_FILE = Path(__file__).parent.parent.parent / "logs" / "notifier_state.json"

# Cooldowns (sec)
COOLDOWN_QUEUE = 30 * 60       # 30 min
COOLDOWN_REVIEW = 24 * 3600    # 24h
COOLDOWN_DEADLINE_PER = 24 * 3600  # 24h per deadline_id
COOLDOWN_HEALTH_PER = 20 * 60  # 20 min per check_name
# Ile minut wstecz uznajemy pomiar health_checks za "świeży" (zbieżne z regułą service_check).
HEALTH_FRESHNESS_MIN = 20


@dataclass
class NotifierState:
    """Stan utrzymywany między tickami żeby uniknąć spamu."""
    last_dlq_count: int = 0
    last_dlq_alert_ts: float = 0.0
    last_queue_alert_ts: float = 0.0
    last_review_alert_ts: float = 0.0
    deadline_alerted: dict[str, float] = field(default_factory=dict)
    health_alerted: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "NotifierState":
        if not STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return cls(**data)
        except Exception as e:
            log.warning("notifier state load fail: %s", e)
            return cls()

    def save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("notifier state save fail: %s", e)


def _build_s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=BotoConfig(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            retries={"max_attempts": 2},
        ),
    )


def _count_objects_sync(prefix: str) -> int:
    """Synchroniczny count obiektów pod prefix (max 1000)."""
    client = _build_s3()
    resp = client.list_objects_v2(
        Bucket=settings.s3_bucket,
        Prefix=prefix,
        MaxKeys=1000,
    )
    contents = resp.get("Contents", []) or []
    return sum(
        1 for o in contents
        if not o["Key"].endswith("/") and not o["Key"].endswith("/.gitkeep")
    )


# === Pojedyncze checki ===

async def check_dlq(state: NotifierState) -> str | None:
    """Alert jeśli DLQ count wzrósł od ostatniego ticka."""
    if not settings.s3_endpoint or not settings.s3_access_key_id:
        return None
    try:
        count = await asyncio.to_thread(_count_objects_sync, "inbox-failed/")
    except ClientError as e:
        log.warning("check_dlq S3 error: %s", e)
        return None
    except Exception as e:
        log.warning("check_dlq unexpected: %s", e)
        return None

    if count > state.last_dlq_count:
        diff = count - state.last_dlq_count
        msg = (
            f"🪦 DLQ wzrósł: +{diff} nowych failed.\n"
            f"Total: {count} items w inbox-failed/\n"
            f"Sprawdź: /dlq {min(diff + 5, 20)}"
        )
        state.last_dlq_count = count
        state.last_dlq_alert_ts = datetime.now(timezone.utc).timestamp()
        return msg

    # Update stan nawet jeśli count spadł (np. po manual cleanup)
    state.last_dlq_count = count
    return None


async def check_inbox_queue(state: NotifierState) -> str | None:
    """Alert jeśli >threshold plików w inbox/ (Worker nie nadąża)."""
    if not settings.s3_endpoint or not settings.s3_access_key_id:
        return None
    try:
        count = await asyncio.to_thread(_count_objects_sync, "inbox/")
    except Exception as e:
        log.warning("check_inbox_queue error: %s", e)
        return None

    if count < ALERT_QUEUE_THRESHOLD:
        return None

    now = datetime.now(timezone.utc).timestamp()
    if now - state.last_queue_alert_ts < COOLDOWN_QUEUE:
        return None

    state.last_queue_alert_ts = now
    return (
        f"⏳ Worker queue: {count} plików w inbox/ czeka.\n"
        f"Próg alertu: >{ALERT_QUEUE_THRESHOLD}.\n"
        f"Worker przerabia ~2/30s = ~240 plików/h. Czas oczekiwania: ~{count // 4} min.\n"
        f"Sprawdź healthcheck workera."
    )


async def check_needs_review(state: NotifierState) -> str | None:
    """Alert: items z status='pending_review' starsze niż 7 dni."""
    if not settings.directus_url or not settings.directus_token:
        return None

    now = datetime.now(timezone.utc).timestamp()
    if now - state.last_review_alert_ts < COOLDOWN_REVIEW:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=ALERT_REVIEW_DAYS)).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{settings.directus_url}/items/beezzy_products",
                params={
                    "fields": "id",
                    "filter[status][_eq]": "pending_review",
                    "filter[date_created][_lt]": cutoff,
                    "aggregate[count]": "id",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
            if r.status_code != 200:
                return None
            data = r.json().get("data", [])
            count = int(data[0].get("count", {}).get("id", 0)) if data else 0
    except Exception as e:
        log.warning("check_needs_review error: %s", e)
        return None

    if count == 0:
        return None

    state.last_review_alert_ts = now
    return (
        f"📋 _NEEDS_REVIEW: {count} items pending_review > {ALERT_REVIEW_DAYS} dni.\n"
        f"Wymagają decyzji: zatwierdź lub odrzuć.\n"
        f"Sprawdź Directus admin → beezzy_products → filter status='pending_review'"
    )


async def check_grant_deadlines(state: NotifierState) -> list[str]:
    """
    Vault search po dokumentach z frontmatter `deadline_date` ≤ ALERT_DEADLINE_DAYS.

    Używa MCP vault_search jeśli skonfigurowane.
    Jako fallback: Directus query po knowledge_items.document_date jeśli type='grant_deadline'.
    """
    if not settings.directus_url or not settings.directus_token:
        return []

    cutoff_iso = (datetime.now(timezone.utc) + timedelta(days=ALERT_DEADLINE_DAYS)).date().isoformat()
    today_iso = datetime.now(timezone.utc).date().isoformat()

    alerts: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "fields": "id,title,type,project,document_date",
                    "filter[type][_in]": "grant_deadline,grant_call,deadline",
                    "filter[document_date][_gte]": today_iso,
                    "filter[document_date][_lte]": cutoff_iso,
                    "sort": "document_date",
                    "limit": "20",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
            if r.status_code != 200:
                return []
            items = r.json().get("data", [])
    except Exception as e:
        log.warning("check_grant_deadlines error: %s", e)
        return []

    now = datetime.now(timezone.utc).timestamp()
    for it in items:
        deadline_id = it.get("id", "")
        deadline_date = it.get("document_date", "")
        if not deadline_id or not deadline_date:
            continue
        # Per-deadline cooldown 24h
        last = state.deadline_alerted.get(deadline_id, 0.0)
        if now - last < COOLDOWN_DEADLINE_PER:
            continue
        try:
            d = datetime.fromisoformat(deadline_date).date()
            days_left = (d - datetime.now(timezone.utc).date()).days
        except Exception:
            days_left = "?"
        alerts.append(
            f"⏰ Deadline za {days_left} dni: {deadline_date}\n"
            f"  {it.get('title', '?')[:80]}\n"
            f"  Projekt: {it.get('project', '-')}\n"
            f"  ID: {deadline_id[:8]}"
        )
        state.deadline_alerted[deadline_id] = now

    return alerts


async def check_health_failures(state: NotifierState) -> list[str]:
    """
    NOWE 2026-07-15 (Hubert: chce realnego alertu na Telegram gdy cos sie zesra na Sparku/
    workerze, zamiast dowiadywac sie po 1-2 dniach).

    Odpytuje kolekcje health_checks w ulos_knowledge (przez MCP directus_query — ten sam
    serwer co reszta bota, zero nowych credentiali) po najnowszym wierszu KAżDEGO check_name
    i alertuje na status='fail'. Ignoruje 'warn' celowo — warny sa czeste (np. drive rozjazdy)
    i zalatwiane osobno; fail = realna awaria wymagajaca uwagi.

    Świeżość: pomija pomiary starsze niż HEALTH_FRESHNESS_MIN — martwy runner (overall=stale
    w service_check) nie powinien generowac starych alertow w kolko.

    Cooldown PER check_name (nie per tick) — ten sam check_name w stanie fail nie alertuje
    ponownie przez COOLDOWN_HEALTH_PER, ale RESETUJE cooldown gdy wraca do ok (żeby kolejny
    fail po naprawie od razu alertowal, nie czekal na wygasniecie starego cooldownu).
    """
    client = get_mcp_client()
    if client is None:
        return []

    try:
        result = await client.call_tool(
            "directus_query",
            {
                "collection": "health_checks",
                "query": {
                    "fields": ["check_name", "status", "value", "detail", "ts"],
                    "sort": ["-ts"],
                    "limit": 200,
                },
            },
        )
        text = extract_text_content(result)
        rows = json.loads(text) if text else []
        if isinstance(rows, dict):
            rows = rows.get("data", [])
    except (MCPError, json.JSONDecodeError, Exception) as e:
        log.warning("check_health_failures MCP error: %s", e)
        return []

    # Najnowszy wiersz per check_name (rows juz posortowane -ts, wiec pierwszy trafiony wygrywa).
    latest: dict[str, dict] = {}
    for row in rows:
        name = row.get("check_name")
        if name and name not in latest:
            latest[name] = row

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - HEALTH_FRESHNESS_MIN * 60
    alerts: list[str] = []

    for name, row in latest.items():
        status = row.get("status")
        ts_raw = row.get("ts")
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue  # pomiar zbyt stary — martwy runner, nie realny fail teraz

        if status != "fail":
            # Wraca do zdrowia — resetuj cooldown, zeby kolejny fail alertowal od razu.
            state.health_alerted.pop(name, None)
            continue

        last = state.health_alerted.get(name, 0.0)
        if now - last < COOLDOWN_HEALTH_PER:
            continue

        alerts.append(
            f"🔥 Health FAIL: {name}\n"
            f"  {row.get('detail', '?')[:200]}\n"
            f"  wartosc: {row.get('value', '?')}"
        )
        state.health_alerted[name] = now

    return alerts


# === Bot job ===

async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Wywołane co 4h przez JobQueue. Sprawdza wszystkie warunki, wysyła
    alerty do whitelist'y.
    """
    log.info("notifier tick start")
    state = NotifierState.load()
    messages: list[str] = []

    # Wszystkie checki równolegle (asyncio.gather z return_exceptions)
    results = await asyncio.gather(
        check_dlq(state),
        check_inbox_queue(state),
        check_needs_review(state),
        check_grant_deadlines(state),
        check_health_failures(state),
        return_exceptions=True,
    )

    for r in results:
        if isinstance(r, Exception):
            log.warning("notifier check error: %s", r)
            continue
        if isinstance(r, str):
            messages.append(r)
        elif isinstance(r, list):
            messages.extend(r)

    state.save()

    if not messages:
        log.info("notifier tick: brak alertów")
        return

    payload = "🔔 UL OS notifications\n\n" + "\n\n---\n\n".join(messages)
    # Telegram max 4096 chars/msg
    if len(payload) > 3900:
        payload = payload[:3900] + "\n…(obciete)"

    for uid in settings.admin_user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=payload)
            log.info("notifier sent to %s (%d alerts)", uid, len(messages))
        except Exception as e:
            log.warning("notifier send fail to %s: %s", uid, e)


async def manual_run() -> dict[str, Any]:
    """Wywołane przez bot komendę /alerts żeby ręcznie sprawdzić alerty."""
    state = NotifierState.load()
    results: dict[str, Any] = {}

    dlq_msg = await check_dlq(state)
    results["dlq"] = dlq_msg or "OK"

    queue_msg = await check_inbox_queue(state)
    results["queue"] = queue_msg or "OK"

    review_msg = await check_needs_review(state)
    results["needs_review"] = review_msg or "OK"

    deadlines = await check_grant_deadlines(state)
    results["deadlines"] = deadlines or ["OK"]

    health = await check_health_failures(state)
    results["health"] = health or ["OK"]

    state.save()
    return results
