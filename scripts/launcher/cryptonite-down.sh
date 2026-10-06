#!/usr/bin/env bash
# Stops what cryptonite-up.sh started: the web UI and every compose service (all profiles).
# Containers are stopped, not removed; data volumes are untouched. Ollama (systemd) keeps running.
set -uo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cryptonite"
WEB_PID="$STATE_DIR/web.pid"

printf '\033[1;36m==> Stopping the web UI\033[0m\n'
if [[ -f "$WEB_PID" ]] && kill -0 "$(cat "$WEB_PID")" 2>/dev/null; then
  # setsid made the server its own process group: take down pnpm and next together.
  kill -TERM -- "-$(cat "$WEB_PID")" 2>/dev/null || kill -TERM "$(cat "$WEB_PID")"
  rm -f "$WEB_PID"
  echo "    stopped"
else
  echo "    not started by the launcher (a manual 'pnpm dev' keeps running)"
fi

printf '\033[1;36m==> Stopping the Docker stack\033[0m\n'
docker compose -f "$REPO/infra/docker-compose.yml" --profile '*' stop

command -v notify-send >/dev/null && notify-send -a Cryptonite "Cryptonite stopped"
