# UL OS Telegram Bot — Architektura

**Last updated**: 2026-05-09

## Cel

Bot Telegram jako **primary interface** dla Huberta + Grzegorza do ekosystemu HiveLive:
- File ingest (PDF/foto/voice memo) → Worker UL OS klasyfikuje → Directus + Vault
- Komendy zarządzania (/health, /koszty, /digest, /audit, /breakers, /limits)
- Search po Vault i Directus (`/szukaj`, `/mcp_szukaj`, `/produkt`, `/ostatnie`)
- Powiadomienia administracyjne (rate limit hit, błędy klasyfikatora, MCP/Directus pad)

## Komponenty

```
┌──────────────────────────────────────────────────────────────────┐
│                        TELEGRAM CLOUD                            │
│  ┌──────────────────────────┐  ┌─────────────────────────────┐ │
│  │ User (Hubert, Grzegorz)  │  │ Bot @ulos_worker_bot         │ │
│  └────────────┬─────────────┘  └─────────────┬───────────────┘ │
└───────────────┼───────────────────────────────┼─────────────────┘
                │ messages                      │ getUpdates / sendMessage
                │ (long polling)                │
                ▼                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                       UL OS TELEGRAM BOT                         │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Application (python-telegram-bot 21.6, asyncio)            │ │
│  │                                                            │ │
│  │  ┌──────────────────────────────────────────────────────┐ │ │
│  │  │ Middleware: authorized_or_ignore                     │ │ │
│  │  │  1. Whitelist auth (ADMIN_CHAT_IDS)                 │ │ │
│  │  │  2. Rate limit (per user × per command)             │ │ │
│  │  │  3. Audit log (JSONL append)                        │ │ │
│  │  └──────────────────────────────────────────────────────┘ │ │
│  │                                                            │ │
│  │  Handlers:                                                 │ │
│  │  • Commands: /start /help /health /szukaj /produkt ...    │ │
│  │  • Files: document, photo, voice (z idempotency check)    │ │
│  │                                                            │ │
│  │  Background jobs (JobQueue):                               │ │
│  │  • Health monitor (5 min) - sprawdza Directus + MCP       │ │
│  │  • Daily digest (09:00 UTC) - statystyki do whitelist     │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ HTTP Health Server (aiohttp, port 8080)                    │ │
│  │  GET /health  - status JSON (200 ok / 503 degraded)        │ │
│  │  GET /metrics - Prometheus text format                     │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Stability layer:                                                │
│  • Circuit breakers (directus, mcp, anthropic)                  │
│  • Idempotency cache (LRU, TTL 1h, file ingest dedup)          │
│  • Anti-flapping monitor (alert po 2 fails)                     │
│                                                                  │
│  Observability:                                                  │
│  • Sentry (gdy DSN) - error tracking + performance              │
│  • JSON structured logs (LOG_FORMAT=json)                       │
│  • Audit log (logs/audit.jsonl - kazda akcja)                  │
│  • Rate limit stats (in-memory)                                 │
└──────────────────────────────────────────────────────────────────┘
                │                                │
                │ httpx (z circuit breakers)     │
                ▼                                ▼
┌─────────────────────────┐  ┌──────────────────────────────────┐
│  Directus REST API       │  │  MCP Server (mcp.bidbee.pl)      │
│  cms.osadathehive.pl     │  │                                  │
│                          │  │  5 tools (Streamable HTTP):      │
│  • knowledge_items       │  │  • vault_search                  │
│  • beezzy_products       │  │  • vault_read                    │
│  • dokumenty_cp          │  │  • recent_changes                │
│  • monet_* (read-only    │  │  • directus_query                │
│    for kuzyn scope)      │  │  • vault_write (z auto-commit)   │
│                          │  │                                  │
│  Bearer token auth       │  │  OAuth 2.1 + PKCE + Bearer       │
└─────────────────────────┘  └──────────────────────────────────┘
                                            │
                                            ▼
                              ┌──────────────────────────────────┐
                              │  GitHub OsadaTheHive/HiveLive_    │
                              │  Vault (git push from MCP)        │
                              └──────────────────────────────────┘
```

## Stack

| Warstwa | Technologia | Wersja |
|---|---|---|
| Runtime | Python | 3.11+ |
| Framework Telegram | python-telegram-bot | 21.6 |
| HTTP client | httpx (async) | 0.27.2 |
| HTTP server | aiohttp (health endpoint) | 3.10.10 |
| Config | pydantic-settings | 2.6.1 |
| Schedulers | APScheduler (przez ptb job-queue) | 3.x |
| Error tracking | sentry-sdk | 2.18.0 (optional) |
| Tests | pytest + pytest-asyncio | 8.x + 0.24 |
| Lint | ruff | 0.7.x |

## Komponenty kodu

```
app/
├── __init__.py
├── main.py                  # Entry point, Application builder, command registration
├── config.py                # Settings (env-driven, pydantic)
├── handlers.py              # All command handlers + file handlers
├── audit.py                 # JSONL audit log writer
├── limiter.py               # Sliding window rate limiter (per user × command)
├── monitor.py               # Background health monitor (anti-flapping)
├── breaker.py               # Circuit breaker (directus/mcp/anthropic)
├── idempotency.py           # LRU cache for file ingest dedup
├── health_endpoint.py       # aiohttp /health + /metrics server
├── observability.py         # Sentry init + JSON logging setup
└── services/
    ├── __init__.py
    ├── usage_stats.py       # /koszty stats from audit + Directus
    ├── dlq.py               # /dlq placeholder (Worker DLQ integration)
    └── mcp_client.py        # JSON-RPC over Streamable HTTP MCP client

tests/
├── test_audit.py            (6)
├── test_breaker.py          (9)
├── test_config.py           (5)
├── test_idempotency.py      (9)
├── test_limiter.py          (8)
└── test_services.py         (5)
                             ===
                             42 tests
```

## Komendy bota (16)

### Podstawowe (3)
- `/start` — auth check + powitanie
- `/help` — lista komend
- `/health` — status systemu (Directus, MCP, breakers)

### Search (3)
- `/szukaj <query>` — full-text Directus po title/content/summary
- `/mcp_szukaj <query>` — Vault search przez MCP vault_search tool
- `/mcp_tools` — lista 5 tools wystawionych przez MCP

### Directus query (3)
- `/produkt <nazwa>` — info BEEzzy (z beezzy_products)
- `/ostatnie` — 10 najnowszych knowledge_items
- `/ulos_status` — counts per brand

### Diagnostyka admin (5)
- `/koszty [days]` — estymata API + ingest stats
- `/dlq` — failed ingests (placeholder do Worker DLQ)
- `/digest` — daily summary (manual trigger)
- `/audit` — przegląd 20 ostatnich akcji
- `/breakers` — circuit breakers state
- `/limits` — twoje rate limits per komenda

### MCP advanced (1)
- `/mcp_status` — vault sync info, tools count

### Files (auto-handled, 3)
- Document (PDF/DOCX/...) — z idempotency dedup
- Photo — placeholder dla multimodal AI
- Voice — placeholder dla Whisper transkrypcji

## Bezpieczeństwo

### Authentication
- **Whitelist** (`ADMIN_CHAT_IDS`) - tylko Hubert + Grzegorz
- Każda nieautoryzowana próba → audit log + polite NO

### Rate limiting
- Per user × per command (sliding window)
- Lekkie komendy: 30/min
- Heavy queries (Directus): 10/min
- File ingest (Anthropic): 3/min
- Globalny: 60/min/user

### Audit
- JSONL append-only (`logs/audit.jsonl`)
- Każda akcja: timestamp, user, action, args, result, error
- W przyszłości: persist do Directus tabela `audit_log` (per ADR-014 proposal)

### Secrets
- W `.env` lokalnie (gitignored)
- W Coolify env vars (production)
- Rotation: scripts/rotate-secret.sh (per ADR-012, 90 dni cycle)

## Niezawodność

### Stability
- **Circuit breakers** (3): directus, mcp, anthropic
- **Idempotency keys** dla file ingest (chroni przed dup costs)
- **Anti-flapping** w health monitor (alert po 2 fails)
- **Auto-restart** przez launchd (lokalnie) / Coolify (production)
- **Graceful shutdown** (tini w Dockerfile + SIGTERM handler)

### Self-healing
- Coolify healthcheck (curl /health) → restart on fail
- Circuit breaker recovery (HALF_OPEN → CLOSED testing)
- Idempotency cache TTL eviction

### Observability
- HTTP /health (UptimeRobot, external monitor)
- Sentry crash reporting
- JSON structured logs (Loki ready)
- Daily digest (auto-trigger 09:00 UTC)

## Performance

### Targets (per FILAR 6 z planu autonomii)
- Directus query: <100ms p95
- MCP call: <2s p95
- Telegram bot response: <2s p95 (text), <10s p95 (file processing)

### Resource limits (Coolify production)
- Memory: 256-512 MB
- CPU: 0.25-0.5
- Network: minimal (long polling Telegram + occasional Directus/MCP calls)

## Deployment modes

### Mode 1: Long polling (default, dziś)
```python
USE_WEBHOOK=false
```
Bot łączy się z Telegram cloud co ~10s przez `getUpdates`. Nie wymaga publicznego endpointu HTTP.

### Mode 2: Webhook (production przyszłość)
```python
USE_WEBHOOK=true
WEBHOOK_URL=https://bot.osadathehive.pl/telegram/webhook
WEBHOOK_SECRET=<random>
```
Telegram pcha messages do bota przez HTTPS. Wymaga publicznego endpointu z SSL (Cloudflare WAF rekomendowany).

## Disaster recovery

### Bot crash
- Coolify auto-restart (max 3×/h, potem alert)
- Auto-resume long polling (Telegram dostarcza missed messages)

### Directus pad
- Circuit breaker opens po 3 fail
- Bot odpowiada: "⚠ Directus circuit breaker: ..."
- Auto-recovery po cooldown (60s)

### MCP pad
- Identycznie jak Directus, breaker name "mcp"

### Anthropic API outage
- Circuit breaker (cooldown 120s)
- Bot odpowiada graceful error
- Worker fallback (rule-based) — TODO Sprint 1

### VPS pad
- Coolify znowu wstaje (jeśli VPS żyje)
- Long polling auto-recover po przywróceniu
- Health monitor po 2 fails alertuje admina (ale jeśli bot nie żyje, alert nie pójdzie - dlatego potrzebujemy UptimeRobot, runbook)
