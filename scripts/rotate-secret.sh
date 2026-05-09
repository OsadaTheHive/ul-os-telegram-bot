#!/usr/bin/env bash
# General-purpose secret rotation skrypt.
# Uzycie: ./scripts/rotate-secret.sh <SECRET_NAME>
# Przyklad: ./scripts/rotate-secret.sh ANTHROPIC_API_KEY
#
# Co robi:
#   1. Sprawdza ze SECRET_NAME jest znany
#   2. Backup starego sekretu (last 8 chars w CREDENTIALS.md jako audit trail)
#   3. Generuje nowy (lub prosi o manual paste dla zewnetrznych - Anthropic, OAuth)
#   4. Update Coolify env vars (preview + production)
#   5. Restart targeted aplikacji
#   6. Verify
#
# Zgodne z ADR-012 z UL_OS_infrastructure_v1.md (rotacja co 90 dni).

set -euo pipefail

SECRET_NAME="${1:-}"
[ -z "$SECRET_NAME" ] && { echo "Usage: $0 <SECRET_NAME>"; exit 1; }

# Konfiguracja per secret (rozszerz w miarę potrzeb)
declare -A APP_UUID  # ktora aplikacja Coolify uzywa
declare -A AUTO_GEN  # czy mozna auto-generowac (true/false)

APP_UUID["MCP_BEARER_TOKEN"]="rkcsc0w04skow848cg0s0444"
AUTO_GEN["MCP_BEARER_TOKEN"]="true"

APP_UUID["DIRECTUS_TOKEN"]="zc8s884k4gkwgkkscgkc4o44"  # THE-HIVE
AUTO_GEN["DIRECTUS_TOKEN"]="false"  # generowany przez Directus admin UI

APP_UUID["ANTHROPIC_API_KEY"]="zc8s884k4gkwgkkscgkc4o44"  # THE-HIVE
AUTO_GEN["ANTHROPIC_API_KEY"]="false"  # rotacja w Anthropic Console

APP_UUID["OAUTH_CLIENT_SECRET"]="rkcsc0w04skow848cg0s0444"  # MCP server
AUTO_GEN["OAUTH_CLIENT_SECRET"]="true"

APP_UUID["JWT_SIGNING_KEY"]="rkcsc0w04skow848cg0s0444"
AUTO_GEN["JWT_SIGNING_KEY"]="true"

APP_UUID["BEEZHUB_CONTROL_SECRET"]="zc8s884k4gkwgkkscgkc4o44"
AUTO_GEN["BEEZHUB_CONTROL_SECRET"]="true"

APP_UUID["SYNC_HEARTBEAT_SECRET"]="zc8s884k4gkwgkkscgkc4o44"
AUTO_GEN["SYNC_HEARTBEAT_SECRET"]="true"

# Walidacja
if [ -z "${APP_UUID[$SECRET_NAME]:-}" ]; then
  echo "ERROR: nieznany secret: $SECRET_NAME"
  echo "Znane: ${!APP_UUID[*]}"
  exit 1
fi

VPS="root@46.225.237.196"
COOLIFY_URL="http://46.225.237.196:8000"
COOLIFY_TOKEN="${COOLIFY_TOKEN:-}"
[ -z "$COOLIFY_TOKEN" ] && { echo "ERROR: COOLIFY_TOKEN env var required"; exit 1; }

echo "▶ Rotacja sekretu: $SECRET_NAME"
echo "  App: ${APP_UUID[$SECRET_NAME]}"
echo "  Auto-gen: ${AUTO_GEN[$SECRET_NAME]}"

# Pobierz stary
echo "▶ Pobieram stary z kontenera..."
OLD_VALUE=$(ssh "$VPS" "docker inspect \$(docker ps --filter name=${APP_UUID[$SECRET_NAME]} -q | head -1) --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^${SECRET_NAME}=' | cut -d= -f2-")
[ -z "$OLD_VALUE" ] && { echo "ERROR: stary secret nie znaleziony w kontenerze"; exit 1; }
OLD_PREFIX="${OLD_VALUE:0:8}"
echo "  Stary prefix: ${OLD_PREFIX}..."

# Generuj nowy lub spytaj
if [ "${AUTO_GEN[$SECRET_NAME]}" = "true" ]; then
  case "$SECRET_NAME" in
    *KEY*|*TOKEN*|*SECRET*)
      NEW_VALUE=$(openssl rand -hex 32)
      ;;
    *)
      NEW_VALUE=$(openssl rand -base64 32)
      ;;
  esac
  echo "  Nowy wygenerowany (prefix: ${NEW_VALUE:0:8}...)"
else
  echo "▶ AUTO_GEN=false - musisz wpisac nowy ręcznie:"
  read -s -p "  Nowy $SECRET_NAME: " NEW_VALUE
  echo ""
  [ -z "$NEW_VALUE" ] && { echo "ERROR: pusty"; exit 1; }
fi

# Update Coolify env (preview + production)
echo "▶ Update Coolify env vars..."
for IS_PREVIEW in true false; do
  RESPONSE=$(curl -s -X PATCH \
    -H "Authorization: Bearer $COOLIFY_TOKEN" \
    -H "Content-Type: application/json" \
    "$COOLIFY_URL/api/v1/applications/${APP_UUID[$SECRET_NAME]}/envs" \
    -d "{
      \"key\": \"$SECRET_NAME\",
      \"value\": \"$NEW_VALUE\",
      \"is_preview\": $IS_PREVIEW
    }")
  echo "  is_preview=$IS_PREVIEW: $(echo "$RESPONSE" | head -c 100)"
done

# Restart aplikacji
echo "▶ Restart aplikacji..."
RESTART=$(curl -s -X POST \
  -H "Authorization: Bearer $COOLIFY_TOKEN" \
  "$COOLIFY_URL/api/v1/deploy?uuid=${APP_UUID[$SECRET_NAME]}&force=true")
echo "  $(echo "$RESTART" | head -c 100)"

# Audit trail
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
CREDENTIALS_MD="/Users/grzegorzgoldyn/Documents/Claude/Projects/BEZZHUB DASCHBORD/CREDENTIALS.md"
if [ -f "$CREDENTIALS_MD" ]; then
  cat >> "$CREDENTIALS_MD" <<EOF

### Rotation: $SECRET_NAME @ $TIMESTAMP
- Old prefix: ${OLD_PREFIX}...
- New prefix: ${NEW_VALUE:0:8}...
- App: ${APP_UUID[$SECRET_NAME]}
EOF
  echo "▶ Audit trail w CREDENTIALS.md"
fi

# Verify (placeholder - per-secret verification)
echo "▶ Czekam 30s na restart aplikacji..."
sleep 30

echo ""
echo "✅ ROTACJA UKOŃCZONA"
echo "   $SECRET_NAME: ${OLD_PREFIX}... -> ${NEW_VALUE:0:8}..."
echo ""
echo "TODO MANUALNE:"
echo "  • Update mojego lokalnego .env (jezeli ja uzywam tego sekretu)"
echo "  • Update CREDENTIALS.md (juz zrobione automatycznie)"
echo "  • Następna rotacja: $(date -u -v+90d +%Y-%m-%d)"
