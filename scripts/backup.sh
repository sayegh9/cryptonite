#!/usr/bin/env bash
# Cryptonite backups (remediation R7.9; restore drill hardened for release gate A3).
#
#   backup.sh db       nightly: pg_dump -Fc of the dev database, 14-day rotation,
#                      plus a per-table row-count manifest beside the dump
#   backup.sh data     weekly: data/ (Nautilus catalog, analytics lake, backtest artifacts), 4 kept
#   backup.sh secrets  weekly: .env, .env.notifier, infra/security/*.pem, gpg-encrypted, 4 kept
#   backup.sh drill    restore the newest DB dump into a scratch database, prove the restore
#                      byte-for-byte at the row-count level, then drop the scratch database
#
# The DB's TOTP secrets are encrypted with the AES key in .env, so a DB dump is only restorable
# together with the secrets archive. Passphrase: $PASSPHRASE_FILE (mode 600); keep a copy off this machine.
#
# Environment overrides (defaults preserve production behavior; tests point these at throwaways):
#   CRYPTONITE_REPO, CRYPTONITE_BACKUP_DIR, CRYPTONITE_PG_CONTAINER,
#   CRYPTONITE_BACKUP_DB (source database, default cryptonite),
#   CRYPTONITE_BACKUP_SCRATCH (scratch database, default cryptonite_restore_drill),
#   CRYPTONITE_PG_DIRECT=1 (or PGHOST/PGPORT/PGDATABASE) to run PG clients directly instead of
#   through the container (how CI operates)
set -euo pipefail
umask 077  # dumps hold password hashes and encrypted TOTP secrets

REPO="${CRYPTONITE_REPO:-$HOME/Documents/projects/cryptonite/repo}"
DEST="${CRYPTONITE_BACKUP_DIR:-$HOME/backups/cryptonite}"
PG_CONTAINER="${CRYPTONITE_PG_CONTAINER:-infra-postgres-1}"
DB="${CRYPTONITE_BACKUP_DB:-cryptonite}"
SCRATCH="${CRYPTONITE_BACKUP_SCRATCH:-cryptonite_restore_drill}"
PASSPHRASE_FILE="${CRYPTONITE_BACKUP_PASSPHRASE_FILE:-$HOME/.config/cryptonite-backup/passphrase}"
STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
# Portable PG access: run the client binaries directly when pointed at a reachable cluster
# (CRYPTONITE_PG_DIRECT=1 or PGHOST/PGPORT/PGDATABASE set — that is how CI talks to its throwaway
# postgres service); otherwise go through the compose container as a convenience.
if [ -n "${CRYPTONITE_PG_DIRECT:-}" ] || [ -n "${PGHOST:-}${PGPORT:-}${PGDATABASE:-}" ]; then
    PG_DIRECT=1
else
    PG_DIRECT=
fi
pg() {  # run a postgres client binary on the reachable cluster
    if [ -n "$PG_DIRECT" ]; then "$@"; else docker exec "$PG_CONTAINER" "$@"; fi
}
# Tables the drill compares. The manifest is written right after pg_dump (not inside the same
# snapshot), so under concurrent writes it can lag the dump by seconds; that is why the drill
# compares the restore against the manifest, never against the live database.
TABLES="operator_users ai_mandates execution_commands paper_accounts portfolio_decisions alembic_version"

mkdir -p "$DEST"/{db,data,secrets}
LOGFILE="$DEST/backup.log"
EVIDENCE=""  # drill mode sets this to its per-run evidence file; log() writes to both
log() {
    local line
    line="$(date -u +%FT%TZ) $*"
    echo "$line" | tee -a "$LOGFILE"
    if [ -n "$EVIDENCE" ]; then echo "$line" >> "$EVIDENCE"; fi
}
fail() { log "ERROR: $*"; exit 1; }

prune() {  # prune <dir> <glob> <keep>
    ls -1t "$1"/$2 2>/dev/null | tail -n +"$(($3 + 1))" | xargs -r rm -f || true
}

case "${1:-}" in
db)
    out="$DEST/db/cryptonite-$STAMP.dump"
    rm -f "$out.part" "$out.part.counts"
    trap 'rm -f "$out.part" "$out.part.counts"' EXIT
    pg pg_dump -U cryptonite -Fc "$DB" > "$out.part"
    # Row-count snapshot for the drill's equality check (see TABLES note above).
    : > "$out.part.counts"
    for t in $TABLES; do
        printf '%s=%s\n' "$t" "$(pg psql -U cryptonite -d "$DB" -tAc "SELECT count(*) FROM $t")" >> "$out.part.counts"
    done
    mv "$out.part" "$out"
    mv "$out.part.counts" "$out.counts"
    trap - EXIT
    find "$DEST/db" -name 'cryptonite-*.dump' -mtime +14 -delete
    find "$DEST/db" -name 'cryptonite-*.dump.counts' -mtime +14 -delete
    log "db backup $out ($(du -h "$out" | cut -f1)); counts in $out.counts"
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
    # The scratch database is the only database this mode may drop. Refuse a configuration
    # that would point the scratch cleanup at the source database (or at postgres/template*).
    case "$SCRATCH" in
        "$DB"|postgres|template0|template1) fail "refusing drill: scratch database '$SCRATCH' is not a throwaway" ;;
    esac
    dump="$(ls -1t "$DEST"/db/cryptonite-*.dump 2>/dev/null | head -1 || true)"
    [ -n "$dump" ] || fail "drill: no dumps in $DEST/db"
    manifest="$dump.counts"
    EVIDENCE="$DEST/drill-$STAMP.log"
    : > "$EVIDENCE"
    cleanup_scratch() { pg dropdb -U cryptonite --if-exists "$SCRATCH" >/dev/null 2>&1 || true; }
    trap cleanup_scratch EXIT  # the scratch database is always dropped, on success and on error
    log "drill start dump=$dump"
    pg dropdb -U cryptonite --if-exists "$SCRATCH"
    pg createdb -U cryptonite "$SCRATCH"
    # roles and extensions already exist in the cluster; ownership/grant noise is expected.
    # The restore exit code is recorded, never masked: a failed restore fails the drill.
    if [ -n "$PG_DIRECT" ]; then
        if pg_restore -U cryptonite -d "$SCRATCH" --no-owner < "$dump" >>"$EVIDENCE" 2>&1; then
            restore_rc=0
        else
            restore_rc=$?
        fi
    else
        if docker exec -i "$PG_CONTAINER" pg_restore -U cryptonite -d "$SCRATCH" --no-owner < "$dump" >>"$EVIDENCE" 2>&1; then
            restore_rc=0
        else
            restore_rc=$?
        fi
    fi
    log "drill pg_restore exit=$restore_rc"
    q() { pg psql -U cryptonite -d "$1" -tAc "$2"; }
    status=0
    [ "$restore_rc" = 0 ] || status=1
    if [ ! -f "$manifest" ]; then
        log "drill MISMATCH manifest: no counts manifest $manifest (take a fresh db backup first)"
        status=1
    else
        while IFS='=' read -r table expected; do
            [ -n "$table" ] || continue
            if ! restored="$(q "$SCRATCH" "SELECT count(*) FROM $table" 2>/dev/null)"; then
                log "drill MISMATCH $table: table missing in restored database (restore incomplete)"
                status=1
                continue
            fi
            if [ "$restored" = "$expected" ]; then
                log "drill MATCH $table snapshot=$expected restored=$restored"
            else
                log "drill MISMATCH $table snapshot=$expected restored=$restored"
                status=1
            fi
        done < "$manifest"
    fi
    if restored_version="$(q "$SCRATCH" 'SELECT version_num FROM alembic_version' 2>/dev/null)"; then
        log "drill alembic $restored_version"
    else
        log "drill MISMATCH alembic_version: unreadable in restored database"
        status=1
    fi
    cleanup_scratch
    trap - EXIT
    if [ "$status" = 0 ]; then
        log "drill OK ($dump); evidence $EVIDENCE"
    else
        log "drill FAILED ($dump); evidence $EVIDENCE"
        exit 1
    fi
    ;;
*)
    echo "usage: $0 db|data|secrets|drill" >&2; exit 2 ;;
esac
