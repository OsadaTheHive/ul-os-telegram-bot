# Troubleshooting

## Bot nie odpowiada

### Check 1: Czy proces żyje?
```bash
launchctl list | grep ulos.telegram-bot
# Lub w Coolify:
docker ps | grep ul-os-telegram-bot
```

Jeśli brak → restart:
```bash
launchctl load ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
# Coolify: redeploy w UI
```

### Check 2: Czy bot odpowiada na getMe?
```bash
curl https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe
```
- 200 OK z bot info → token OK
- 401 → token unieważniony (rotacja przez @BotFather)
- timeout → network problem

### Check 3: Czy /health odpowiada?
```bash
curl http://127.0.0.1:8080/health
```
- 200 → wszystko ok
- 503 → degraded (sprawdź `circuit_breakers` w response)
- connection refused → bot nie startuje (logs)

### Check 4: Logi
```bash
tail -100 ~/dev/ul-os-telegram-bot/logs/stdout.log
tail -100 ~/dev/ul-os-telegram-bot/logs/stderr.log
```

Typowe błędy:
- `pydantic_core._pydantic_core.ValidationError` → config error, sprawdź `.env`
- `telegram.error.Conflict: terminated by other getUpdates` → 2 instancje bota równocześnie, kill jedną
- `httpx.ConnectError` → Directus / MCP nieosiągalne

## Bot odpowiada "Brak uprawnień"

User_id nie w whitelist (`ADMIN_CHAT_IDS`).

### Fix
1. User pisze `/start` (audit log capture jego user_id)
2. ```bash
   tail logs/audit.jsonl | jq 'select(.action=="start") | .user_id'
   ```
3. Dodaj do `ADMIN_CHAT_IDS` w `.env`
4. Restart bot:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
   launchctl load ~/Library/LaunchAgents/com.ulos.telegram-bot.plist
   ```

## Circuit breaker OPEN

`/breakers` pokazuje `directus: open` lub podobne.

### Check
```bash
# Test bezpośrednio Directus
curl https://cms.osadathehive.pl/server/health
# 200 → Directus OK, problem może być po stronie bota network
# Inne → Directus down
```

### Recovery
- Auto: po `recovery_timeout` (60s dla Directus/MCP, 120s dla Anthropic)
- Manual: restart bot (resetuje wszystkie breakers do CLOSED)

## "Już wysłany" przy ponownym uploadzie pliku

Idempotency cache zatrzymał duplikat (TTL 1h).

Jest to **celowe** — chroni przed duplikatami w audit + Anthropic costs.

Jeśli **chcesz** re-upload tego samego pliku:
1. Poczekaj 1h (TTL cache wygasa)
2. Lub restart bota (cache reset)
3. Lub zmień zawartość (sha256 inny)

## /mcp_szukaj zwraca błąd "Already connected"

(Stary bug Huberta MCP server, naprawiony 2026-05-09 commit `cec982b`).

Jeśli wciąż występuje:
- Hubert ma aktywną sesję MCP w Cursor / Claude.ai → musi zamknąć
- Lub redeploy MCP server (Coolify project `ul-os`)

## Rate limit hit

`/limits` pokazuje 0 pozostałych dla danej komendy.

### Co to znaczy
Spam check zadziałał — anti-flood. Poczekaj okno (zwykle 60s).

### Konfiguracja
`app/limiter.py` → `LIMITS` dict:
```python
LIMITS = {
    "produkt": (10, 60),  # 10 / 60s
    ...
}
```

Edycja wymaga restartu bota.

## Health monitor wysyła false positive

Bot wysyła `🔴 DIRECTUS pad` ale Directus żyje.

### Causes
- Network glitch (chwilowy timeout)
- Directus restart (Coolify redeploy)
- DNS hiccup

### Tuning
`app/monitor.py`:
```python
ALERT_AFTER_FAILURES = 2   # default 2 (= 10 min downtime)
ALERT_COOLDOWN = 1800       # 30 min między alertami
```

Zwiększ `ALERT_AFTER_FAILURES` do 3 jeśli false positives częste.

## Daily digest się nie wysłał (09:00 UTC)

### Check
```bash
grep "daily_digest" logs/stdout.log | tail -5
```

Jeśli brak:
- Bot nie chodził o 09:00 (sprawdź launchctl uptime)
- JobQueue nie zarejestrowane (sprawdź startup log: `Daily digest scheduled`)

### Manual trigger
```
/digest
```

## Tests fail

```bash
pytest tests/ -v
```

### Typowe problemy

**`ValidationError: TELEGRAM_BOT_TOKEN required`**
→ Test config: dodaj `monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")`

**`ImportError`**
→ pip install -r requirements.txt

**Asyncio test fails: `RuntimeError: ... no current event loop`**
→ Dodaj `pytest_asyncio` lub `@pytest.mark.asyncio` do testu

## Sentry nie wysyła events

### Check
```python
from app.observability import SENTRY_AVAILABLE
print(SENTRY_AVAILABLE)
import os
print(os.getenv("SENTRY_DSN"))
```

- `SENTRY_AVAILABLE=False` → `pip install sentry-sdk[httpx]`
- `SENTRY_DSN=None` → nieustawione w env, dodaj do `.env`
- Wszystko OK ale events brak → sprawdź `before_send` filter (może wszystko strip-uje)

## Disk space pełen (audit.jsonl rośnie)

### Rotation
Jeszcze brak built-in rotation. Manual:
```bash
mv logs/audit.jsonl logs/audit.jsonl.$(date +%Y%m%d)
gzip logs/audit.jsonl.*
# Bot będzie pisał do nowego audit.jsonl po następnej akcji
```

W przyszłości: `logrotate` config lub Python `RotatingFileHandler`.

## Anthropic 429 (rate limit)

Worker (gdy Hubert wdroży) trafi na limit przy spike load.

### Auto-protect
- Circuit breaker `anthropic` opens po 3 fails (cooldown 120s)
- Bot dla file ingest: max 3/min (LIMITS["document"] = 3/60s)

### Manual fallback
- Reduce `MAX_DOCS_PER_HOUR` w env (per ADR-010)
- Lub wait

## DLQ pełen (po Worker deploy)

`/dlq` pokazuje listę failed items.

### Causes typowe
- Corrupt PDF
- Multimodal AI fail (image too large)
- Anthropic quota exceeded
- HOS upload fail

### Manual review
1. `/dlq` → lista
2. SSH na VPS → `aws s3 cp s3://ul-os-storage/dlq/<date>/<hash>.error.json -`
3. Sprawdź error_type
4. Manual fix lub delete

## Logi się powtarzają (każde 10s)

```
httpx INFO: HTTP Request: POST /getUpdates "HTTP/1.1 200 OK"
```

To **normal long-polling** — bot pyta Telegram co 10s o nowe wiadomości. Nie błąd.

Jeśli chcesz cisza:
```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

## Bot wysyła wiadomość ale Telegram nie pokazuje

### Check
- Czy bot ma uprawnienia do chat? (private chat z botem zawsze OK)
- Czy chat_id w whitelist właściwy?
- Czy nie ma blocking po stronie Telegram (spam detection)?

### Test
```bash
curl -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
  -d "chat_id=$CHAT_ID&text=test"
```
- 200 → bot OK, problem inny (telefon nie zsync?)
- 403 → user blocked bot
- 400 → chat_id zły
