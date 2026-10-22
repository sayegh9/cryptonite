#!/usr/bin/env bash
# Bring up the Cryptonite STAGING stack (compose project "cryptonite-staging"; plan P0.7, gate V).
#
#   scripts/staging_up.sh [--ref <git-ref>] [--skip-restore]
#
# Steps: check >= 15 GiB available memory; build from <git-ref> (default HEAD) in a temporary
# worktree; copy the production .env files into it (read only); start staging Postgres/Redis;
# pg_dump the PRODUCTION database (read only) and restore it into staging Postgres; Alembic
# upgrade head on staging; build and start the rest; print URLs.
#
# It never stops, restarts or writes to a production container. See docs/runbooks/staging.md.
set -euo pipefail

PROJECT="cryptonite-staging"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
PROJECTS_DIR="$(dirname "$REPO_ROOT")"
DEPLOY="${CRYPTONITE_DEPLOY:-$PROJECTS_DIR/deploy-main}"
export STAGING_DATA_DIR="${STAGING_DATA_DIR:-$HOME/cryptonite-staging-data}"
export STAGING_LAKE_DIR="${STAGING_LAKE_DIR:-$DEPLOY/data}"
ENV_SRC="${CRYPTONITE_ENV_SRC:-$DEPLOY}"                       # where the production .env files live
PROD_PG="${CRYPTONITE_PG_CONTAINER:-infra-postgres-1}"         # read only: pg_dump / pg_dumpall
MIN_AVAIL_GIB=15
REF="HEAD"
RESTORE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
        --skip-restore) RESTORE=0; shift ;;
        -h|--help) sed -n 2,12p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

log() { printf '[staging_up %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# 1. memory gate ------------------------------------------------------------------------------
avail_gib=$(awk '/^MemAvailable:/ {printf "%d", $2/1048576}' /proc/meminfo)
[ "$avail_gib" -ge "$MIN_AVAIL_GIB" ] || die "only ${avail_gib} GiB available, need >= ${MIN_AVAIL_GIB} GiB. Not starting."
log "memory ok: ${avail_gib} GiB available"

# 2. guards -----------------------------------------------------------------------------------
case "$PROD_PG" in stg_*|cryptonite-staging*) die "CRYPTONITE_PG_CONTAINER must be the production DB container, got $PROD_PG" ;; esac
[ -d "$STAGING_LAKE_DIR/analytics_lake" ] || die "lake not found at $STAGING_LAKE_DIR/analytics_lake"
[ -f "$ENV_SRC/.env" ] || die "no .env in $ENV_SRC (set CRYPTONITE_ENV_SRC)"
docker inspect "$PROD_PG" >/dev/null 2>&1 || die "production Postgres container $PROD_PG not found"

# 3. temporary worktree for the requested ref ------------------------------------------------
SHA="$(git -C "$REPO_ROOT" rev-parse --verify "$REF^{commit}")" || die "bad ref $REF"
WT="$STAGING_DATA_DIR/worktree"
mkdir -p "$STAGING_DATA_DIR"
if [ -e "$WT" ]; then
    git -C "$REPO_ROOT" worktree remove --force "$WT" 2>/dev/null || die "stale $WT exists; remove it by hand"
fi
git -C "$REPO_ROOT" worktree add --detach "$WT" "$SHA" >/dev/null
log "worktree $WT at ${SHA:0:12} ($REF)"
install -m 600 "$ENV_SRC/.env" "$WT/.env"
if [ -f "$ENV_SRC/.env.notifier" ]; then install -m 600 "$ENV_SRC/.env.notifier" "$WT/.env.notifier"; else : > "$WT/.env.notifier"; fi

OVERRIDE="$WT/infra/docker-compose.staging.yml"
[ -f "$OVERRIDE" ] || OVERRIDE="$REPO_ROOT/infra/docker-compose.staging.yml"   # ref predates the override
PROFILES=(--profile monitoring)
if [ -n "${STAGING_TELEGRAM_CHAT_ID:-}" ] && [ -n "${STAGING_TELEGRAM_BOT_TOKEN:-}" ]; then
    PROFILES+=(--profile notifier)
else
    log "STAGING_TELEGRAM_CHAT_ID / STAGING_TELEGRAM_BOT_TOKEN not both set: notifier disabled"
fi
dc() { docker compose -p "$PROJECT" -f "$WT/infra/docker-compose.yml" -f "$OVERRIDE" "${PROFILES[@]}" "$@"; }
dc config >/dev/null || die "compose config failed"

# 4. staging-only data dirs (the live lake is only ever mounted :ro) --------------------------
for d in data ingest backtest_artifacts; do mkdir -p "$STAGING_DATA_DIR/$d"; chmod 0777 "$STAGING_DATA_DIR/$d"; done

# 5. staging Postgres + Redis -----------------------------------------------------------------
dc up -d postgres redis
log "waiting for stg_postgres"
for _ in $(seq 1 60); do
    docker exec stg_postgres pg_isready -U cryptonite -d cryptonite >/dev/null 2>&1 && break
    sleep 2
done
docker exec stg_postgres pg_isready -U cryptonite -d cryptonite >/dev/null || die "stg_postgres not ready"

# 6. restore the production DB into staging (production side is read only) -------------------
if [ "$RESTORE" = 1 ]; then
    log "copying roles from $PROD_PG (pg_dumpall --roles-only)"
    docker exec "$PROD_PG" pg_dumpall -U cryptonite --roles-only \
        | docker exec -i stg_postgres psql -U cryptonite -d postgres -q -v ON_ERROR_STOP=0 >/dev/null 2>&1 || true
    log "pg_dump $PROD_PG -> pg_restore stg_postgres (this takes a while)"
    docker exec "$PROD_PG" pg_dump -U cryptonite -Fc cryptonite \
        | docker exec -i stg_postgres pg_restore -U cryptonite -d cryptonite --no-owner --clean --if-exists \
        || log "pg_restore reported warnings (usually harmless 'does not exist' notices); check the row counts below"
    docker exec stg_postgres psql -U cryptonite -d cryptonite -Atc \
        "select 'operator_users='||count(*) from operator_users" || die "restore left no usable database"
fi

# 7. Alembic upgrade on staging, before any app container sees the schema ---------------------
log "alembic upgrade head on staging (127.0.0.1:5532)"
(cd "$WT" && DATABASE_URL="postgresql://cryptonite:cryptonite_dev_pass@127.0.0.1:5532/cryptonite" \
    uv run --frozen python -m alembic -c infra/migrations/alembic.ini upgrade head)

# 7b. staging-only DB hardening: LLM cap and dry-run. Asserts the target is the staging Postgres first.
STG_PG="stg_postgres"
[ "$STG_PG" != "$PROD_PG" ] || die "target DB container equals the production one"
docker port "$STG_PG" 5432/tcp 2>/dev/null | grep -q ':5532$' || die "$STG_PG is not published on 5532; refusing to write"
[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$STG_PG")" = "$PROJECT" ] \
    || die "$STG_PG does not belong to project $PROJECT; refusing to write"
spsql() { docker exec -i "$STG_PG" psql -U cryptonite -d cryptonite -Atq -v ON_ERROR_STOP=1 "$@"; }
spsql -c "INSERT INTO system_settings (key, value) VALUES ('LLM_DAILY_BUDGET_USD', '2'::jsonb)
          ON CONFLICT (key) DO UPDATE SET value = '2'::jsonb, updated_at = now()"
if [ "$(spsql -c "select count(*) from information_schema.columns where table_name='ai_mandates' and column_name='dry_run'")" = 1 ]; then
    n=$(spsql -c "WITH u AS (UPDATE ai_mandates SET dry_run = true WHERE status <> 'ARCHIVED' AND NOT dry_run RETURNING 1) SELECT count(*) FROM u")
    total=$(spsql -c "select count(*) from ai_mandates where status <> 'ARCHIVED' and dry_run")
    log "staging DB: LLM_DAILY_BUDGET_USD=2; dry_run=true set on $n mandates ($total non-archived mandates now dry-run)"
else
    log "staging DB: LLM_DAILY_BUDGET_USD=2; ai_mandates.dry_run absent (pre-050), no mandates changed"
fi

# 8. build and start everything ---------------------------------------------------------------
log "building and starting the stack"
dc up -d --build

cat <<OUT

Staging is up (project $PROJECT, ref ${SHA:0:12}).
  web        http://127.0.0.1:3200
  api        http://127.0.0.1:8100/health
  scanner    http://127.0.0.1:8182/health
  ingest     http://127.0.0.1:8180/health
  postgres   127.0.0.1:5532   redis 127.0.0.1:6479
  web WebSocket -> ws://127.0.0.1:8100/ws/stream (staging API)
Soak report:  uv run python scripts/staging_soak_report.py --hours 24
Tear down:    scripts/staging_down.sh
OUT
