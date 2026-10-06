#!/usr/bin/env bash
# One-click start: Docker stack (+ profiles), including the web UI (compose service `web`, :3000), then
# opens the terminal in an app window. Safe to run when everything is already up: it only fills in what
# is missing.
#
# It never changes what is already running (F-M2):
#   - no migrations: it only warns when the schema is behind head. Upgrading is an explicit, reviewed
#     step (see CLAUDE.md "DB").
#   - `up --no-recreate --no-deps` on an explicit service list: an existing container is never
#     recreated, even when its compose config changed. HELD_SERVICES are left out of `up`: an
#     existing held container is only `start`ed if stopped, never created or recreated.
#
# So it never deploys anything either. These stay explicit steps (from the repo root):
#   - UI or service update:  docker compose -f infra/docker-compose.yml up -d --build --no-deps web
#     (same for any other non-held service)
#   - ending the hold: clearing HELD_SERVICES only restarts the held containers as they are (old image
#     and config). Deploy them, after review, with
#       docker compose -f infra/docker-compose.yml up -d --build --force-recreate --no-deps \
#         orchestrator scanner debate_worker learner
#
#   CRYPTONITE_PROFILES   compose profiles to start (default: "monitoring notifier"). Opt-in services
#                         that were running when cryptonite-down.sh stopped them (e.g. research_worker)
#                         are restarted as they are even if their profile is not listed.
#   CRYPTONITE_HELD_SERVICES  overrides HELD_SERVICES below (set it to "" to start everything)
#   CRYPTONITE_NO_BROWSER set to 1 to skip opening the window
#   --pause-on-error      keep the terminal open on failure (used by the desktop launcher)
set -Eeuo pipefail

# Services under the deploy hold (until the L2 review): the launcher never creates or recreates them,
# so they stay on their current image and config. Clear this (HELD_SERVICES="") once the hold ends.
HELD_SERVICES="${CRYPTONITE_HELD_SERVICES-orchestrator scanner debate_worker learner}"

REPO="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
COMPOSE_FILE="$REPO/infra/docker-compose.yml"
PROFILES="${CRYPTONITE_PROFILES:-monitoring notifier}"
WEB_URL="http://localhost:3000"
API_HEALTH="http://127.0.0.1:8000/health"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cryptonite"
WEB_PID="$STATE_DIR/web.pid"  # host `pnpm start` from launchers before A4
RUNNING_BEFORE_STOP="$STATE_DIR/running-services"  # written by cryptonite-down.sh

PAUSE_ON_ERROR=0
[[ "${1:-}" == "--pause-on-error" ]] && PAUSE_ON_ERROR=1

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }
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

serving() { curl -fs -o /dev/null --max-time 2 "$1"; }
listed() { [[ " ${services[*]} " == *" $1 "* ]]; }

mkdir -p "$STATE_DIR"

step "Checking Docker and Ollama"
docker info >/dev/null 2>&1 || fail "Docker is not running (sudo systemctl start docker)"
ok "Docker is up"
if curl -fs -o /dev/null --max-time 2 http://127.0.0.1:11434/api/version; then
  ok "Ollama is up"
else
  warn "Ollama is not responding: local AI routes will fall back to the cloud tiers"
fi

# A misspelled held name would silently release the real service into `up`: refuse instead.
all_services=" $(docker compose -f "$COMPOSE_FILE" --profile '*' config --services | tr '\n' ' ') "
for svc in $HELD_SERVICES; do
  [[ "$all_services" == *" $svc "* ]] || fail "HELD_SERVICES names '$svc', which is not a compose service (typo?)"
done

step "Starting Postgres and Redis"
compose up -d --wait --no-recreate postgres redis
ok "healthy"

step "Checking the database schema (read-only)"
# `alembic current` marks the applied revision "(head)" only when nothing is pending. A failure here
# (e.g. the test image is not built) is reported, never fatal, and never upgrades anything.
# --no-deps: a plain `run` would converge postgres/redis and recreate them if their config changed.
if schema="$(compose run --rm --no-deps -T test uv run python -m alembic -c infra/migrations/alembic.ini current 2>/dev/null)" \
    && grep -q '(head)' <<<"$schema"; then
  ok "schema at head"
else
  warn "schema is NOT at head (current: ${schema:-unknown}). Migrations are not applied here:"
  warn "review them, then run the DB command from CLAUDE.md."
fi

step "Starting the stack (profiles: $PROFILES; held: ${HELD_SERVICES:-none})"
# Every service the active profiles enable, minus the held ones. --no-deps keeps compose from
# starting a held service as someone's dependency; postgres and redis are already up from above.
services=()
while read -r svc; do
  [[ " $HELD_SERVICES " == *" $svc "* ]] || services+=("$svc")
done < <(compose config --services)
(( ${#services[@]} )) || fail "no services left to start (HELD_SERVICES='$HELD_SERVICES')"

if listed web; then
  # Before A4 the launcher ran the UI on the host (`pnpm start`, pid in $WEB_PID). It holds :3000, so
  # the web container could not bind it: stop it, as cryptonite-down.sh does.
  if [[ -f "$WEB_PID" ]] && kill -0 "$(cat "$WEB_PID")" 2>/dev/null; then
    kill -TERM -- "-$(cat "$WEB_PID")" 2>/dev/null || kill -TERM "$(cat "$WEB_PID")" || true
    for _ in {1..15}; do serving "$WEB_URL" || break; sleep 1; done
    ok "stopped the host web UI from an older launcher (the compose service takes over :3000)"
  fi
  rm -f "$WEB_PID"
  if [[ -z "$(compose ps -q --status running web 2>/dev/null)" ]] && serving "$WEB_URL"; then
    warn "something else serves :3000 (a manual pnpm dev/start?): the web container is left out."
    warn "Stop it and run the launcher again to hand :3000 to the compose service."
    kept=()
    for svc in "${services[@]}"; do [[ "$svc" == web ]] || kept+=("$svc"); done
    services=("${kept[@]}")
  fi
fi

# A service whose image does not exist yet (e.g. web on the first run after A4) is built here; an
# existing container is never rebuilt or recreated (see the header for explicit updates).
compose up -d --no-recreate --no-deps "${services[@]}"
ok "containers up (existing containers left as they were)"
# Held services keep running on their old container (old image and config). `start` only restarts an
# existing stopped container (e.g. after cryptonite-down.sh); it never creates or recreates one.
for svc in $HELD_SERVICES; do
  if [[ -z "$(compose ps -a -q "$svc" 2>/dev/null)" ]]; then
    warn "held: $svc has no container; left alone (deploy it explicitly when the hold ends)"
  elif compose start "$svc" >/dev/null; then
    ok "held: $svc (existing container, not recreated)"
  else
    warn "held: $svc could not be started (docker compose logs $svc)"
  fi
done
# Opt-in services outside the active profiles that were running before cryptonite-down.sh: restart
# their existing container as it is (never create one).
if [[ -f "$RUNNING_BEFORE_STOP" ]]; then
  while read -r svc; do
    [[ -n "$svc" ]] || continue
    listed "$svc" && continue
    [[ " $HELD_SERVICES " == *" $svc "* ]] && continue
    if [[ -n "$(docker compose -f "$COMPOSE_FILE" --profile '*' ps -a -q "$svc" 2>/dev/null)" ]] \
        && docker compose -f "$COMPOSE_FILE" --profile '*' start "$svc" >/dev/null; then
      ok "opt-in: $svc restarted (it was running before the last stop)"
    else
      warn "opt-in: $svc was running before the last stop but could not be restarted"
    fi
  done <"$RUNNING_BEFORE_STOP"
  rm -f "$RUNNING_BEFORE_STOP"
fi
wait_http "$API_HEALTH" 90 "API"
ok "API healthy"

step "Waiting for the web UI"
if listed web; then
  # Supervised by compose (A4): started with the stack above (a first build takes a few minutes).
  wait_http "$WEB_URL" 120 "Web UI (docker compose -f infra/docker-compose.yml logs web)"
  ok "serving on $WEB_URL (compose service)"
elif serving "$WEB_URL"; then
  ok "already serving on $WEB_URL (not the compose service)"
else
  warn "nothing serves $WEB_URL and the web service is not in the stack list."
  warn "Start it with: docker compose -f infra/docker-compose.yml up -d --build --no-deps web"
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
