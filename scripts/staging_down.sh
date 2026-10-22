#!/usr/bin/env bash
# Stop and remove ONLY the cryptonite-staging compose project. Refuses any other project name.
#
#   scripts/staging_down.sh [--purge]
#
# --purge also deletes the staging volumes (stg_*), the staging data dir and the temporary worktree.
# Production (project "infra", containers cryptonite_* / infra-*) is never touched.
set -euo pipefail

PROJECT="${STAGING_PROJECT:-cryptonite-staging}"
[ "$PROJECT" = "cryptonite-staging" ] || { echo "refusing: project must be cryptonite-staging, got '$PROJECT'" >&2; exit 1; }

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
STAGING_DATA_DIR="${STAGING_DATA_DIR:-$HOME/cryptonite-staging-data}"

# Project name alone selects containers by label; --profile "*" so profiled services go too.
docker compose -p "$PROJECT" --profile "*" down --remove-orphans
echo "project $PROJECT removed"

if [ "$PURGE" = 1 ]; then
    for v in stg_postgres_data stg_redis_data stg_sandbox_jobs; do docker volume rm "$v" 2>/dev/null && echo "removed volume $v" || true; done
    git -C "$REPO_ROOT" worktree remove --force "$STAGING_DATA_DIR/worktree" 2>/dev/null || true
    git -C "$REPO_ROOT" worktree prune
    # the data dir may hold root-owned files written by containers; only remove what we own
    rm -rf "$STAGING_DATA_DIR/data" "$STAGING_DATA_DIR/ingest" "$STAGING_DATA_DIR/backtest_artifacts" 2>/dev/null || true
    echo "staging data purged"
fi
