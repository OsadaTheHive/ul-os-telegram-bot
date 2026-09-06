#!/usr/bin/env bash
# Restic backup do Hetzner Object Storage (per ADR-001 + ADR-009 z planu autonomii).
#
# Backupuje:
#   - Postgres ulos_knowledge (baza wiedzy — 18 GB, 447k+ dokumentów, sprawy,
#     kontrahenci, beneficjenci CP — NAJWAŻNIEJSZY zasób grupy)
#   - Directus volumes (database, uploads, extensions, templates)
#   - Coolify configs (env vars, docker-compose generated)
#   - Vault repo (na wypadek gdyby GitHub padl)
#   - Bot logs (audit.jsonl - na potrzeby compliance)
#
# 🔴 NAPRAWA 2026-09-06 (audyt P11 planu Jarvis, Hubert: "potwierdzenie
# harmonogramu backupu"): ten skrypt NIGDY nie backupował głównej bazy
# `ulos_knowledge` (pgvector, kontener docker uruchomiony bezpośrednio na
# VPS, NIE jako zasób Coolify z wbudowanym backupem) — sprawdzone bezpośrednio
# na produkcji (`docker exec ... psql -l`, `pg_dump` w allowliście SSH):
# baza istnieje, ma 18 GB, i do tej pory nie miała ŻADNEJ kopii poza samym
# diskiem, na którym leży. Dodano krok [0] poniżej — pg_dump w formacie custom
# (kompresja + szybkie odtwarzanie pojedynczych tabel), przez `docker exec`
# do kontenera Postgresa, bez wystawiania hasła (peer auth wewnątrz kontenera).
# Dodatkowo POTWIERDZONE 06.09: żaden cron/kontener (`ofelia`, `restic`, itp.)
# uruchamiający TEN skrypt nie istnieje dziś na produkcji — sam skrypt bez
# instalacji crona jest martwym kodem. Instalacja crona (patrz dół pliku)
# to jedyny krok, którego nie da się zrobić zdalnie bez sudo na hoście.
#
# Uruchom: ./scripts/restic-backup.sh
# Cron na VPS:
#   0 3 * * * root /usr/local/bin/restic-backup.sh > /var/log/restic-backup.log 2>&1

set -euo pipefail

# Config (override w env)
S3_ENDPOINT="${S3_ENDPOINT:-https://nbg1.your-objectstorage.com}"
S3_BUCKET="${S3_BUCKET:-ul-os-storage}"
S3_PREFIX="${S3_PREFIX:-backups}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
# Kontener Postgresa ulos_knowledge (docker ps: pgvector/pgvector:pg16). Nazwa
# to losowy identyfikator nadany przy pierwszym `docker run` — jeśli kontener
# kiedyś zostanie odtworzony pod inną nazwą, nadpisać tę zmienną w env crona.
ULOS_DB_CONTAINER="${ULOS_DB_CONTAINER:-ec08wkcgoc0oskos4k84gs4w}"
ULOS_DB_NAME="${ULOS_DB_NAME:-ulos_knowledge}"

[ -z "$RESTIC_PASSWORD" ] && { echo "ERROR: RESTIC_PASSWORD env required"; exit 1; }

# Restic repo URL (S3 format)
export RESTIC_REPOSITORY="s3:${S3_ENDPOINT}/${S3_BUCKET}/${S3_PREFIX}"
export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:?S3_ACCESS_KEY_ID required}"
export AWS_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:?S3_SECRET_ACCESS_KEY required}"

# Init repo (idempotent - failuje jezeli juz istnieje, ignorujemy)
restic init 2>/dev/null || echo "Repo exists, skipping init"

# Tags per backup type (do pozniejszego retention)
TIMESTAMP=$(date -u +%Y%m%d_%H%M)
HOSTNAME="$(hostname)"

# === 0. Postgres ulos_knowledge (NAJWAŻNIEJSZE — baza wiedzy całej grupy) ===
echo "▶ [0/5] Postgres ulos_knowledge (18 GB, dump może potrwać kilka minut)..."
if docker inspect "$ULOS_DB_CONTAINER" >/dev/null 2>&1; then
  DUMP_NAME="ulos_knowledge_${TIMESTAMP}.dump"
  docker exec "$ULOS_DB_CONTAINER" pg_dump -U postgres -d "$ULOS_DB_NAME" -Fc -f "/tmp/${DUMP_NAME}"
  docker cp "${ULOS_DB_CONTAINER}:/tmp/${DUMP_NAME}" "/tmp/${DUMP_NAME}"
  docker exec "$ULOS_DB_CONTAINER" rm -f "/tmp/${DUMP_NAME}"
  restic backup "/tmp/${DUMP_NAME}" --tag "ulos-knowledge-db" --tag "$TIMESTAMP" --host "$HOSTNAME"
  rm -f "/tmp/${DUMP_NAME}"
  echo "  ulos_knowledge OK"
else
  echo "  BŁĄD: kontener $ULOS_DB_CONTAINER nie istnieje — sprawdź ULOS_DB_CONTAINER (nazwa mogła się zmienić po docker run)"
  exit 1
fi

# === 1. Directus DB snapshot (SQLite legacy — patrz uwaga w nagłówku) ===
echo "▶ [1/5] Directus database snapshot..."
DB_VOLUME="/var/lib/docker/volumes/zg4kwook0osks0gsoco48s04_directus-database/_data"
if [ -d "$DB_VOLUME" ]; then
  # Hot SQLite backup (bez stop kontenera)
  TMP_BAK="/tmp/data_${TIMESTAMP}.bak"
  docker exec directus-zg4kwook0osks0gsoco48s04 \
    sqlite3 /directus/database/data.db ".backup /tmp/$(basename $TMP_BAK)" 2>/dev/null || true
  docker cp "directus-zg4kwook0osks0gsoco48s04:/tmp/$(basename $TMP_BAK)" "$TMP_BAK" 2>/dev/null || true

  if [ -f "$TMP_BAK" ]; then
    restic backup "$TMP_BAK" --tag "directus-db" --tag "$TIMESTAMP" --host "$HOSTNAME"
    rm -f "$TMP_BAK"
    echo "  Directus DB OK"
  else
    echo "  WARN: Directus DB snapshot nie wygenerowany - skip"
  fi
fi

# === 2. Directus uploads (pliki) ===
echo "▶ [2/5] Directus uploads..."
UPLOADS_VOLUME="/var/lib/docker/volumes/zg4kwook0osks0gsoco48s04_directus-uploads/_data"
if [ -d "$UPLOADS_VOLUME" ]; then
  restic backup "$UPLOADS_VOLUME" --tag "directus-uploads" --tag "$TIMESTAMP" --host "$HOSTNAME"
  echo "  Uploads OK"
fi

# === 3. Coolify config (compose + env) ===
echo "▶ [3/5] Coolify configs..."
if [ -d "/data/coolify/applications" ]; then
  restic backup /data/coolify/applications /data/coolify/services \
    --tag "coolify-config" --tag "$TIMESTAMP" --host "$HOSTNAME" \
    --exclude='*.log' --exclude='*.tmp'
  echo "  Coolify OK"
fi

# === 4. Vault repo (offline backup) ===
echo "▶ [4/5] Vault offline backup..."
VAULT_PATH="${VAULT_PATH:-/data/vault-cache}"
if [ -d "$VAULT_PATH" ]; then
  restic backup "$VAULT_PATH" --tag "vault" --tag "$TIMESTAMP" --host "$HOSTNAME" \
    --exclude='.git/objects/pack/*'  # GitHub ma to
  echo "  Vault OK"
fi

# === Retention - usuwaj stare ===
echo "▶ Retention policy..."
restic forget \
  --keep-daily 7 \
  --keep-weekly 4 \
  --keep-monthly 12 \
  --keep-yearly 5 \
  --prune

# === Stats ===
echo ""
echo "▶ Stats:"
restic stats --mode raw-data | head -5

echo ""
echo "✅ Backup completed: $TIMESTAMP"
