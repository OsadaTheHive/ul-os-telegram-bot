#!/usr/bin/env bash
# Quick & dirty: nasluchuj na /start od Huberta i Grzegorza,
# wypisz chat_id zeby dodac do ADMIN_CHAT_IDS w .env.
#
# Uruchom: ./scripts/get_chat_ids.sh
# Potem: napisz /start do @ulos_worker_bot z Telegrama
#
# Skrypt pokaze chat_id, ktore mozna wstawic do .env.

set -euo pipefail

# Wczytaj token z .env
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN brak w .env"
  exit 1
fi

OFFSET=0
echo "Czekam na /start od Ciebie i Huberta..."
echo "Bot: @ulos_worker_bot"
echo "Wcisnij Ctrl+C zeby zakonczyc."
echo ""

while true; do
  RESPONSE=$(curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates?offset=$OFFSET&timeout=30")

  COUNT=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('result',[])))")

  if [ "$COUNT" -gt 0 ]; then
    echo "$RESPONSE" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for upd in d.get('result', []):
    msg = upd.get('message') or upd.get('edited_message') or {}
    user = msg.get('from', {})
    chat = msg.get('chat', {})
    text = msg.get('text', '')
    print(f\"--- update {upd['update_id']} ---\")
    print(f\"  user_id:  {user.get('id')}\")
    print(f\"  username: @{user.get('username','-')}\")
    print(f\"  name:     {user.get('first_name','')} {user.get('last_name','') or ''}\".rstrip())
    print(f\"  chat_id:  {chat.get('id')}\")
    print(f\"  text:     {text[:200]}\")
    print()
"
    # Advance offset to ack
    LAST=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); ids=[u['update_id'] for u in d.get('result',[])]; print(max(ids) if ids else 0)")
    OFFSET=$((LAST + 1))
  fi
done
