"""
Dead Letter Queue listing - realne boto3 listing dla s3://ul-os-storage/inbox-failed/.

Worker UL OS (mode=hos, src/hos-poller.ts po Sprint 1 2026-05-11) wpisuje:
  s3://ul-os-storage/inbox-failed/<YYYY-MM-DD>/<filename>
z S3 metadata:
  x-worker-error: <error message, max 200 chars>

Bot komenda /dlq pokazuje ostatnie N items z dat + error_message + size + LastModified.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class DLQItem:
    key: str
    filename: str
    date: str  # YYYY-MM-DD z prefix
    size: int
    last_modified: datetime | None
    error_message: str | None


def _build_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=BotoConfig(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    )


def _list_failed_sync(limit: int) -> tuple[list[DLQItem], int]:
    """Synchroniczny listing inbox-failed/ + pobieranie metadata per obiekt."""
    client = _build_client()
    failed_prefix = "inbox-failed/"

    # List wszystkie pod inbox-failed/
    resp = client.list_objects_v2(
        Bucket=settings.s3_bucket,
        Prefix=failed_prefix,
        MaxKeys=200,  # bierzemy nadwyżkę żeby posortować i pokazać top N
    )
    contents = resp.get("Contents", []) or []
    # Wyklucz .gitkeep i "/" placeholdery
    real = [
        o for o in contents
        if o.get("Key", "")
        and not o["Key"].endswith("/")
        and not o["Key"].endswith("/.gitkeep")
    ]
    total = len(real)

    # Posortuj malejąco po LastModified (najnowsze najpierw)
    real.sort(key=lambda o: o.get("LastModified") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    top = real[:limit]

    # Dla każdego pobierz metadata (head_object) żeby wyciągnąć x-worker-error
    items: list[DLQItem] = []
    for obj in top:
        key = obj["Key"]
        # Wyciągnij datę z klucza inbox-failed/<YYYY-MM-DD>/<filename>
        parts = key[len(failed_prefix):].split("/", 1)
        date_part = parts[0] if len(parts) == 2 else "?"
        filename = parts[1] if len(parts) == 2 else key.split("/")[-1]

        error_msg = None
        try:
            head = client.head_object(Bucket=settings.s3_bucket, Key=key)
            # boto3 lowercase'uje user metadata keys
            metadata = head.get("Metadata", {}) or {}
            error_msg = metadata.get("x-worker-error") or metadata.get("worker-error")
        except ClientError as e:
            log.warning("head_object failed for %s: %s", key, e)

        items.append(
            DLQItem(
                key=key,
                filename=filename,
                date=date_part,
                size=obj.get("Size", 0) or 0,
                last_modified=obj.get("LastModified"),
                error_message=error_msg,
            )
        )

    return items, total


async def list_dlq_items(limit: int = 10) -> dict[str, Any]:
    """
    List failed items w DLQ (inbox-failed/).

    Args:
        limit: ile elementow zwrocic (default 10, max 50)

    Returns:
        dict z 'status', 'items' (list[DLQItem]), 'total', 'message'.
    """
    if not settings.s3_access_key_id or not settings.s3_endpoint:
        return {
            "status": "not_configured",
            "message": (
                "DLQ wymaga skonfigurowanego HOS.\n"
                "Ustaw S3_ENDPOINT, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY w .env."
            ),
            "items": [],
            "total": 0,
        }

    limit = max(1, min(limit, 50))

    try:
        items, total = await asyncio.to_thread(_list_failed_sync, limit)
    except ClientError as e:
        err = e.response.get("Error", {})
        msg = f"{err.get('Code', '?')}: {err.get('Message', str(e))}"
        log.error("DLQ list_objects_v2 ClientError: %s", msg)
        return {
            "status": "error",
            "message": f"S3 error: {msg}",
            "items": [],
            "total": 0,
        }
    except Exception as e:
        log.exception("DLQ unexpected error")
        return {
            "status": "error",
            "message": f"Blad: {e}",
            "items": [],
            "total": 0,
        }

    if total == 0:
        return {
            "status": "empty",
            "message": "DLQ pusty - wszystko OK, Worker nie zwracal failed ingestow.",
            "items": [],
            "total": 0,
        }

    return {
        "status": "ok",
        "message": f"DLQ ma {total} failed item(s). Pokazuje top {len(items)}.",
        "items": items,
        "total": total,
    }


# === Retry (Sprint 1.5+ — operacyjna obsługa DLQ) ===

@dataclass
class RetryResult:
    success: bool
    moved_from: str = ""
    moved_to: str = ""
    error: Optional[str] = None


def _retry_sync(failed_key: str) -> RetryResult:
    """Kopiuj z inbox-failed/.../<file> → inbox/<file>, potem usuń źródło."""
    client = _build_client()

    # Sprawdź czy źródło istnieje
    try:
        client.head_object(Bucket=settings.s3_bucket, Key=failed_key)
    except ClientError as e:
        err = e.response.get("Error", {})
        return RetryResult(success=False, error=f"Source not found: {err.get('Code')}")

    # Wyciągnij nazwę pliku z klucza (ostatni segment)
    filename = failed_key.split("/")[-1]
    # Dodaj timestamp prefix żeby uniknąć kolizji + wskazać że to retry
    retry_prefix = settings.s3_inbox_prefix or "inbox/"
    retry_prefix = retry_prefix.rstrip("/") + "/"
    new_key = f"{retry_prefix}retry-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{filename}"

    # S3 metadata wymaga ASCII tylko — sanityzujemy polskie znaki
    safe_from = failed_key.encode("ascii", "replace").decode("ascii").replace("?", "_")[:1000]
    try:
        # Server-side copy
        client.copy_object(
            Bucket=settings.s3_bucket,
            CopySource={"Bucket": settings.s3_bucket, "Key": failed_key},
            Key=new_key,
            MetadataDirective="REPLACE",
            Metadata={
                "ulos-retry-from": safe_from,
                "ulos-retry-at": datetime.now(timezone.utc).isoformat(),
            },
        )
        # Delete original z inbox-failed/
        client.delete_object(Bucket=settings.s3_bucket, Key=failed_key)
        return RetryResult(success=True, moved_from=failed_key, moved_to=new_key)
    except ClientError as e:
        err = e.response.get("Error", {})
        return RetryResult(
            success=False,
            error=f"{err.get('Code', '?')}: {err.get('Message', str(e))}",
        )
    except Exception as e:
        return RetryResult(success=False, error=str(e))


async def retry_dlq_item(failed_key: str) -> RetryResult:
    """
    Przerzuć plik z inbox-failed/<date>/<file> z powrotem do inbox/retry-<ts>-<file>.
    Worker (mode=hos) podniesie w max 30s i spróbuje przetworzyć ponownie.

    Idempotent: jeśli source juz nie istnieje, zwraca błąd "Source not found".
    """
    if not settings.s3_access_key_id or not settings.s3_endpoint:
        return RetryResult(success=False, error="S3 not configured")
    if not failed_key.startswith("inbox-failed/"):
        return RetryResult(
            success=False,
            error="Key musi zaczynać się od 'inbox-failed/' (sanity check)",
        )
    return await asyncio.to_thread(_retry_sync, failed_key)


async def retry_all_dlq(date_filter: Optional[str] = None, max_items: int = 50) -> dict[str, Any]:
    """
    Bulk retry — przerzuć wszystkie failed items (opcjonalnie tylko z konkretnej daty).

    Args:
        date_filter: YYYY-MM-DD, lub None dla wszystkich
        max_items: cap żeby nie spamić workera tysiącami plików

    Returns:
        dict z 'moved', 'errors', 'list'.
    """
    listed = await list_dlq_items(limit=max_items)
    if listed["status"] != "ok":
        return {"moved": 0, "errors": 0, "message": listed.get("message", "?")}

    moved = 0
    errors = 0
    items_log: list[str] = []
    for it in listed["items"]:
        if date_filter and it.date != date_filter:
            continue
        result = await retry_dlq_item(it.key)
        if result.success:
            moved += 1
            items_log.append(f"OK {it.filename}")
        else:
            errors += 1
            items_log.append(f"FAIL {it.filename}: {result.error}")

    return {
        "moved": moved,
        "errors": errors,
        "filter": date_filter or "all",
        "log": items_log,
    }
