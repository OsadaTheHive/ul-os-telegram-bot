"""
Agent session persistence — per chat_id state for /claude command.

Storage strategy:
  Primary:  S3 (Hetzner Object Storage / Wasabi). Key:
            s3://${S3_BUCKET}/${S3_INBOX_PREFIX}claude-sessions/${chat_id}/${session_id}.json
            Plus index: s3://.../claude-sessions/${chat_id}/_active.json (pointer to active session_id).
  Fallback: local file logs/claude-sessions/${chat_id}/${session_id}.json (when S3 unconfigured).

Session shape (json):
  {
    "id": "uuid",
    "chat_id": 123,
    "user_id": 456,
    "title": "first 60 chars of first prompt",
    "status": "active" | "paused" | "awaiting_approval" | "completed" | "error",
    "history": [{role, content, ...}],   # Anthropic messages format
    "tokens_in": 0,
    "tokens_out": 0,
    "cost_usd": 0.0,
    "tool_calls": [{"name": "...", "ts": ..., "ok": true}, ...],
    "pending_approval": {                  # set when status == awaiting_approval
      "tool_name": "github_pr_merge",
      "tool_use_id": "toolu_...",
      "tool_input": {...},
      "reason": "PR merge wymaga /yes Huberta (Constitution Tier)",
      "requested_at": <iso>
    } | null,
    "last_progress_message_id": 789,       # Telegram message id used for streaming
    "model": "claude-sonnet-4-...",
    "created_at": <iso>,
    "updated_at": <iso>
  }

Concurrency: one active session per chat_id. New /claude on active session = continuation,
unless user invokes /claude_new (force new) or status is completed/error.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO_OK = True
except Exception:  # pragma: no cover - boto3 should always be present per requirements.txt
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment, misc]
    ClientError = Exception  # type: ignore[assignment, misc]
    _BOTO_OK = False

from ..config import settings

log = logging.getLogger(__name__)

LOCAL_DIR = Path(__file__).parent.parent.parent / "logs" / "claude-sessions"


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class PendingApproval:
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any]
    reason: str
    requested_at: str = field(default_factory=_iso)


@dataclass
class AgentSession:
    id: str
    chat_id: int
    user_id: int
    title: str = ""
    status: str = "active"  # active|paused|awaiting_approval|completed|error
    history: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    last_progress_message_id: int | None = None
    model: str = ""
    created_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)

    def touch(self) -> None:
        self.updated_at = _iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSession:
        return cls(
            id=data["id"],
            chat_id=int(data["chat_id"]),
            user_id=int(data.get("user_id", 0)),
            title=data.get("title", ""),
            status=data.get("status", "active"),
            history=data.get("history", []),
            tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            tool_calls=data.get("tool_calls", []),
            pending_approval=data.get("pending_approval"),
            last_progress_message_id=data.get("last_progress_message_id"),
            model=data.get("model", ""),
            created_at=data.get("created_at", _iso()),
            updated_at=data.get("updated_at", _iso()),
        )


# ─── Backend selection ─────────────────────────────────────────────────────────

def _s3_configured() -> bool:
    return bool(
        _BOTO_OK
        and settings.s3_endpoint
        and settings.s3_bucket
        and settings.s3_access_key_id
        and settings.s3_secret_access_key
    )


def _s3_client():
    return boto3.client(  # type: ignore[union-attr]
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region or "us-east-1",
    )


def _s3_session_key(chat_id: int, session_id: str) -> str:
    return f"{settings.s3_inbox_prefix}claude-sessions/{chat_id}/{session_id}.json"


def _s3_active_key(chat_id: int) -> str:
    return f"{settings.s3_inbox_prefix}claude-sessions/{chat_id}/_active.json"


def _s3_chat_prefix(chat_id: int) -> str:
    return f"{settings.s3_inbox_prefix}claude-sessions/{chat_id}/"


def _local_session_path(chat_id: int, session_id: str) -> Path:
    return LOCAL_DIR / str(chat_id) / f"{session_id}.json"


def _local_active_path(chat_id: int) -> Path:
    return LOCAL_DIR / str(chat_id) / "_active.json"


# ─── Public API ────────────────────────────────────────────────────────────────

def new_session(chat_id: int, user_id: int, model: str, title_seed: str = "") -> AgentSession:
    title = (title_seed or "").strip().splitlines()[0][:60] if title_seed else ""
    return AgentSession(
        id=str(uuid.uuid4()),
        chat_id=chat_id,
        user_id=user_id,
        title=title or "(bez tytułu)",
        model=model,
    )


def save(session: AgentSession) -> None:
    session.touch()
    data = json.dumps(session.to_dict(), ensure_ascii=False, default=str).encode("utf-8")

    if _s3_configured():
        try:
            cli = _s3_client()
            cli.put_object(
                Bucket=settings.s3_bucket,
                Key=_s3_session_key(session.chat_id, session.id),
                Body=data,
                ContentType="application/json",
            )
            if session.status in ("active", "paused", "awaiting_approval"):
                cli.put_object(
                    Bucket=settings.s3_bucket,
                    Key=_s3_active_key(session.chat_id),
                    Body=json.dumps({"session_id": session.id, "status": session.status}).encode("utf-8"),
                    ContentType="application/json",
                )
            return
        except (BotoCoreError, ClientError) as e:
            log.warning("S3 save failed (%s), falling back to local", e)

    # Local fallback
    p = _local_session_path(session.chat_id, session.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if session.status in ("active", "paused", "awaiting_approval"):
        _local_active_path(session.chat_id).write_text(
            json.dumps({"session_id": session.id, "status": session.status}),
            encoding="utf-8",
        )


def load(chat_id: int, session_id: str) -> AgentSession | None:
    if _s3_configured():
        try:
            cli = _s3_client()
            obj = cli.get_object(Bucket=settings.s3_bucket, Key=_s3_session_key(chat_id, session_id))
            return AgentSession.from_dict(json.loads(obj["Body"].read()))
        except (BotoCoreError, ClientError) as e:
            log.warning("S3 load failed for %s/%s (%s), trying local", chat_id, session_id, e)

    p = _local_session_path(chat_id, session_id)
    if not p.exists():
        return None
    try:
        return AgentSession.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning("Local load failed for %s/%s: %s", chat_id, session_id, e)
        return None


def load_active(chat_id: int) -> AgentSession | None:
    """Returns active|paused|awaiting_approval session for chat or None."""
    active_id = None
    if _s3_configured():
        try:
            cli = _s3_client()
            obj = cli.get_object(Bucket=settings.s3_bucket, Key=_s3_active_key(chat_id))
            active_id = json.loads(obj["Body"].read()).get("session_id")
        except (BotoCoreError, ClientError):
            pass

    if not active_id and _local_active_path(chat_id).exists():
        try:
            active_id = json.loads(_local_active_path(chat_id).read_text(encoding="utf-8")).get("session_id")
        except Exception as e:  # noqa: BLE001 — corrupt _active.json shouldn't crash bot
            log.debug("local _active.json read failed for chat=%s: %s", chat_id, e)

    if not active_id:
        return None
    sess = load(chat_id, active_id)
    if sess and sess.status in ("active", "paused", "awaiting_approval"):
        return sess
    return None


def clear_active_pointer(chat_id: int) -> None:
    """Remove _active pointer (after session completed/errored)."""
    if _s3_configured():
        try:
            _s3_client().delete_object(Bucket=settings.s3_bucket, Key=_s3_active_key(chat_id))
        except (BotoCoreError, ClientError):
            pass
    p = _local_active_path(chat_id)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def list_all_active() -> list[AgentSession]:
    """Used on bot startup to notify all chats that bot restarted."""
    out: list[AgentSession] = []
    seen_chat_ids: set[int] = set()

    if _s3_configured():
        try:
            cli = _s3_client()
            prefix = f"{settings.s3_inbox_prefix}claude-sessions/"
            paginator = cli.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj["Key"]
                    if not key.endswith("/_active.json"):
                        continue
                    try:
                        chat_id = int(key.split("/")[-2])
                    except (IndexError, ValueError):
                        continue
                    seen_chat_ids.add(chat_id)
                    sess = load_active(chat_id)
                    if sess:
                        out.append(sess)
        except (BotoCoreError, ClientError) as e:
            log.warning("S3 list_all_active failed: %s", e)

    if not LOCAL_DIR.exists():
        return out
    for chat_dir in LOCAL_DIR.iterdir():
        if not chat_dir.is_dir():
            continue
        try:
            chat_id = int(chat_dir.name)
        except ValueError:
            continue
        if chat_id in seen_chat_ids:
            continue
        sess = load_active(chat_id)
        if sess:
            out.append(sess)
    return out


def list_chat_sessions(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Light index: id, title, status, created_at — sorted newest first."""
    out: list[dict[str, Any]] = []
    if _s3_configured():
        try:
            cli = _s3_client()
            paginator = cli.get_paginator("list_objects_v2")
            prefix = _s3_chat_prefix(chat_id)
            for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj["Key"]
                    if key.endswith("/_active.json") or not key.endswith(".json"):
                        continue
                    try:
                        body = cli.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
                        d = json.loads(body)
                        out.append({
                            "id": d.get("id"),
                            "title": d.get("title"),
                            "status": d.get("status"),
                            "created_at": d.get("created_at"),
                            "tokens_in": d.get("tokens_in", 0),
                            "tokens_out": d.get("tokens_out", 0),
                            "cost_usd": d.get("cost_usd", 0.0),
                        })
                    except Exception as e:  # noqa: BLE001 — corrupt session file shouldn't poison list
                        log.debug("skipping unreadable S3 session %s: %s", key, e)
                        continue
        except (BotoCoreError, ClientError) as e:
            log.warning("S3 list_chat_sessions failed: %s", e)

    chat_dir = LOCAL_DIR / str(chat_id)
    if chat_dir.exists():
        for f in chat_dir.iterdir():
            if not f.suffix == ".json" or f.name == "_active.json":
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if any(o.get("id") == d.get("id") for o in out):
                    continue
                out.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "status": d.get("status"),
                    "created_at": d.get("created_at"),
                    "tokens_in": d.get("tokens_in", 0),
                    "tokens_out": d.get("tokens_out", 0),
                    "cost_usd": d.get("cost_usd", 0.0),
                })
            except Exception as e:  # noqa: BLE001 — corrupt session file shouldn't poison list
                log.debug("skipping unreadable local session %s: %s", f, e)
                continue

    out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return out[:limit]


def backend_label() -> str:
    """For diagnostics/logging."""
    return "s3" if _s3_configured() else "local"
