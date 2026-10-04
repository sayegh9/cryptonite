#!/usr/bin/env bash
# Cryptonite backups (remediation R7.9).
#
#   backup.sh db       nightly: pg_dump -Fc of the dev database, 14-day rotation
#   backup.sh data     weekly: data/ (Nautilus catalog, analytics lake, backtest artifacts), 4 kept
#   backup.sh secrets  weekly: .env, .env.notifier, infra/security/*.pem, gpg-encrypted, 4 kept
#   backup.sh drill    restore the newest DB dump into a scratch database, check it, drop it
#
# The DB's TOTP secrets are encrypted with the AES key in .env, so a DB dump is only restorable
# together with the secrets archive. Passphrase: $PASSPHRASE_FILE (mode 600); keep a copy off this machine.
set -euo pipefail
umask 077  # dumps hold password hashes and encrypted TOTP secrets

REPO="${CRYPTONITE_REPO:-$HOME/Documents/projects/cryptonite/repo}"
DEST="${CRYPTONITE_BACKUP_DIR:-$HOME/backups/cryptonite}"
PG_CONTAINER="${CRYPTONITE_PG_CONTAINER:-infra-postgres-1}"
DB="cryptonite"
PASSPHRASE_FILE="${CRYPTONITE_BACKUP_PASSPHRASE_FILE:-$HOME/.config/cryptonite-backup/passphrase}"
STAMP="$(date -u +%Y-%m-%dT%H%MZ)"

mkdir -p "$DEST"/{db,data,secrets}
log() { echo "$(date -u +%FT%TZ) $*"; }

prune() {  # prune <dir> <glob> <keep>
    ls -1t "$1"/$2 2>/dev/null | tail -n +"$(($3 + 1))" | xargs -r rm -f
}

case "${1:-}" in
db)
    out="$DEST/db/cryptonite-$STAMP.dump"
    docker exec "$PG_CONTAINER" pg_dump -U cryptonite -Fc "$DB" > "$out.part"
    mv "$out.part" "$out"
    find "$DEST/db" -name 'cryptonite-*.dump' -mtime +14 -delete
    log "db backup $out ($(du -h "$out" | cut -f1))"
    ;;
data)
    out="$DEST/data/data-$STAMP.tar.zst"
    tar -C "$REPO" -cf - data | zstd -q -T0 -o "$out.part"
    mv "$out.part" "$out"
    prune "$DEST/data" 'data-*.tar.zst' 4
    log "data backup $out ($(du -h "$out" | cut -f1))"
    ;;
secrets)
    [ -r "$PASSPHRASE_FILE" ] || { echo "missing passphrase file $PASSPHRASE_FILE" >&2; exit 1; }
    out="$DEST/secrets/secrets-$STAMP.tar.gpg"
    (cd "$REPO" && tar -cf - .env $( [ -f .env.notifier ] && echo .env.notifier ) infra/security/*.pem) \
        | gpg --batch --yes --pinentry-mode loopback --passphrase-file "$PASSPHRASE_FILE" \
              --symmetric --cipher-algo AES256 -o "$out.part"
    mv "$out.part" "$out"
    chmod 600 "$out"
    prune "$DEST/secrets" 'secrets-*.tar.gpg' 4
    log "secrets backup $out"
    ;;
drill)
    dump="$(ls -1t "$DEST"/db/cryptonite-*.dump | head -1)"
    scratch="cryptonite_restore_drill"
    docker exec "$PG_CONTAINER" dropdb -U cryptonite --if-exists "$scratch"
    docker exec "$PG_CONTAINER" createdb -U cryptonite "$scratch"
    # roles and extensions already exist in the cluster; ownership/grant noise is expected
    docker exec -i "$PG_CONTAINER" pg_restore -U cryptonite -d "$scratch" --no-owner < "$dump" 2> "$DEST/drill-last.log" || true
    q() { docker exec "$PG_CONTAINER" psql -U cryptonite -d "$1" -tAc "$2"; }
    status=0
    for t in operator_users ai_mandates execution_commands paper_accounts portfolio_decisions alembic_version; do
        live="$(q "$DB" "SELECT count(*) FROM $t")"; restored="$(q "$scratch" "SELECT count(*) FROM $t")"
        log "drill $t live=$live restored=$restored"
        [ "$restored" -gt 0 ] || status=1
    done
    log "drill alembic $(q "$scratch" 'SELECT version_num FROM alembic_version')"
    docker exec "$PG_CONTAINER" dropdb -U cryptonite "$scratch"
    [ "$status" = 0 ] && log "drill OK ($dump)" || { log "drill FAILED ($dump)"; exit 1; }
    ;;
*)
    echo "usage: $0 db|data|secrets|drill" >&2; exit 2 ;;
esac
