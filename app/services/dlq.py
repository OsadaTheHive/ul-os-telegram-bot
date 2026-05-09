"""
Dead Letter Queue check - placeholder do czasu Worker DLQ implementation.

Plan (per FILAR 1 S-2 z planu autonomii):
  HOS bucket struktura:
    ul-os-storage/incoming/   ← Worker zassawia
    ul-os-storage/processed/  ← sukces
    ul-os-storage/dlq/        ← failed po 3 retry
    ul-os-storage/dlq/<date>/<hash>.error.json  ← manifest

Aktualnie:
- Worker NIE wdrozony w Coolify
- HOS bucket utworzony ale niewykorzystany
- DLQ struktura nie istnieje

Bot komenda /dlq:
- jezeli ENV S3_ACCESS_KEY_ID jest ustawiony -> sprawdz bucket dlq/
- jezeli nie -> powiedz ze placeholder, czeka na Worker deploy
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)


async def list_dlq_items(limit: int = 10) -> dict[str, Any]:
    """List failed items in DLQ.

    Returns:
        dict z 'status', 'items' (list), 'total', albo 'error'.
    """
    if not settings.s3_access_key_id:
        return {
            "status": "not_configured",
            "message": (
                "DLQ wymaga skonfigurowanego Hetzner Object Storage.\n"
                "Worker DLQ jest planowany w Sprint 1 (Tier 0) - po wdrozeniu "
                "Workera w Coolify (osobny project ul-os per ADR-002)."
            ),
            "items": [],
            "total": 0,
        }

    # TODO: gdy S3 keys sa - uzyj boto3/aiobotocore do listy s3://ul-os-storage/dlq/
    # Aktualnie placeholder - bedzie aktywne po Worker deploy.
    return {
        "status": "configured_no_data",
        "message": (
            "S3 keys skonfigurowane, ale Worker DLQ jeszcze nie pisze do bucket.\n"
            "Po wdrozeniu Workera w Coolify - failed ingests trafiaja do "
            f"s3://{settings.s3_bucket}/dlq/<date>/<hash>.error.json."
        ),
        "items": [],
        "total": 0,
    }
