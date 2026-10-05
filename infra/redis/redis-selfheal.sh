#!/bin/sh
# Repair a damaged append-only file before Redis starts, then hand over to the image's entrypoint.
#
# A host crash can cut a command in half at the end of the AOF; Redis then refuses to start ("Bad
# file format reading the append only file ... use redis-check-aof --fix"), and every service that
# needs Redis stays down. This runs the fix Redis asks for: the damaged tail (the last moments of
# writes before the crash) is cut. The directory is copied to appendonlydir.bak-<UTC stamp> in the
# same volume first; the newest 5 copies are kept. Postgres is the system of record.
set -eu

DIR=/data/appendonlydir
KEEP=5

if [ -d "$DIR" ]; then
    MANIFEST=$(ls "$DIR"/*.manifest 2>/dev/null | head -1 || true)
    if [ -n "$MANIFEST" ] && ! REPORT=$(redis-check-aof "$MANIFEST" 2>&1); then
        STAMP=$(date -u +%Y%m%dT%H%M%SZ)
        cp -a "$DIR" "/data/appendonlydir.bak-$STAMP"
        echo "[redis-selfheal] AOF check failed; copied to appendonlydir.bak-$STAMP; repairing:"
        echo "$REPORT" | grep -iE "error|truncat|analyzed" | tail -4 || true
        echo y | redis-check-aof --fix "$MANIFEST" | tail -3
        ls -dt /data/appendonlydir.bak-* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
    fi
fi

exec docker-entrypoint.sh "$@"
