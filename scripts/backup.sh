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
pgstd() {  # line-buffered psql with stdin attached (`-i`): used by the snapshot holder (-f - handoff)
    if [ -n "$PG_DIRECT" ]; then stdbuf -oL psql "$@"; else docker exec -i "$PG_CONTAINER" stdbuf -oL psql "$@"; fi
}
# Tables the drill compares. The dump and these counts come from a single exported PG snapshot (see
# the db mode), so the manifest always describes exactly the rows inside the dump; the drill compares
# the restore against the manifest, never against the live database.
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
fail() { log "ERROR: $*"; FAIL_LOGGED=1; exit 1; }

# A durable nonzero verdict for every failed backup attempt, whatever the exit path. Mode failures
# that already went through fail() logged a specific reason (FAIL_LOGGED=1); anything that exits
# under set -e before a log call (e.g. a failing tar|zstd or gpg pipeline, a failed mv) still gets a
# verdict appended to backup.log here, so "a failed nightly run is logged" is true for db/data/secrets.
MODE="${1:-}"
FAIL_LOGGED=0
stop_snapshot_holder() { :; }  # db redefines the real one (snapshot-holder teardown)
cleanup_scratch() { :; }       # drill redefines the real one (scratch database drop)
on_exit() {
    local rc=$?
    case "$MODE" in
        db)
            stop_snapshot_holder
            if [ -n "${out:-}" ]; then rm -f "$out.part" "$out.part.counts"; fi
            rm -f "${snap_fifo:-}" "${snap_ctl:-}"
            ;;
        data|secrets)
            if [ -n "${out:-}" ]; then rm -f "$out.part"; fi
            ;;
        drill)
            cleanup_scratch
            ;;
    esac
    if [ "$rc" != 0 ] && [ "$FAIL_LOGGED" != 1 ]; then
        case "$MODE" in
            db|data|secrets)
                log "ERROR: $MODE backup attempt failed (exit $rc); nothing published"
                ;;
        esac
    fi
}
trap on_exit EXIT

prune() {  # prune <dir> <glob> <keep>
    ls -1t "$1"/$2 2>/dev/null | tail -n +"$(($3 + 1))" | xargs -r rm -f || true
}

case "${1:-}" in
db)
    out="$DEST/db/cryptonite-$STAMP.dump"
    rm -f "$out.part" "$out.part.counts"
    snap_fifo=""
    snap_ctl=""
    SNAP_PID=
    stop_snapshot_holder() {  # end the holder session that kept the exported snapshot alive
        if [ -n "${SNAP_PID:-}" ]; then
            # Close the control pipe: the producer's `cat` sees EOF, psql reaches stdin EOF and
            # leaves the transaction, releasing the exported snapshot. O_RDWR open never blocks.
            if [ -n "${snap_ctl:-}" ] && [ -p "$snap_ctl" ]; then
                exec 3<>"$snap_ctl" 3>&-
            fi
            wait "$SNAP_PID" 2>/dev/null || true
            if kill -0 "$SNAP_PID" 2>/dev/null; then kill -9 "$SNAP_PID" 2>/dev/null || true; fi
            SNAP_PID=
        fi
    }
    # Mode cleanup (holder, partial files) runs via the global on_exit trap on every exit path.

    # One exported PostgreSQL snapshot feeds the dump AND every per-table count, so the manifest can
    # never lag or lead the dump under concurrent writes. A holder psql session exports the snapshot
    # and keeps the transaction open (its stdin is held by the producer) until the dump and all
    # counts have consumed it. psql runs in -f - mode so each statement's output is flushed at once
    # (gnu line-buffering), which avoids `-c` buffering every result until the whole string returns.
    SNAPSHOT=""
    snap_fifo="${TMPDIR:-/tmp}/cryptonite-snapshot-$$.fifo"
    snap_ctl="${TMPDIR:-/tmp}/cryptonite-snapctl-$$.fifo"
    if ! mkfifo "$snap_fifo" || ! mkfifo "$snap_ctl"; then
        fail "db: cannot create snapshot fifos"
    fi
    (
        printf 'BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;\n'
        printf 'SELECT pg_export_snapshot();\n'
        cat <"$snap_ctl"   # keep psql's stdin open so the exporting transaction stays alive
    ) | pgstd -U cryptonite -d "$DB" -v ON_ERROR_STOP=1 -Atq -f - >"$snap_fifo" 2>&1 &
    SNAP_PID=$!
    # pgstd flushes the exported snapshot id immediately; the timeout is belt-and-braces against a
    # broken psql/docker exec so the backup fails loudly instead of hanging forever.
    read -t 30 -r SNAPSHOT <"$snap_fifo" || true
    case "$SNAPSHOT" in
        ''|*[!0-9a-fA-F-]*) fail "db: could not export a shared snapshot (saw '$SNAPSHOT')" ;;
    esac
    if ! pg pg_dump -U cryptonite --snapshot="$SNAPSHOT" -Fc "$DB" > "$out.part"; then
        fail "db: pg_dump failed"
    fi
    # Per-table counts from the SAME snapshot. Every query is fail-checked before the dump and
    # manifest are published: a failed or non-numeric count aborts the backup instead of logging
    # success with a hollow manifest value.
    : > "$out.part.counts"
    for t in $TABLES; do
        count=""
        if ! count="$(pg psql -U cryptonite -d "$DB" -v ON_ERROR_STOP=1 -Atq -c \
            "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY; SET TRANSACTION SNAPSHOT '$SNAPSHOT'; SELECT count(*) FROM $t; COMMIT;" 2>/dev/null)"; then
            fail "db: count query failed for $t"
        fi
        case "$count" in
            ''|*[!0-9]*) fail "db: non-numeric count for $t ('$count')" ;;
        esac
        printf '%s=%s\n' "$t" "$count" >> "$out.part.counts"
    done
    # alembic revision from the same snapshot: sha256 over the ordered version values, so a drill can
    # prove not just that the table restored but that the migrated head is unchanged.
    versions=""
    if ! versions="$(pg psql -U cryptonite -d "$DB" -v ON_ERROR_STOP=1 -Atq -c \
        "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY; SET TRANSACTION SNAPSHOT '$SNAPSHOT'; SELECT version_num FROM alembic_version ORDER BY version_num; COMMIT;" 2>/dev/null)"; then
        fail "db: alembic version query failed"
    fi
    printf 'alembic_version_hash=%s\n' "$(printf '%s' "$versions" | sha256sum | cut -d' ' -f1)" >> "$out.part.counts"
    stop_snapshot_holder
    rm -f "$snap_fifo" "$snap_ctl"
    mv "$out.part" "$out"
    mv "$out.part.counts" "$out.counts"
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
    EVIDENCE="$DEST/drill-$STAMP.log"
    : > "$EVIDENCE"
    # The plan names a stable latest pointer: symlink the newest run's evidence here. Refreshed at
    # the start of every drill run, so it also covers a run that fails before restoring anything.
    ln -sfn "drill-$STAMP.log" "$DEST/drill-last.log"
    # The scratch database is the only database this mode may drop. Refuse a configuration
    # that would point the scratch cleanup at the source database (or at postgres/template*).
    case "$SCRATCH" in
        "$DB"|postgres|template0|template1) fail "refusing drill: scratch database '$SCRATCH' is not a throwaway" ;;
    esac
    dump="$(ls -1t "$DEST"/db/cryptonite-*.dump 2>/dev/null | head -1 || true)"
    [ -n "$dump" ] || fail "drill: no dumps in $DEST/db"
    manifest="$dump.counts"
    cleanup_scratch() { pg dropdb -U cryptonite --if-exists "$SCRATCH" >/dev/null 2>&1 || true; }
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
        # Strict manifest validation: exactly the six count tables, each with one numeric count, no
        # extras or duplicates, identifiers allowlisted (manifest content can never reach SQL), and a
        # present alembic revision hash that makes the version proof verifiable. Anything else fails
        # the drill: an empty/truncated manifest can never slip through on the strength of one table.
        declare -A want=()
        version_hash_expected=""
        bad=""
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                '')
                    bad="manifest has a blank line"
                    break
                    ;;
                *=*) ;;
                *)
                    bad="manifest line has no '=': '$line'"
                    break
                    ;;
            esac
            key="${line%%=*}"
            val="${line#*=}"
            case "$key" in
                operator_users|ai_mandates|execution_commands|paper_accounts|portfolio_decisions|alembic_version)
                    case "$val" in
                        ''|*[!0-9]*) bad="non-numeric count for $key: '$val'"; break ;;
                    esac
                    if [ -n "${want[$key]+x}" ]; then
                        bad="duplicate manifest line for $key"
                        break
                    fi
                    want[$key]="$val"
                    ;;
                alembic_version_hash)
                    case "$val" in
                        ''|*[!0-9a-f]*) bad="malformed alembic_version_hash: '$val'"; break ;;
                    esac
                    if [ -n "$version_hash_expected" ]; then
                        bad="duplicate alembic_version_hash"
                        break
                    fi
                    version_hash_expected="$val"
                    ;;
                *)
                    bad="unexpected manifest key '$key'"
                    break
                    ;;
            esac
        done < "$manifest"
        if [ -z "$bad" ]; then
            for t in $TABLES; do
                if [ -z "${want[$t]+x}" ]; then
                    bad="manifest missing $t"
                    break
                fi
            done
        fi
        if [ -z "$bad" ] && [ -z "$version_hash_expected" ]; then
            bad="manifest missing alembic_version_hash"
        fi
        if [ -n "$bad" ]; then
            log "drill MISMATCH manifest invalid: $bad"
            status=1
        else
            for t in $TABLES; do
                expected="${want[$t]}"
                if ! restored="$(q "$SCRATCH" "SELECT count(*) FROM $t" 2>/dev/null)"; then
                    log "drill MISMATCH $t: table missing in restored database (restore incomplete)"
                    status=1
                    continue
                fi
                if [ "$restored" = "$expected" ]; then
                    log "drill MATCH $t snapshot=$expected restored=$restored"
                else
                    log "drill MISMATCH $t snapshot=$expected restored=$restored"
                    status=1
                fi
            done
            # alembic revision proof: the restored version set must hash to the backup-time value.
            versions="$(q "$SCRATCH" "SELECT version_num FROM alembic_version ORDER BY version_num")" || versions=""
            restored_hash="$(printf '%s' "$versions" | sha256sum | cut -d' ' -f1)"
            if [ "$restored_hash" = "$version_hash_expected" ]; then
                log "drill MATCH alembic_version hash=$version_hash_expected"
            else
                log "drill MISMATCH alembic_version hash: expected=$version_hash_expected restored=$restored_hash"
                status=1
            fi
        fi
    fi
    cleanup_scratch
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
