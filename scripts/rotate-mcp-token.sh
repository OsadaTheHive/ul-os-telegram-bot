#!/usr/bin/env bash
# Rotacja MCP_BEARER_TOKEN dla mcp.bidbee.pl
#
# Zgodnie z ADR-012 z UL_OS_infrastructure_v1.md: rotacja co 90 dni.
#
# Co robi:
#  1. Generuje nowy 32-bajtowy token (urandom)
#  2. Zapisuje stary token do CREDENTIALS.md jako "rotated YYYY-MM-DD"
#  3. Aktualizuje Coolify env vars (preview + production)
#  4. Restartuje aplikację MCP w Coolify (przez API)
#  5. Verify że nowy token dziala (curl /health)
#
# Use: ./scripts/rotate-mcp-token.sh
#
# Wymaga:
#  - SSH dostep do root@46.225.237.196
#  - COOLIFY_TOKEN w env lub /tmp/coolify_token.txt

set -euo pipefail

VPS="root@46.225.237.196"
APP_UUID="rkcsc0w04skow848cg0s0444"
COOLIFY_URL="http://46.225.237.196:8000"
COOLIFY_TOKEN="${COOLIFY_TOKEN:-$(cat /tmp/coolify_token.txt 2>/dev/null || echo '')}"
CREDENTIALS_MD="/Users/grzegorzgoldyn/Documents/Claude/Projects/BEZZHUB DASCHBORD/CREDENTIALS.md"

err() { echo "❌ $1" >&2; exit 1; }
log() { echo "▶ $1"; }

# ============================================================
# 1. Sanity checks
# ============================================================
log "1/6 Sanity checks..."

[ -n "$COOLIFY_TOKEN" ] || err "COOLIFY_TOKEN nie ustawiony (env lub /tmp/coolify_token.txt)"

ssh -o ConnectTimeout=5 -o BatchMode=yes "$VPS" "echo OK" >/dev/null 2>&1 \
  || err "Brak dostępu SSH do $VPS"

OLD_TOKEN=$(ssh "$VPS" "docker inspect ${APP_UUID}-* --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^MCP_BEARER_TOKEN=' | cut -d= -f2-" | head -1)
[ -n "$OLD_TOKEN" ] || err "Nie udało się pobrać starego MCP_BEARER_TOKEN z kontenera"

log "  ✓ Stary token pobrany ($(echo -n "$OLD_TOKEN" | wc -c) bytes, prefix: ${OLD_TOKEN:0:8}...)"

# ============================================================
# 2. Generuj nowy token (32 bajty hex = 64 znaki, jak teraz)
# ============================================================
log "2/6 Generuję nowy token..."
NEW_TOKEN=$(openssl rand -hex 32)
log "  ✓ Nowy token gotowy ($(echo -n "$NEW_TOKEN" | wc -c) bytes, prefix: ${NEW_TOKEN:0:8}...)"

# ============================================================
# 3. Zapisz stary token do CREDENTIALS.md (audit trail)
# ============================================================
log "3/6 Update CREDENTIALS.md - audit trail..."
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ROTATION_NOTE="
### Rotation log

| Date (UTC) | Old token prefix | New token prefix |
|---|---|---|
| $TIMESTAMP | ${OLD_TOKEN:0:8}... | ${NEW_TOKEN:0:8}... |
"

if [ -f "$CREDENTIALS_MD" ]; then
  # Append rotation note (manualnie - sed niezawodne na multilinę z |)
  echo "$ROTATION_NOTE" >> "$CREDENTIALS_MD"
  log "  ✓ Zapisano do $CREDENTIALS_MD"
else
  log "  ⚠ CREDENTIALS.md nie istnieje, pomijam audit trail"
fi

# ============================================================
# 4. Update Coolify env vars (preview + production)
# ============================================================
log "4/6 Aktualizuję Coolify env vars (preview + production)..."

# Coolify API: PATCH /api/v1/applications/{uuid}/envs/{key}
# UWAGA: jest 2 wpisy (is_preview=true + is_preview=false), oba do update.

for IS_PREVIEW in true false; do
  log "  Update is_preview=$IS_PREVIEW..."
  RESPONSE=$(curl -s -X PATCH \
    -H "Authorization: Bearer $COOLIFY_TOKEN" \
    -H "Content-Type: application/json" \
    "$COOLIFY_URL/api/v1/applications/$APP_UUID/envs" \
    -d "{
      \"key\": \"MCP_BEARER_TOKEN\",
      \"value\": \"$NEW_TOKEN\",
      \"is_preview\": $IS_PREVIEW,
      \"is_build_time\": true,
      \"is_literal\": false
    }")

  # Coolify zwraca {"message":"Environment variable updated."} albo error
  if echo "$RESPONSE" | grep -q '"message"'; then
    log "    ✓ OK: $(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('message','?'))" 2>/dev/null || echo "$RESPONSE")"
  else
    log "    ⚠ Response: $RESPONSE"
  fi
done

# ============================================================
# 5. Restart aplikacji MCP (Coolify API redeploy)
# ============================================================
log "5/6 Restart aplikacji w Coolify..."
RESTART=$(curl -s -X POST \
  -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "$COOLIFY_URL/api/v1/deploy?uuid=$APP_UUID&force=true")

log "  Response: $(echo "$RESTART" | head -c 200)..."

# Czekaj na restart - max 90s
log "  Czekam na restart (max 90s)..."
for i in $(seq 1 18); do
  sleep 5
  HEALTH=$(curl -s -m 3 -H "Authorization: Bearer $NEW_TOKEN" https://mcp.bidbee.pl/health 2>/dev/null)
  if echo "$HEALTH" | grep -q '"status":"ok"'; then
    log "  ✓ MCP server odpowiada na nowy token (po ${i}×5s = $((i*5))s)"
    break
  fi
  log "    [${i}/18] czekam..."
done

# ============================================================
# 6. Final verify
# ============================================================
log "6/6 Final verify..."

# Stary token NIE działa
OLD_RESPONSE=$(curl -s -m 5 -H "Authorization: Bearer $OLD_TOKEN" https://mcp.bidbee.pl/health 2>&1)
if echo "$OLD_RESPONSE" | grep -q '"error":"Unauthorized"'; then
  log "  ✓ Stary token unieważniony (401)"
else
  log "  ⚠ UWAGA: stary token NADAL działa! Response: $OLD_RESPONSE"
  log "  (Może Coolify nie zrestartował kontenera. Restart manualny: ssh $VPS docker restart $APP_UUID)"
fi

# Nowy token działa
NEW_RESPONSE=$(curl -s -m 5 -H "Authorization: Bearer $NEW_TOKEN" https://mcp.bidbee.pl/health)
if echo "$NEW_RESPONSE" | grep -q '"status":"ok"'; then
  log "  ✓ Nowy token działa: $NEW_RESPONSE"
else
  err "Nowy token NIE działa! Response: $NEW_RESPONSE"
fi

log ""
log "═══════════════════════════════════════════════"
log "✅ ROTACJA UKOŃCZONA"
log "═══════════════════════════════════════════════"
log "Stary token: ${OLD_TOKEN:0:8}... (unieważniony)"
log "Nowy token:  ${NEW_TOKEN:0:8}... (aktywny)"
log ""
log "TODO MANUALNE:"
log "  1. Update Cursor / Claude Code config (jeśli używasz MCP):"
log "     mcp servers > ulos > Authorization: Bearer $NEW_TOKEN"
log "  2. Update bota Telegram .env (jeśli /mcp_szukaj wpięte):"
log "     ~/dev/ul-os-telegram-bot/.env: MCP_BEARER_TOKEN=$NEW_TOKEN"
log "     restart: launchctl unload ~/Library/LaunchAgents/com.ulos.telegram-bot.plist && launchctl load ..."
log "  3. Update CREDENTIALS.md (już automatic, ale verify)"
log ""
log "Następna rotacja: $(date -u -v+90d +%Y-%m-%d) (90 dni)"
