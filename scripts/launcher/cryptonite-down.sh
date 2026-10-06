#!/usr/bin/env bash
# Stops what cryptonite-up.sh started: every compose service (all profiles, the web UI included) and a
# host web UI left by a launcher from before A4. Containers are stopped, not removed; data volumes are
# untouched. Ollama (systemd) keeps running.
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
COMPOSE_FILE="$REPO/infra/docker-compose.yml"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cryptonite"
WEB_PID="$STATE_DIR/web.pid"
mkdir -p "$STATE_DIR"

if [[ -f "$WEB_PID" ]] && kill -0 "$(cat "$WEB_PID")" 2>/dev/null; then
  printf '\033[1;36m==> Stopping the host web UI (older launcher)\033[0m\n'
  # setsid made the server its own process group: take down pnpm and next together.
  kill -TERM -- "-$(cat "$WEB_PID")" 2>/dev/null || kill -TERM "$(cat "$WEB_PID")"
  rm -f "$WEB_PID"
  echo "    stopped"
fi

printf '\033[1;36m==> Stopping the Docker stack\033[0m\n'
# cryptonite-up.sh restarts opt-in services (e.g. research_worker) only if they were running here.
docker compose -f "$COMPOSE_FILE" --profile '*' ps --services --status running >"$STATE_DIR/running-services" 2>/dev/null
docker compose -f "$COMPOSE_FILE" --profile '*' stop

command -v notify-send >/dev/null && notify-send -a Cryptonite "Cryptonite stopped"
