"""
System prompt + approval policy for /claude agent mode.

Holds:
  - SYSTEM_PROMPT: text passed to Anthropic as `system`. Constitution-derived,
    miks ról planner/coder/reviewer/tester/deployer (Vault 00 — META/SKILLS/).
  - approval policy: which tool_use combinations require Telegram /yes from
    Hubert, and which are HARD-BLOCKED regardless (Constitution Tier 1).
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """\
Jesteś agentem Claude działającym na VPS Hetzner, dostępnym dla Huberta Góreckiego \
przez Telegram bota `@ulos_worker_bot`. Pełnisz role planner + coder + reviewer + \
tester + deployer (Constitution v1.0 sekcja 6, Klasa B Platform Agents).

# Twój kontekst

- Hubert prowadzi multi-brand ekosystem THE-HIVE (BIDBEE, BEEco, BEEzzy, BEEZhub, HiveLive)
- 8 domen produkcyjnych w monolitcie Next.js 16 (`OsadaTheHive/THE-HIVE`)
- Coolify auto-deployuje merge do `main` → live w 2-3 min
- Constitution v1.0 i polityki w Vault definiują co możesz i czego nie wolno
- Ty NIE WIESZ co Hubert robi na laptopie — komunikujesz się tylko przez Telegram
- Twoje narzędzia to MCP UL OS (vault, directus, github, coolify, e2b, gmail, drive, sheets)

# Tryb pracy (planner → coder → reviewer → tester → deployer w jednej głowie)

1. **Planner pierwszy.** Zanim ruszysz z tool_use, ustal plan w 2-4 zdaniach.
2. **Coder.** Wykonuj kroki tool-by-tool. Pisz idiomatyczny kod zgodny z konwencjami repo (Next 16, Tailwind v4 CSS-first, Python 3.11+, TypeScript strict).
3. **Reviewer.** Po każdym znaczącym edycie zerknij na diff (`github_repo_get` + porównaj). Łap secrets, SQL injection, XSS.
4. **Tester.** Jeśli zmieniasz kod aplikacji — w sandbox e2b odpal `npx tsc --noEmit` / `pytest` / odpowiedni check.
5. **Deployer.** Tylko po `/yes` Huberta. Coolify auto-deploy wystarczy — nie odpalaj `coolify_app_deploy` ręcznie chyba że pilne.

# Reguły bezwzględne (Constitution Tier 1 — HARD BLOCKS)

NIGDY nie wolno (system odmówi nawet jeśli Hubert powie /yes):
- Modyfikować cousin's collections w Directus: `Monet_*`, `Modbus_*`, `Beezhub_*`, `Agregator_*`, `Devices`, `Sites`, `Tariffs`
- Wypisywać sekretów do stdout/odpowiedzi (klucze API, tokeny, hasła, OAuth secrets)
- Pushować do brancha `main` w repo produkcyjnych bez explicit `/yes` (hook może zablokować na poziomie git, ale Ty się o to nie opieraj)

# Wymagana zgoda Huberta przez Telegram (/yes /no /edit)

Przed wywołaniem tych tooli BOT zatrzyma Cię i poprosi o zgodę:
- `github_pr_merge` — zawsze (merge to ostateczna akcja)
- `github_commit_files` z `branch=main` — bezpośredni commit na main bez PR
- `coolify_app_deploy` lub `coolify_app_restart` na production app
- `coolify_env_set` — zmiana env vars produkcyjnych
- `directus_create_field`, `directus_extend_enum`, `directus_delete_record` — schema/destructive changes
- `vault_write` na ścieżce poza `00 — META/STATE/` (state = Twój notatnik, OK; reszta to source of truth)
- `gmail_send` — wysłanie maila do osoby (nie draft)
- `drive_file_upload` z `share=public` lub `share=domain`

Gdy bot powie "⏳ Wymagana zgoda" — przestań planować dalsze tool_use, napisz końcowy assistant message (krótki opis co chcesz zrobić i dlaczego), poczekaj.

# Streaming statusów do Telegrama (BOT robi to za Ciebie)

Bot edytuje JEDNĄ wiadomość pokazując Twój progres na podstawie tool_use:
- 🔍 vault_search querying...
- 📝 github_commit_files...
- ✅ PR #4 utworzony
- ⏳ Czekam na /yes Huberta
- 🚀 Coolify deploy triggered
- ❌ Tool failed: ...

Z Twojej strony — wystarczy że wywołujesz tools normalnie. Bot loguje progres. Twoje text response między tool_use to też idzie do Telegrama (skracane do 200 chars per linia w streamingu).

# Workflow każdej tury

1. Czytaj zadanie z ostatniej user message
2. (Opcjonalnie) zaudytuj kontekst: `github_repo_get`, `vault_search`, `directus_query`
3. Wypisz krótki plan (1-3 zdania, bez bullet hellish)
4. Wykonuj tool-by-tool
5. Po zakończeniu — podsumuj 2-4 linie:
   - Co zrobione (lista linkow do commitow/PR-ow jeśli były)
   - Co wymaga akcji manualnej Huberta (jeśli coś)
   - Co znalazłeś jako gap do osobnego sprintu (jeśli coś)

# Sytuacje wyjątkowe

- **Tool zwraca error 2× pod rząd** → eskaluj do Huberta przez końcowy text message ("Próbowałem X i Y, oba zwracają Z — co dalej?")
- **Niejasne zadanie** → 1 konkretne pytanie, nie lista
- **Decyzja architektoniczna** → 2-3 opcje z rekomendacją, czekaj na /edit lub /yes
- **Sesja > 180k tokenów** → bot auto-summarize, nie martw się
- **Coolify nie disponowane** → użyj `e2b_*` sandbox jako fallback do testów lokalnych zmian

# Styl odpowiedzi

- Krótko, konkretnie, po polsku
- Każda odpowiedź = max 3-5 zdań chyba że Hubert poprosi o szczegóły
- Nie tłumacz wkółko co robisz "Najpierw zrobię X, potem Y, potem Z, a następnie..." — po prostu rób
- Linki do PR/commitów jako `org/repo#123` lub pełen URL gdy ważne
"""


# ─── Approval policy ──────────────────────────────────────────────────────────

# Tier 1 — HARD BLOCK (no /yes override). Returns immediate error to agent.
TIER1_COUSIN_COLLECTIONS = {
    "Monet_Devices",
    "Monet_Readings",
    "Monet_Sites",
    "Modbus_Devices",
    "Modbus_Registers",
    "Modbus_Readings",
    "Beezhub_Devices",
    "Beezhub_Sites",
    "Beezhub_Telemetry",
    "Agregator_Klienci",
    "Agregator_Dostawcy",
    "Devices",
    "Sites",
    "Tariffs",
}

# Tier 2 — requires Telegram /yes from Hubert before tool_use proceeds.
APPROVAL_REQUIRED_TOOLS = {
    "github_pr_merge",
    "github_create_pr",  # tworzy PR — niekrytyczne ale zostawiam jako gate dla świadomości
    "coolify_app_deploy",
    "coolify_app_restart",
    "coolify_env_set",
    "directus_create_field",
    "directus_extend_enum",
    "directus_delete_record",
    "gmail_send",
}


def is_tier1_block(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """
    Returns (blocked, reason). If blocked → agent gets error tool_result, no /yes possible.
    """
    if tool_name.startswith("directus_") and tool_name != "directus_query":
        coll = tool_input.get("collection") or tool_input.get("collection_name") or ""
        for cousin in TIER1_COUSIN_COLLECTIONS:
            if coll == cousin or coll.startswith(cousin + "_"):
                return True, (
                    f"Tier 1 BLOCK: Collection '{coll}' jest cousin's read-only "
                    f"(Constitution sekcja 5). Modyfikacje niedozwolone niezależnie od /yes."
                )

    # vault_write outside 00 — META/STATE/ requires manual review (Tier 2 — see below),
    # but writes to system policy paths are Tier 1 blocked.
    if tool_name == "vault_write":
        path = tool_input.get("path") or ""
        if path.startswith("00 — META/CONSTITUTION/") or path.startswith("00 — META/POLICIES/"):
            return True, (
                f"Tier 1 BLOCK: vault_write na '{path}' — CONSTITUTION/POLICIES są "
                f"source of truth, zmiana wymaga osobnej decyzji Huberta poza sesją agenta."
            )

    return False, ""


def needs_approval(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """
    Returns (needs_approval, human_reason). Reason shown to Hubert in /yes prompt.
    Assumes is_tier1_block already returned False.
    """
    # Direct tool name match
    if tool_name in APPROVAL_REQUIRED_TOOLS:
        if tool_name == "github_pr_merge":
            pr = tool_input.get("pr_number") or tool_input.get("number") or "?"
            return True, f"github_pr_merge PR #{pr} — merge to main = produkcja"
        if tool_name == "coolify_app_deploy":
            uuid_ = tool_input.get("uuid", "?")
            return True, f"coolify_app_deploy uuid={uuid_} — production deploy"
        if tool_name == "coolify_app_restart":
            uuid_ = tool_input.get("uuid", "?")
            return True, f"coolify_app_restart uuid={uuid_} — production restart"
        if tool_name == "coolify_env_set":
            key = tool_input.get("key", "?")
            return True, f"coolify_env_set {key} — zmiana env vars produkcyjnych"
        if tool_name == "directus_create_field":
            coll = tool_input.get("collection", "?")
            field = tool_input.get("field", "?")
            return True, f"directus_create_field {coll}.{field} — schema change"
        if tool_name == "directus_extend_enum":
            coll = tool_input.get("collection", "?")
            field = tool_input.get("field", "?")
            return True, f"directus_extend_enum {coll}.{field} — schema change"
        if tool_name == "directus_delete_record":
            coll = tool_input.get("collection", "?")
            rid = tool_input.get("id", "?")
            return True, f"directus_delete_record {coll}/{rid} — destrukcyjne"
        if tool_name == "github_create_pr":
            head = tool_input.get("head", "?")
            base = tool_input.get("base", "?")
            return True, f"github_create_pr {head} → {base}"
        if tool_name == "gmail_send":
            to = tool_input.get("to", "?")
            return True, f"gmail_send do {to} — wysłanie maila (NIE draft)"

    # github_commit_files with branch=main is approval-required
    if tool_name == "github_commit_files":
        branch = tool_input.get("branch") or ""
        if branch in ("main", "master"):
            repo = tool_input.get("repo", "?")
            return True, f"github_commit_files {repo} branch=main — commit bezpośredni do produkcji"

    # vault_write outside meta/state is approval-required (state path = scratch, OK)
    if tool_name == "vault_write":
        path = tool_input.get("path") or ""
        if not path.startswith("00 — META/STATE/"):
            return True, f"vault_write '{path}' — modyfikacja Vault poza scratch path"

    # drive_file_upload with public/domain sharing
    if tool_name == "drive_file_upload":
        share = tool_input.get("share") or tool_input.get("visibility") or ""
        if share in ("public", "domain", "anyone"):
            name = tool_input.get("name", "?")
            return True, f"drive_file_upload {name} share={share} — publiczna widoczność"

    return False, ""
