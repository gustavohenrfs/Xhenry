#!/usr/bin/env bash
# Backup diário do XHenry no VPS: dump do PostgreSQL + data/avatars.
# Se qualquer etapa falhar, avisa no Telegram com a etapa e o erro.
#
#   scripts/backup.sh
#
# cron (todo dia às 03:30):
#   30 3 * * * /caminho/do/xhenry/scripts/backup.sh >> /var/log/xhenry-backup.log 2>&1
#
# Variáveis (ambiente ou .env; o ambiente ganha):
#   DATABASE_URL        PostgreSQL do painel (obrigatória)
#   BACKUP_DIR          onde guardar (padrão /var/backups/xhenry)
#   BACKUP_KEEP_DAYS    apaga backups mais velhos que isso (padrão 14)
#   TELEGRAM_BOT_TOKEN ou TELEGRAM_TOKEN, e TELEGRAM_CHAT_ID; sem eles, tenta o
#   app_settings do banco (o que se salva em Configurações).
#   XHENRY_DIR / ENV_FILE  pasta do projeto e .env (padrão: a pasta acima deste script)
#
# Restaurar:
#   pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" xhenry-AAAAMMDD-HHMM.dump
#   tar -xzf xhenry-avatars-AAAAMMDD-HHMM.tar.gz -C /caminho/do/xhenry/data
#
# Os avatares novos ficam no banco (users.avatar_url); data/avatars só tem os antigos.

set -Eeuo pipefail
umask 077  # o dump tem hash de senha e tokens

XHENRY_DIR="${XHENRY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$XHENRY_DIR/.env}"
TELEGRAM_API="${TELEGRAM_API:-https://api.telegram.org}"
STAMP="$(date +%Y%m%d-%H%M)"
STEP="início"
ERR_LOG="$(mktemp)"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Lê uma chave do .env sem executar o arquivo.
env_value() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n 1 \
    | sed -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//"
}

DATABASE_URL="${DATABASE_URL:-$(env_value DATABASE_URL)}"
BACKUP_DIR="${BACKUP_DIR:-$(env_value BACKUP_DIR)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/xhenry}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-$(env_value BACKUP_KEEP_DAYS)}"
KEEP_DAYS="${KEEP_DAYS:-14}"
# O app aceita postgres:// e postgresql+psycopg://; o pg_dump só postgresql://.
PG_URL="${DATABASE_URL/#postgres:\/\//postgresql://}"
PG_URL="${PG_URL/#postgresql+psycopg:\/\//postgresql://}"

telegram_creds() {
  TG_TOKEN="${TELEGRAM_BOT_TOKEN:-${TELEGRAM_TOKEN:-$(env_value TELEGRAM_BOT_TOKEN)}}"
  TG_TOKEN="${TG_TOKEN:-$(env_value TELEGRAM_TOKEN)}"
  TG_CHAT="${TELEGRAM_CHAT_ID:-$(env_value TELEGRAM_CHAT_ID)}"
  if { [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; } && command -v psql >/dev/null && [ -n "$PG_URL" ]; then
    local row
    row="$(psql -At -F '|' -c \
      "SELECT coalesce(telegram_token, ''), coalesce(telegram_chat_id, '') FROM app_settings WHERE id = 1" \
      --dbname="$PG_URL" 2>/dev/null || true)"
    TG_TOKEN="${TG_TOKEN:-${row%%|*}}"
    TG_CHAT="${TG_CHAT:-${row#*|}}"
  fi
}

notify_failure() {
  local text
  text="$(printf '💾 Backup do XHenry falhou\nHost: %s\nEtapa: %s\nErro: %s' \
    "$(hostname)" "$STEP" "$(tail -n 5 "$ERR_LOG" | cut -c1-300)")"
  telegram_creds
  if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
    log "Telegram não configurado; aviso não enviado."
    return
  fi
  curl -sS --max-time 20 -o /dev/null -X POST "$TELEGRAM_API/bot$TG_TOKEN/sendMessage" \
    --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$text" \
    || log "Telegram falhou ao enviar o aviso."
}

on_error() {
  local code=$?
  trap - ERR
  log "FALHOU na etapa '$STEP' (código $code)"
  cat "$ERR_LOG" >&2
  notify_failure
  rm -f ${TMP_FILES[@]+"${TMP_FILES[@]}"} "$ERR_LOG"
  exit "$code"
}
TMP_FILES=()
trap on_error ERR

fail() { echo "$*" >>"$ERR_LOG"; false; }

STEP="configuração"
[ -n "$PG_URL" ] || fail "DATABASE_URL ausente (ambiente ou $ENV_FILE)."
case "$PG_URL" in postgresql://*) ;; *) fail "DATABASE_URL não é PostgreSQL." ;; esac
case "$KEEP_DAYS" in '' | *[!0-9]*) fail "BACKUP_KEEP_DAYS precisa ser um número de dias." ;; esac
command -v pg_dump >/dev/null || fail "pg_dump não encontrado (apt install postgresql-client)."
command -v pg_restore >/dev/null || fail "pg_restore não encontrado (apt install postgresql-client)."
mkdir -p "$BACKUP_DIR" 2>>"$ERR_LOG"

STEP="trava"
if command -v flock >/dev/null; then
  exec 9>"$BACKUP_DIR/.lock"
  flock -n 9 || fail "Outro backup ainda está rodando."
fi

STEP="dump do PostgreSQL"
DUMP="$BACKUP_DIR/xhenry-$STAMP.dump"
TMP_FILES+=("$DUMP.part")
pg_dump --format=custom --no-owner --no-privileges --dbname="$PG_URL" --file="$DUMP.part" 2>>"$ERR_LOG"
STEP="conferência do dump"
[ -s "$DUMP.part" ] || fail "Dump vazio."
pg_restore --list "$DUMP.part" >/dev/null 2>>"$ERR_LOG"
mv "$DUMP.part" "$DUMP"
log "dump ok: $DUMP ($(du -h "$DUMP" | cut -f1))"

STEP="avatares"
if [ -d "$XHENRY_DIR/data/avatars" ]; then
  AVATARS="$BACKUP_DIR/xhenry-avatars-$STAMP.tar.gz"
  TMP_FILES+=("$AVATARS.part")
  tar -czf "$AVATARS.part" -C "$XHENRY_DIR/data" avatars 2>>"$ERR_LOG"
  tar -tzf "$AVATARS.part" >/dev/null 2>>"$ERR_LOG"
  mv "$AVATARS.part" "$AVATARS"
  log "avatares ok: $AVATARS ($(du -h "$AVATARS" | cut -f1))"
else
  log "sem $XHENRY_DIR/data/avatars; pulando (avatares novos já estão no dump)."
fi

STEP="limpeza dos antigos"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'xhenry-*' -mtime +"$KEEP_DAYS" -print -delete 2>>"$ERR_LOG" \
  | sed 's/^/apagado: /'

rm -f "$ERR_LOG"
log "backup concluído."
