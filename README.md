# UL OS Telegram Bot — `@ulos_worker_bot`

Telegram bot dla ekosystemu HiveLive. Primary interface dla Huberta + Grzegorza do:

- File ingest (PDF, foto, voice memo) → Worker UL OS klasyfikuje → Directus + Vault
- Komendy: `/szukaj`, `/produkt`, `/ostatnie`, `/health`, `/ulos_status`
- Powiadomienia administracyjne (rate limit hit, błędy klasyfikatora, backup failed)

## Stack

- **python-telegram-bot v21+** (asyncio, long-polling lub webhook)
- **httpx** — async HTTP do Directus, Worker, B2
- **pydantic-settings** — env config

## Architektura (per ADR-006 z `UL_OS_infrastructure_v1.md`)

```
┌────────────┐
│  Telegram  │
│   Cloud    │
└─────┬──────┘
      │ long polling (PL/WAW → Telegram DC)
      │ albo webhook https://bot.osadathehive.pl
      ▼
┌─────────────────────┐
│  ul-os-telegram-bot │ ← Coolify Docker container
│  (Python 3.12)      │
│                     │
│  - whitelist auth   │
│  - command router   │
│  - file forwarder   │
└──────┬───────┬──────┘
       │       │
       ▼       ▼
  Worker UL OS  Directus 11
  (Q2 2026)     (cms.osadathehive.pl)
```

## Quick start (lokalnie)

```bash
# 1. Setup
git clone https://github.com/OsadaTheHive/ul-os-telegram-bot
cd ul-os-telegram-bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Env
cp .env.example .env
# Wpisz TELEGRAM_BOT_TOKEN i ADMIN_CHAT_IDS

# 3. Run
python -m app.main
```

## Deployment do Coolify (Tier 0+ wg roadmapy)

```bash
# 1. Push do GitHub
git remote add origin git@github.com:OsadaTheHive/ul-os-telegram-bot.git
git push -u origin main

# 2. W Coolify UI:
#    - New Application → Public Repository
#    - Repository: OsadaTheHive/ul-os-telegram-bot
#    - Build pack: Dockerfile
#    - Environment variables: skopiowac z .env.local (NIE commitowac)
#    - Domains: brak (long-polling, nie wymaga publicznego URL)
#    - LUB jezeli webhook: bot.osadathehive.pl + USE_WEBHOOK=true
```

## Bezpieczeństwo

- **Whitelist `ADMIN_CHAT_IDS`** — tylko Hubert + Grzegorz mogą używać. Reszta dostaje polite NO + log.
- **Token w env**, nigdy w kodzie. `.env` w `.gitignore`.
- **Rotation** każde 90 dni przez `/revoke` + `/token` w `@BotFather`.
- **Webhook secret** — gdy USE_WEBHOOK=true, weryfikuje że request idzie z Telegram.

## Komendy bota

| Komenda | Opis | Status |
|---|---|---|
| `/start` | powitanie + sprawdzenie dostępu | ✅ MVP |
| `/help` | lista komend | ✅ MVP |
| `/health` | status Worker + Directus + B2 | ✅ MVP |
| `/szukaj <q>` | semantic search (pgvector) | 🚧 Q3 2026 |
| `/produkt <n>` | dane z `beezzy_products` | ✅ MVP |
| `/ostatnie` | top 10 ostatnich `knowledge_items` | ✅ MVP |
| `/ulos_status` | counts per brand + koszty API | ✅ MVP (bez kosztów) |

## File handlers

| Typ | Działanie | Status |
|---|---|---|
| Document (PDF/DOCX/XLSX/PPTX/ZIP) | ack + forward do Worker → INBOX | 🚧 Q2 |
| Photo (JPG/PNG) | klasyfikator + multimodal AI description | 🚧 Q2 |
| Voice memo | Whisper transcription → klasyfikator | 🚧 Q3 |

## Roadmapa (vs `UL_OS_infrastructure_v1.md`)

- **Q2 2026 (maj-czerwiec)** — long-polling MVP, file ingest do Worker, podstawowe komendy
- **Q3 2026 (lipiec-wrzesień)** — Whisper voice memos, semantic search, /social `<brand>` (rolki Instagram)
- **Q4 2026** — multi-tenant: per-tenant bot tokens, dynamic command set z bazy
