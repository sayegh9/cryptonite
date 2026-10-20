#!/usr/bin/env bash
# Stops what cryptonite-up.sh started: every compose service (all profiles, the web UI included) and a
# host web UI left by a launcher from before A4. Containers are stopped, not removed; data volumes are
# untouched. Ollama (systemd) keeps running.
#
# Each service gets up to STOP_TIMEOUT_S to shut down cleanly (Redis and Postgres flush, ingest commits its
# batch) before Docker kills it; compose stops dependants before what they depend on. Stopped containers stay
# stopped across a reboot (restart: unless-stopped) until cryptonite-up.sh starts them.
#
#   --pause-on-error   keep the window open on failure (used by the desktop launcher)
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
# The stack runs from the deploy worktree (HANDOFF.md "Deploy"): its compose file mounts the live data/ and the
# deployed code, so a container this script creates gets the same mounts as the running ones. repo/ only when
# there is no deploy worktree. CRYPTONITE_DEPLOY overrides the location.
DEPLOY="${CRYPTONITE_DEPLOY:-$(dirname "$REPO")/deploy-main}"
if [[ -f "$DEPLOY/infra/docker-compose.yml" ]]; then COMPOSE_FILE="$DEPLOY/infra/docker-compose.yml"
else COMPOSE_FILE="$REPO/infra/docker-compose.yml"; fi
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cryptonite"
WEB_PID="$STATE_DIR/web.pid"
LOG="$STATE_DIR/launcher.log"
STOP_TIMEOUT_S="${CRYPTONITE_STOP_TIMEOUT_S:-30}"
PAUSE_ON_ERROR=0
[[ "${1:-}" == "--pause-on-error" ]] && PAUSE_ON_ERROR=1
mkdir -p "$STATE_DIR"
exec > >(tee -a "$LOG") 2> >(tee -a "$LOG" >&2)
printf '\n===== stop %s =====\n' "$(date '+%F %T %Z')"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
notify() { command -v notify-send >/dev/null && notify-send -a Cryptonite "$@" || true; }
fail() {
  printf '\n\033[1;31mStop failed: %s\033[0m\n' "$*" >&2
  notify -u critical "Cryptonite did not stop cleanly" "$*"
  if (( PAUSE_ON_ERROR )); then read -rp "Press Enter to close." _; fi
  exit 1
}
compose_all() { docker compose -f "$COMPOSE_FILE" --profile '*' "$@"; }

docker info >/dev/null 2>&1 || fail "Docker is not running, so there is nothing to stop"

if [[ -f "$WEB_PID" ]] && kill -0 "$(cat "$WEB_PID")" 2>/dev/null; then
  step "Stopping the host web UI (older launcher)"
  # setsid made the server its own process group: take down pnpm and next together.
  kill -TERM -- "-$(cat "$WEB_PID")" 2>/dev/null || kill -TERM "$(cat "$WEB_PID")"
  rm -f "$WEB_PID"
  ok "stopped"
fi

running="$(compose_all ps --services --status running 2>/dev/null)"
if [[ -z "$running" ]]; then
  step "Nothing is running"
  ok "Cryptonite is already stopped"
  notify "Cryptonite is already stopped"
  sleep 3
  exit 0
fi

step "Stopping the Docker stack ($(wc -l <<<"$running") services, up to ${STOP_TIMEOUT_S} s each)"
# cryptonite-up.sh restarts opt-in services (e.g. research_worker) only if they were running here.
printf '%s\n' "$running" >"$STATE_DIR/running-services"
compose_all stop -t "$STOP_TIMEOUT_S" || fail "docker compose stop returned an error (see above)"

left="$(compose_all ps --services --status running 2>/dev/null)"
[[ -z "$left" ]] || fail "still running: $(tr '\n' ' ' <<<"$left")"
ok "every service is stopped (containers and data kept)"
notify "Cryptonite stopped" "Double-click Cryptonite to start it again"
sleep 3
