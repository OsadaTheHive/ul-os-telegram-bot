# Deployment guide

## Środowiska

### Local development

```bash
# Setup
git clone https://github.com/OsadaTheHive/ul-os-telegram-bot
cd ul-os-telegram-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Config
cp .env.example .env
# Wpisz TELEGRAM_BOT_TOKEN, ADMIN_CHAT_IDS, DIRECTUS_TOKEN, MCP_BEARER_TOKEN

# Run
python -m app.main
```

### Local persistent (launchd, macOS)

```bash
./scripts/install-launchd.sh
# Bot startuje automatycznie, restart po crashie, auto-start po reboot
```

Logi:
```bash
tail -f logs/stdout.log
tail -f logs/audit.jsonl | jq .
```

Status:
```bash
launchctl list | grep ulos.telegram-bot
curl http://127.0.0.1:8080/health
```

Stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
```

### Production: Coolify (project `ul-os`)

Per ADR-002 (UL OS osobny projekt Coolify, oddzielny od `hive-live`).

#### 1. Push repo do GitHub

```bash
# Setup remote
git remote add origin git@github.com:OsadaTheHive/ul-os-telegram-bot.git
git push -u origin main
```

#### 2. Coolify UI setup

1. Coolify → Project `ul-os` → New Resource → Application
2. **Source**: Public Git Repository → `https://github.com/OsadaTheHive/ul-os-telegram-bot`
3. **Build Pack**: Dockerfile
4. **Branch**: main
5. **Port**: 8080 (health endpoint)

#### 3. Environment variables (Coolify)

Dla obu zestawów (preview + production):

```
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_IDS=6908566796,816... (Grzegorz, Hubert)
DIRECTUS_URL=https://cms.osadathehive.pl
DIRECTUS_TOKEN=...
MCP_BASE_URL=https://mcp.bidbee.pl
MCP_BEARER_TOKEN=...
S3_ENDPOINT=https://nbg1.your-objectstorage.com
S3_BUCKET=ul-os-storage
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=nbg1
TENANT_ID=hivelive_ecosystem
SENTRY_DSN=https://....sentry.io/...
SENTRY_ENVIRONMENT=production
LOG_FORMAT=json
```

#### 4. Healthcheck

Coolify auto-detect z Dockerfile `HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/health`. Po 3 fails restart kontenera.

#### 5. Resource limits

```yaml
deploy:
  resources:
    limits:
      memory: 512M
      cpus: '0.5'
    reservations:
      memory: 128M
```

#### 6. Deploy

```
Coolify UI → Deploy
```

Lub via CI/CD (`.github/workflows/ci.yml` posiada step `deploy` który strzela webhook).

## Skalowanie

### Single instance (default)
Bot z long polling = 1 instance. Multiple = race condition na getUpdates (Telegram limitwacja).

### Webhook + multiple instances (przyszłość)
- Telegram → load balancer → N instances
- Każda instance ma własny chat_id whitelist (lub wspólny przez Redis pub/sub)
- Idempotency cache → Redis (zamiast in-memory)

## Monitoring

### Internal (built-in)
- Bot health monitor co 5 min (`app.monitor.tick`)
- Audit log co akcję
- Daily digest 09:00 UTC

### External
- UptimeRobot → `http://46.225.237.196:8080/health` (jeśli expose)
  - Lub przez Cloudflare tunnel: `https://bot.osadathehive.pl/health`
- Sentry → crash reports + performance traces
- Grafana Loki (przyszłość) → structured logs

## Rollback

### Lokalny launchd
```bash
launchctl unload ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
git checkout <previous_commit>
launchctl load ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
```

### Coolify
```
Coolify UI → Application → Deployments → poprzedni success → Redeploy
```

Lub via API:
```bash
curl -X POST -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "$COOLIFY_URL/api/v1/deploy?uuid=$APP_UUID&commit=<sha>&force=true"
```

## Backup + restore

### Auto (production)
Per ADR-009 + scripts/restic-backup.sh:
- Daily backup do HOS bucket `ul-os-storage/backups/`
- Retention: 7d/4w/12mo/5y
- Audit log incremental (każdy nowy event w JSONL)

### Manual (lokalny)
```bash
# Backup
tar -czf bot-backup-$(date +%Y%m%d).tar.gz \
  --exclude='.venv' --exclude='__pycache__' \
  ul-os-telegram-bot/

# Restore
tar -xzf bot-backup-20260509.tar.gz
```

## Secret rotation

Per ADR-012 (90 dni cycle):
```bash
export COOLIFY_TOKEN="<token>"
./scripts/rotate-secret.sh MCP_BEARER_TOKEN
./scripts/rotate-secret.sh OAUTH_CLIENT_SECRET
./scripts/rotate-secret.sh JWT_SIGNING_KEY
```

Cron na VPS:
```
0 3 1 */3 * /usr/local/bin/rotate-secret.sh MCP_BEARER_TOKEN
```

## CI/CD pipeline

`.github/workflows/ci.yml` (już skonfigurowane):

1. **test** (każdy push/PR)
   - Setup Python 3.12
   - `ruff check` + `ruff format --check`
   - `pytest tests/` z mock env

2. **build-docker** (main branch only, after test)
   - `docker build` z multi-stage
   - Smoke test (spawn container 10s)

3. **deploy** (main branch only, after build)
   - Webhook do Coolify trigger redeploy

GitHub Secrets needed:
- `COOLIFY_DEPLOY_WEBHOOK` - webhook URL
- `COOLIFY_TOKEN` - API token
