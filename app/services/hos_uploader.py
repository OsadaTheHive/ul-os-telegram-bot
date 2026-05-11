"""
HOS uploader - forwardowanie plikow z Telegrama do Hetzner Object Storage inbox/.

Po wgraniu pliku do s3://ul-os-storage/inbox/{tenant}/{date}/{filename}
Worker UL OS (mode=hos) wykrywa nowy obiekt w ciagu 30 sek przez HOS Poller,
pobiera, klasyfikuje, zapisuje do Directus + Vault + przenosi do inbox-processed/.

Konfiguracja: env S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
S3_REGION, S3_INBOX_PREFIX (default "inbox/").

Idempotency: caller (handle_document etc.) juz robi idempotency check przez
telegram_file_key(file_id, size). Tu sluzymy tylko uploadowi.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class UploadResult:
    """Rezultat uploadu pliku do HOS inbox."""
    success: bool
    s3_key: str
    bytes_uploaded: int
    error: Optional[str] = None


def _sanitize_filename(name: str) -> str:
    """Usun znaki niebezpieczne dla klucza S3. Zachowaj rozszerzenie."""
    # S3 keys nie cierpia: znaki specjalne, spacje, polskie znaki na niektorych klientach.
    # Maperuje do alphanumeric + - _ .
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "file"


def _build_s3_key(filename: str, telegram_user_id: int) -> str:
    """
    Format: inbox/<tenant>/<YYYY-MM-DD>/<unix_ts>_<user>_<safe_filename>

    Worker w mode=hos polluje s3://ul-os-storage/inbox/ rekursywnie wiec
    podfoldery dziala. Format z timestampem zapobiega kolizjom przy
    tym samym filename wgrywanym wielokrotnie.
    """
    prefix = (settings.s3_inbox_prefix or "inbox/").rstrip("/")
    tenant = settings.tenant_id or "default"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = int(datetime.now(timezone.utc).timestamp())
    safe_name = _sanitize_filename(filename)
    return f"{prefix}/{tenant}/{date}/{ts}_{telegram_user_id}_{safe_name}"


def _is_configured() -> bool:
    """True jesli wszystkie wymagane S3 envs sa ustawione."""
    return bool(
        settings.s3_endpoint
        and settings.s3_bucket
        and settings.s3_access_key_id
        and settings.s3_secret_access_key
    )


def _build_client():
    """Boto3 S3 client zskonfigurowany dla HOS (path-style, region nbg1)."""
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


def _upload_sync(
    data: bytes,
    key: str,
    mime_type: Optional[str],
    metadata: dict[str, str],
) -> UploadResult:
    """Synchroniczny upload przez boto3 put_object."""
    try:
        client = _build_client()
        kwargs: dict = {
            "Bucket": settings.s3_bucket,
            "Key": key,
            "Body": data,
        }
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            # S3 metadata: tylko ASCII, max 2KB total. Filtrujemy non-ASCII.
            safe_meta = {
                k: v.encode("ascii", "ignore").decode("ascii")[:200]
                for k, v in metadata.items()
                if v
            }
            kwargs["Metadata"] = safe_meta
        client.put_object(**kwargs)
        return UploadResult(success=True, s3_key=key, bytes_uploaded=len(data))
    except ClientError as e:
        err = e.response.get("Error", {})
        msg = f"{err.get('Code', '?')}: {err.get('Message', str(e))}"
        log.error("HOS upload ClientError key=%s err=%s", key, msg)
        return UploadResult(success=False, s3_key=key, bytes_uploaded=0, error=msg)
    except Exception as e:
        log.exception("HOS upload unexpected error key=%s", key)
        return UploadResult(success=False, s3_key=key, bytes_uploaded=0, error=str(e))


async def upload_telegram_file(
    data: bytes,
    filename: str,
    mime_type: Optional[str],
    telegram_user_id: int,
    telegram_username: Optional[str] = None,
    extra_metadata: Optional[dict[str, str]] = None,
) -> UploadResult:
    """
    Wgraj plik (bytes) do s3://ul-os-storage/inbox/<tenant>/<date>/<ts>_<user>_<filename>.

    Zwraca UploadResult. Wywoluje boto3 w thread pool zeby nie blokowac event loop.

    Worker UL OS wykryje plik w max 30 sek (mode=hos, POLL_INTERVAL_MS=30000).
    """
    if not _is_configured():
        return UploadResult(
            success=False,
            s3_key="",
            bytes_uploaded=0,
            error="S3 not configured (S3_ENDPOINT/BUCKET/KEY/SECRET missing)",
        )

    key = _build_s3_key(filename, telegram_user_id)
    metadata = {
        "tg-user-id": str(telegram_user_id),
        "tg-username": telegram_username or "unknown",
        "tg-original-filename": filename,
        "tg-uploaded-at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    # boto3 jest synchroniczny — odpalamy w threadpoolu zeby nie blokowac asyncio
    result = await asyncio.to_thread(_upload_sync, data, key, mime_type, metadata)
    if result.success:
        log.info(
            "HOS upload OK key=%s bytes=%d user=%s",
            result.s3_key,
            result.bytes_uploaded,
            telegram_user_id,
        )
    return result


async def healthcheck() -> tuple[bool, str]:
    """Check czy bucket dostepny + zwroc liczbe obiektow w inbox/ aktualnie."""
    if not _is_configured():
        return False, "S3 not configured"
    try:
        prefix = (settings.s3_inbox_prefix or "inbox/").rstrip("/") + "/"

        def _list():
            client = _build_client()
            return client.list_objects_v2(
                Bucket=settings.s3_bucket,
                Prefix=prefix,
                MaxKeys=100,
            )

        resp = await asyncio.to_thread(_list)
        count = resp.get("KeyCount", 0)
        return True, f"inbox/ ma {count} oczekujacych obiektow"
    except Exception as e:
        return False, str(e)
