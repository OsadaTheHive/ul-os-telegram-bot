# Bot Commands Reference

Pełna dokumentacja komend bota (23 podstawowe + 10 /claude family).

## /claude agent mode

Pełen agent Claude (Sonnet 4 + extended thinking) z dostępem do wszystkich MCP UL OS tooli — wykonuje zadania end-to-end (analiza, commit, deploy) z poziomu Telegrama.

### `/claude <prompt>`
Start lub kontynuacja sesji agenta. Jedna aktywna sesja per chat.

```
/claude zaktualizuj hero na /prototypy żeby był bardziej kontrastowy
/claude sprawdź ostatni commit w THE-HIVE i wypisz 3 największe luki w /prototypy
```

Bot tworzy nową sesję (lub kontynuuje istniejącą). Edytuje JEDNĄ wiadomość pokazując progres tool-by-tool:
```
🔍 vault_search prototypy hero
🐙 github_repo_get OsadaTheHive/THE-HIVE
📦 e2b_sandbox_create base
📝 e2b_write_file /repo/src/.../page.tsx
🐙 github_commit_files feature/hero-contrast
⏳ Wymagana zgoda: github_pr_merge PR #5
```

Limit: 5 nowych sesji /h per user (kontynuacje sesji nie liczą się). Storage: S3 (`s3://${S3_BUCKET}/${S3_INBOX_PREFIX}claude-sessions/${chat_id}/`).

### `/claude_new <prompt>`
Wymusza start nowej sesji nawet jeśli istnieje aktywna. Aktywna przechodzi w `completed` (możliwa via /claude_history).

### `/claude_status`
Pokazuje aktywną sesję: id, tytuł, status, tury historii, tokeny, koszt, ostatni tool, storage backend, pending approval (jeśli jest).

### `/claude_history`
Lista 10 ostatnich sesji z statusami, tokenami, kosztem.

### `/claude_pause`
Pauzuje aktywną sesję — kolejna `/claude <prompt>` nie zostanie potraktowana jako kontynuacja. `/claude_resume` aby wrócić.

### `/claude_resume`
Wznawia zapauzowaną sesję. Jeśli status to `awaiting_approval`, informuje o pendingowej decyzji.

### `/claude_cost`
Kumulatywny koszt /claude — total ze wszystkich sesji per chat (z S3).

### `/yes` / `/no [powód]` / `/edit <instrukcja>`
Odpowiedzi na zapytania agenta (gdy bot napisze "⏳ Wymagana zgoda"):
- `/yes` — agent wykonuje pendingowy tool_use
- `/no [powód]` — agent dostaje synthetic error i kontynuuje innym podejściem
- `/edit <new>` — agent anuluje pending tool i dostaje nową dyrektywę jako kolejną user message

### Continuation bez `/claude` (tekst + voice)

Gdy istnieje aktywna sesja (status=active|paused), wiadomości BEZ prefiksu `/claude` są traktowane jako kontynuacja:

- **Plain text:** `wypisz mi tabelę kosztów` → bot wyświetla `💬 (kontynuacja sesji)` i podaje tekst agentowi.
- **Voice message:** transkrypcja przez Whisper (lokalny `whisper-cli`, ten sam stack co istniejący `/voice → HOS`), bot wyświetla `🎙 Transkrypcja: "..."` i podaje transkrypt agentowi.

Gdy sesja jest `awaiting_approval`, plain text i voice **nie kontynuują** — bot przypomina o `/yes /no /edit`. Gdy brak sesji, text bez komendy jest ignorowany (zachowane zachowanie sprzed sprintu — bot nie odpowiada na "cześć"), voice idzie domyślną ścieżką Whisper → HOS dla Worker classification.

Voice transkrypcja używa istniejącej infrastruktury (`whisper.cpp` w kontenerze, model `ggml-base.bin` lub przesłonięty przez `WHISPER_MODEL_PATH`). Brak dodatkowych zależności.

### Approval gates (kiedy bot pyta o /yes)

Tier 2 (wymaga `/yes`):
- `github_pr_merge`, `github_create_pr`
- `github_commit_files` z `branch=main` (bezpośredni commit do produkcji)
- `coolify_app_deploy`, `coolify_app_restart`, `coolify_env_set`
- `directus_create_field`, `directus_extend_enum`, `directus_delete_record`
- `vault_write` poza `00 — META/STATE/`
- `gmail_send` (NIE draft)
- `drive_file_upload` z `share=public|domain|anyone`

Tier 1 (HARD BLOCK, /yes niepomoże):
- `directus_*` na cousin's collections: Monet_*, Modbus_*, Beezhub_*, Agregator_*, Devices, Sites, Tariffs
- `vault_write` na `00 — META/CONSTITUTION/` lub `00 — META/POLICIES/`

### Edge cases

- Sesja > 180k tokenów → auto-summarize first half via Haiku, kontynuacja na nowej krótszej historii
- Bot restart → przy starcie skanuje S3, wysyła "🔄 Bot zrestartowany, sesja wznowiona" do każdego chat z aktywną sesją
- `/claude` na sesji w `awaiting_approval` → bot przypomina pending action i prosi o /yes /no /edit
- 24 iteracje agent-loop max per turn (chroni przed infinite loops)

---

Pełna dokumentacja 23 komend podstawowych bota poniżej.

## Podstawowe

### `/start`
Powitanie + sprawdzenie dostępu.
- Walidacja whitelist (ADMIN_CHAT_IDS)
- Audit log entry
- Lista podstawowych komend

### `/help`
Lista wszystkich komend.

### `/health`
Status systemu UL OS:
- Directus health
- MCP server (z tools count + vault last pull)
- Worker (jeszcze nie wdrożony)
- Hetzner Object Storage (po setup keys)
- **Circuit breakers state** (gdy któryś OPEN, widać od razu)

Przykład response:
```
Status UL OS:
 * Directus: OK
 * MCP server: OK (5 tools, vault pull 2026-05-09 16:31)
 * Worker: nie skonfigurowany
 * Hetzner Object Storage: brak kluczy (Tier 0 milestone)
```

## Search

### `/szukaj <query>`
Full-text search w `knowledge_items` po:
- title
- content_text
- summary

```
/szukaj magazyn energii LFP
```

Wynik: 10 najnowszych docs matching query, sortowane po dacie.

### `/mcp_szukaj <query>`
Vault search przez MCP server (`vault_search` tool).
Grep-style search po Markdown w HiveLive_Vault.

```
/mcp_szukaj BEEzzy strategia sprzedazy
```

Różnica vs `/szukaj`:
- `/szukaj` — Directus REST API, structured data (knowledge_items metadata)
- `/mcp_szukaj` — Vault Markdown content (full strategy docs, briefs, etc.)

### `/mcp_tools`
Lista 5 tools wystawianych przez MCP server.

## Directus query

### `/produkt <nazwa>`
Info produktu BEEzzy z kolekcji `beezzy_products`. Search po:
- title
- model

```
/produkt PowerHill 261kWh
```

Wynik: do 5 produktów z manufacturer, model, capacity_kwh, power_w, price_retail_pln, description_short.

### `/ostatnie`
10 najnowszych dokumentów dodanych do `knowledge_items`.

### `/ulos_status`
Statystyki bazy wiedzy:
- Total docs
- Per brand (BEEzzy, BEEZhub, BEEco, META, bidBEE)

## Diagnostyka admin

### `/koszty [days]`
Estymata kosztów API + ingest stats.

Default: ostatnie 24h. Opcjonalny argument: `/koszty 7` = 7 dni.

Pokazuje:
- Bot events count
- File ingests (estymata Anthropic cost)
- Rate-limited count
- Directus knowledge_items per brand

### `/dlq`
Dead Letter Queue — failed ingests.

Aktualnie placeholder do czasu Worker DLQ implementation. Po wdrożeniu Worker (Sprint 1):
- Lista failed items z `s3://ul-os-storage/dlq/<date>/<hash>.error.json`
- Manifest błędu (error_type, retry_count, last_error_message)

### `/digest`
Manualne wyzwolenie daily summary (auto-trigger 09:00 UTC):
- Ostatnie 24h: ile docs
- Bot activity events count + estymata kosztów
- MCP server status
- Alerts summary

### `/audit`
Przegląd 20 ostatnich akcji z audit log:
- Format: `[timestamp] @user /action=result (args) ⚠ error`
- Cap 3500 chars (Telegram limit)

### `/breakers`
Stan circuit breakers (`directus`, `mcp`, `anthropic`):
- 🟢 closed (normal operating)
- 🟡 half_open (testing recovery)
- 🔴 open (rejecting requests, recovery in Xs)

### `/limits`
Twoje rate limits per komenda:
- Ile pozostalo z limitu
- Window (60s zwykle)
- Total buckets globalnie

### `/mcp_status`
Szczegółowy status MCP servera:
- vault_last_pulled timestamp
- tools_count
- Repo URL
- Tenant ID

## File handlers (auto)

### Document (PDF/DOCX/XLSX/PPTX/ZIP)
Bot ack z file_name + size + mime_type.

**Idempotency check**: jeśli ten sam plik (sha256 file_id+size) wysłany w ostatniej godzinie → `♻️ Plik X już wysłany - skip dedup`.

Po Worker deploy: plik trafi do INBOX → klasyfikator → Directus + Vault.

### Photo (JPG/PNG)
Bot ack z size.

Po Worker deploy: multimodal AI opisuje → klasyfikator.

### Voice memo
Bot ack z duration.

Po Q3 2026: Whisper transkrypcja → klasyfikator.

## Rate limits

| Komenda | Limit |
|---|---|
| `/start, /help, /health` | 30/min |
| `/produkt, /ostatnie, /ulos_status, /szukaj, /audit, /digest, /koszty` | 10/min |
| `/mcp_status, /mcp_tools` | 10/min |
| `/mcp_szukaj` | 5/min |
| `/breakers, /limits, /dlq` | 30/min |
| Document/Photo/Voice ingest | **3/min** (chroni Anthropic budget) |
| **Globalny** | 60/min na usera |

## Whitelist

Tylko user_id z `ADMIN_CHAT_IDS` env var ma dostęp. Reszta dostaje:
```
Ten bot jest prywatny dla ekosystemu HiveLive.
Twoj user_id: 123456789
Skontaktuj sie z h.gorecki@bidbee.pl jesli potrzebujesz dostepu.
```

Plus audit log entry `result=denied`.

## Audit trail

Każda akcja zapisana w `logs/audit.jsonl`:
```json
{
  "ts": 1778316748.123,
  "iso": "2026-05-09T11:52:28Z",
  "user_id": 6908566796,
  "username": "gregor2708",
  "action": "produkt",
  "args": "PowerHill 261kWh",
  "result": "ok"
}
```

Przeglądaj:
```bash
# Wszystkie loginy w 24h
cat logs/audit.jsonl | jq 'select(.action=="start" and .result=="ok")'

# Statystyki użycia per komenda
cat logs/audit.jsonl | jq -r '.action' | sort | uniq -c

# Failed actions
cat logs/audit.jsonl | jq 'select(.result != "ok")'
```
