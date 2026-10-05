#!/usr/bin/env bash
# One-click start: Docker stack (+ profiles), migrations, production web UI, then opens the terminal
# in an app window. Safe to run when everything is already up: it only fills in what is missing.
#
#   CRYPTONITE_PROFILES   compose profiles to start (default: "monitoring notifier")
#   CRYPTONITE_NO_BROWSER set to 1 to skip opening the window
#   --pause-on-error      keep the terminal open on failure (used by the desktop launcher)
set -Eeuo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
COMPOSE_FILE="$REPO/infra/docker-compose.yml"
PROFILES="${CRYPTONITE_PROFILES:-monitoring notifier}"
WEB_DIR="$REPO/apps/web"
WEB_URL="http://localhost:3000"
API_HEALTH="http://127.0.0.1:8000/health"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cryptonite"
WEB_LOG="$STATE_DIR/web.log"
WEB_PID="$STATE_DIR/web.pid"

PAUSE_ON_ERROR=0
[[ "${1:-}" == "--pause-on-error" ]] && PAUSE_ON_ERROR=1

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
notify() { command -v notify-send >/dev/null && notify-send -a Cryptonite "$@" || true; }

fail() {
  printf '\n\033[1;31mStartup failed: %s\033[0m\n' "$*" >&2
  notify -u critical "Cryptonite failed to start" "$*"
  if (( PAUSE_ON_ERROR )); then read -rp "Press Enter to close." _; fi
  exit 1
}
trap 'fail "command failed at line $LINENO (see output above)"' ERR

compose() {
  local args=(-f "$COMPOSE_FILE")
  for p in $PROFILES; do args+=(--profile "$p"); done
  docker compose "${args[@]}" "$@"
}

wait_http() {  # url, seconds, label
  local i
  for ((i = 0; i < $2; i++)); do
    curl -fs -o /dev/null --max-time 2 "$1" && return 0
    sleep 1
  done
  fail "$3 did not respond at $1 within $2 s"
}

mkdir -p "$STATE_DIR"

# The desktop launcher does not load ~/.bashrc, so nvm's node/pnpm are not on PATH yet.
if ! command -v pnpm >/dev/null; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  [[ -s "$NVM_DIR/nvm.sh" ]] && . "$NVM_DIR/nvm.sh"
  command -v pnpm >/dev/null || fail "pnpm not found (nvm not loaded?)"
fi

step "Checking Docker and Ollama"
docker info >/dev/null 2>&1 || fail "Docker is not running (sudo systemctl start docker)"
ok "Docker is up"
if curl -fs -o /dev/null --max-time 2 http://127.0.0.1:11434/api/version; then
  ok "Ollama is up"
else
  printf '    \033[33mOllama is not responding: local AI routes will fall back to the cloud tiers\033[0m\n'
fi

step "Starting Postgres and Redis"
compose up -d --wait postgres redis
ok "healthy"

step "Applying database migrations"
compose run --rm -T test uv run python -m alembic -c infra/migrations/alembic.ini upgrade head
ok "schema at head"

step "Starting the stack (profiles: $PROFILES)"
compose up -d
ok "containers up"
wait_http "$API_HEALTH" 90 "API"
ok "API healthy"

step "Starting the web UI"
if curl -fs -o /dev/null --max-time 2 "$WEB_URL"; then
  ok "already serving on $WEB_URL"
else
  cd "$WEB_DIR"
  if [[ ! -d node_modules ]]; then
    pnpm install --frozen-lockfile
  fi
  # Rebuild only when a source file is newer than the last production build.
  if [[ ! -f .next/BUILD_ID ]] || [[ -n "$(find . \( -path ./node_modules -o -path ./.next \) -prune \
        -o -type f -newer .next/BUILD_ID -print -quit)" ]]; then
    echo "    building (sources changed since the last build)..."
    pnpm build
  fi
  # setsid: the server must outlive the launcher's terminal window.
  setsid nohup pnpm start >"$WEB_LOG" 2>&1 < /dev/null &
  echo $! >"$WEB_PID"
  wait_http "$WEB_URL" 60 "Web UI (log: $WEB_LOG)"
  ok "serving on $WEB_URL (log: $WEB_LOG)"
fi

trap - ERR
notify "Cryptonite is up" "$WEB_URL"

if [[ "${CRYPTONITE_NO_BROWSER:-0}" != 1 ]]; then
  step "Opening Cryptonite"
  for b in google-chrome brave-browser chromium chromium-browser; do
    if command -v "$b" >/dev/null; then
      setsid "$b" --app="$WEB_URL" >/dev/null 2>&1 < /dev/null &
      ok "opened in $b app window"
      exit 0
    fi
  done
  xdg-open "$WEB_URL" >/dev/null 2>&1 &
fi
