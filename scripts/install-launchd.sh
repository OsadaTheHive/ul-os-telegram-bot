#!/usr/bin/env bash
# Instaluje launchd auto-start dla UL OS Telegram Bota.
# Bot bedzie chodzic w tle, auto-restart przy crash, auto-start po restartcie Maca.
# Logs: ~/dev/ul-os-telegram-bot/logs/stdout.log + stderr.log
#
# Uruchom raz: ./scripts/install-launchd.sh
# Wylacz: launchctl unload ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
# Status: launchctl list | grep ulos

set -euo pipefail

REPO="/Users/grzegorzgoldyn/dev/ul-os-telegram-bot"
PLIST_SOURCE="$REPO/scripts/com.ulos.telegram-bot.plist"
PLIST_TARGET="$HOME/Library/LaunchAgents/com.ulos.telegram-bot.plist"

# Sanity checks
[ -d "$REPO/.venv" ] || { echo "ERROR: $REPO/.venv nie istnieje. Uruchom najpierw: python3.11 -m venv .venv && pip install -r requirements.txt"; exit 1; }
[ -f "$REPO/.env" ] || { echo "ERROR: $REPO/.env brak."; exit 1; }

# Logs dir
mkdir -p "$REPO/logs"

# Killuj wszystkie biegające instancje (jakkolwiek zostały uruchomione)
echo "Killuje istniejace instancje bota..."
pkill -f "python.*-m app.main" 2>/dev/null || true
sleep 2

# Skopiuj plist
mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SOURCE" "$PLIST_TARGET"
echo "Plist skopiowany: $PLIST_TARGET"

# Załaduj
launchctl unload "$PLIST_TARGET" 2>/dev/null || true
launchctl load "$PLIST_TARGET"
echo "launchd zaladowany. Bot bedzie chodzil w tle."

# Verify po 3s
sleep 3
echo ""
echo "=== STATUS ==="
launchctl list | grep ulos.telegram-bot || echo "BRAK w launchctl - sprawdz logs"
echo ""
echo "=== LOGS (ostatnie 5 linii stdout) ==="
tail -5 "$REPO/logs/stdout.log" 2>/dev/null || echo "(brak logu jeszcze)"
echo ""
echo "Bot powinien teraz dzialac w tle. Test:"
echo "  - Napisz /help do @ulos_worker_bot na Telegramie"
echo "  - Tail logu: tail -f $REPO/logs/stdout.log"
echo "  - Stop: launchctl unload $PLIST_TARGET"
