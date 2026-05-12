"""
Generator DOCX z markdown w Vault — Sprint 1.10.

Flow:
  /generate <vault-path-or-slug>
    → fetch markdown z Vault (przez MCP vault_read lub Directus knowledge_items)
    → Pandoc convert markdown → DOCX z reference-doc (template firmowy)
    → upload do HOS generated/<date>/<slug>.docx (przez Directus REST)
    → zwroc presigned URL lub Directus /assets/{id}

Wymaga `pandoc` na hostingu bota (apt install pandoc). Templates DOCX
przechowywane lokalnie: app/templates/<brand>.docx
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class GenerateResult:
    success: bool
    filename: str = ""
    size_bytes: int = 0
    source_vault_path: str = ""
    download_url: str = ""
    error: Optional[str] = None


# Template per brand — opcjonalnie reference-doc Pandoc
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
DEFAULT_TEMPLATE = "default"

# Heurystyka brand detection ze ściezki vault
BRAND_PATTERNS = [
    (re.compile(r"^00.*META|10.*HIVELIVE", re.I), "hivelive"),
    (re.compile(r"BEEZHUB", re.I), "beezhub"),
    (re.compile(r"BEEZZY", re.I), "beezzy"),
    (re.compile(r"BEECO|BEEcoLogi", re.I), "beeco"),
    (re.compile(r"BIDBEE", re.I), "bidbee"),
]


def _detect_brand(vault_path: str) -> str:
    for pattern, brand in BRAND_PATTERNS:
        if pattern.search(vault_path):
            return brand
    return DEFAULT_TEMPLATE


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80] or "doc"


def _check_pandoc() -> tuple[bool, str]:
    """Sprawdz czy pandoc jest dostepny w PATH."""
    try:
        result = subprocess.run(
            ["pandoc", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        first_line = result.stdout.split("\n")[0] if result.stdout else "pandoc"
        return True, first_line
    except FileNotFoundError:
        return False, "Pandoc nie zainstalowany. Zainstaluj: brew install pandoc / apt install pandoc"
    except Exception as e:
        return False, f"Pandoc check fail: {e}"


async def _fetch_vault_markdown_via_directus(query: str) -> tuple[str, str] | None:
    """
    Probuje znalezc i pobrac markdown z Directus knowledge_items.

    Strategia:
      1. Jezeli query wyglada na vault_path (zawiera /) → fetch po vault_path
      2. W przeciwnym razie → search po title/vault_path (LIKE %query%)

    Zwraca (vault_path, content_md) lub None.
    """
    if not settings.directus_url or not settings.directus_token:
        return None

    is_path = "/" in query or query.endswith(".md")
    filter_key = "filter[vault_path][_eq]" if is_path else "filter[_or][0][title][_icontains]"
    extra_filter: dict[str, str] = {filter_key: query}
    if not is_path:
        extra_filter["filter[_or][1][vault_path][_icontains]"] = query

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{settings.directus_url}/items/knowledge_items",
                params={
                    "fields": "id,title,vault_path,content_text,brand",
                    **extra_filter,
                    "limit": "5",
                    "sort": "-date_created",
                },
                headers={"Authorization": f"Bearer {settings.directus_token}"},
            )
            if r.status_code != 200:
                log.warning("vault fetch directus HTTP %s", r.status_code)
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            item = items[0]
            md = item.get("content_text", "") or ""
            vp = item.get("vault_path", "") or query
            return vp, md
    except Exception as e:
        log.warning("vault fetch via directus error: %s", e)
        return None


async def _fetch_vault_markdown_via_mcp(query: str) -> tuple[str, str] | None:
    """
    Fallback: probuje MCP vault_read jezeli skonfigurowane.
    """
    if not settings.mcp_base_url or not settings.mcp_bearer_token:
        return None
    try:
        from .mcp_client import call_tool  # type: ignore
    except ImportError:
        log.debug("MCP client niedostepny, pomijam fetch via MCP")
        return None

    try:
        if "/" in query or query.endswith(".md"):
            result = await call_tool("vault_read", {"path": query})
            content = result.get("content", "") if isinstance(result, dict) else ""
            if content:
                return query, content
        else:
            sresult = await call_tool("vault_search", {"query": query, "limit": 1})
            matches = sresult.get("matches", []) if isinstance(sresult, dict) else []
            if matches:
                first = matches[0]
                path = first.get("path", "")
                if path:
                    cresult = await call_tool("vault_read", {"path": path})
                    content = cresult.get("content", "") if isinstance(cresult, dict) else ""
                    if content:
                        return path, content
    except Exception as e:
        log.warning("vault fetch via MCP error: %s", e)
    return None


def _run_pandoc_sync(md_path: str, out_path: str, brand: str) -> tuple[bool, str]:
    """Synchroniczny pandoc convert. Zwraca (ok, err_msg)."""
    args = [
        "pandoc",
        md_path,
        "-o", out_path,
        "--from", "markdown",
        "--to", "docx",
        "--standalone",
    ]
    # Jezeli istnieje template — uzyj jako reference-doc
    template_path = TEMPLATES_DIR / f"{brand}.docx"
    if template_path.exists():
        args.extend(["--reference-doc", str(template_path)])
    elif (TEMPLATES_DIR / "default.docx").exists():
        args.extend(["--reference-doc", str(TEMPLATES_DIR / "default.docx")])

    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Pandoc timeout (60s)"
    except subprocess.CalledProcessError as e:
        return False, f"Pandoc fail: {e.stderr[:500] if e.stderr else e}"
    except Exception as e:
        return False, str(e)


async def _upload_to_directus(file_path: str, title: str) -> tuple[str, str] | None:
    """Upload DOCX do Directus → Directus storage adapter wsadzi do HOS. Zwraca (file_id, download_url)."""
    if not settings.directus_url or not settings.directus_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                files = {
                    "file": (Path(file_path).name, f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                }
                data = {"title": title}
                r = await client.post(
                    f"{settings.directus_url}/files",
                    headers={"Authorization": f"Bearer {settings.directus_token}"},
                    data=data,
                    files=files,
                )
                if r.status_code not in (200, 201):
                    log.warning("directus upload HTTP %s: %s", r.status_code, r.text[:200])
                    return None
                file_id = r.json().get("data", {}).get("id", "")
                if not file_id:
                    return None
                download_url = f"{settings.directus_url}/assets/{file_id}?download="
                return file_id, download_url
    except Exception as e:
        log.warning("directus upload error: %s", e)
        return None


async def generate_docx_from_vault(query: str) -> GenerateResult:
    """
    Glowna funkcja:
      1) Sprawdz pandoc
      2) Fetch markdown z Vault (Directus → MCP fallback)
      3) Pandoc → DOCX (z reference-doc per brand)
      4) Upload do Directus → HOS
      5) Zwroc URL
    """
    ok, msg = _check_pandoc()
    if not ok:
        return GenerateResult(success=False, error=msg)

    # 1) Fetch markdown
    md_result = await _fetch_vault_markdown_via_directus(query)
    if not md_result:
        md_result = await _fetch_vault_markdown_via_mcp(query)
    if not md_result:
        return GenerateResult(
            success=False,
            error=f"Nie znaleziono dokumentu w Vault/Directus dla '{query}'",
        )

    vault_path, md = md_result
    if not md.strip():
        return GenerateResult(
            success=False,
            source_vault_path=vault_path,
            error="Dokument znaleziony ale content_text pusty",
        )

    brand = _detect_brand(vault_path)
    slug = _slug(Path(vault_path).stem or query)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_filename = f"{date}_{slug}_{brand}.docx"

    # 2) Convert
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = os.path.join(tmpdir, "source.md")
        out_path = os.path.join(tmpdir, out_filename)
        Path(md_path).write_text(md, encoding="utf-8")

        ok_pandoc, err = await asyncio.to_thread(_run_pandoc_sync, md_path, out_path, brand)
        if not ok_pandoc:
            return GenerateResult(
                success=False,
                source_vault_path=vault_path,
                error=err,
            )

        size = Path(out_path).stat().st_size

        # 3) Upload do Directus
        upload = await _upload_to_directus(out_path, f"Generated: {slug} ({brand})")
        if not upload:
            return GenerateResult(
                success=False,
                source_vault_path=vault_path,
                filename=out_filename,
                size_bytes=size,
                error="Upload do Directus nieudany",
            )
        file_id, download_url = upload

    return GenerateResult(
        success=True,
        filename=out_filename,
        size_bytes=size,
        source_vault_path=vault_path,
        download_url=download_url,
    )
