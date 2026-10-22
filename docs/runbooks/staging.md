# Staging stack and 24 h soak (plan P0.7, gate V)

Every phase of the Auto-Trade Profiles plan deploys to staging first, soaks for 24 h with dry-run
profiles, and reaches production only with a clean soak report.

Staging is a second compose project, `cryptonite-staging`, built from `infra/docker-compose.yml` plus
the override `infra/docker-compose.staging.yml`. Production is the project `infra` (containers
`cryptonite_*`, `infra-*`).

## What staging is

| Aspect | Staging | Production |
|---|---|---|
| Containers | `stg_<service>` | `cryptonite_*`, `infra-*` |
| Ports (127.0.0.1) | web 3200, api 8100, postgres 5532, redis 6479, ingest 8180, outbox 8181, scanner 8182, backtester 8183, agent_worker 8185 | 3000, 8000, 5432, 6379, 8080-8085 |
| Volumes / networks | `stg_postgres_data`, `stg_redis_data`, `stg_sandbox_jobs`; `stg_default`, `stg_agents_net`, `stg_isolated_backtester_net` | `infra_*` |
| Database | restored from a fresh `pg_dump` of production, then `alembic upgrade head` | live |
| Lake | live `analytics_lake` and `nautilus_catalog` bound **read-only**; staging ingest writes its own copy under `~/cryptonite-staging-data/ingest` | live, read-write |
| Not running | `lake_maintenance`, `executor`, `research_worker` (profile `never`) | |
| Notifier | only if `STAGING_TELEGRAM_CHAT_ID` and `STAGING_TELEGRAM_BOT_TOKEN` are both set; production chat, bot token and ntfy topic are blanked everywhere | live |
| Env | `CRYPTONITE_ENV=staging`, `ENVIRONMENT=PAPER` (paper account only) | |

Memory limits sum to 11.5 GiB (postgres 1, redis 0.25, ingest 0.5, outbox 0.25, scanner 2, derivatives_feed 0.25,
api 1.5, web 0.5, backtester 0.5, strategy_sandbox 0.5, orchestrator 0.5, agent_worker 0.75, debate_worker 0.5 x 1,
exit_monitor 0.25, notifier 0.25, learner 1.5, sentiment 1). `staging_up.sh` refuses to start with less than
15 GiB available. Staging uses a separate bot token because two pollers on one Telegram token fight over `getUpdates`.

## Bring it up

```bash
export STAGING_TELEGRAM_CHAT_ID=...        # optional; with STAGING_TELEGRAM_BOT_TOKEN enables the notifier
export STAGING_TELEGRAM_BOT_TOKEN=...
scripts/staging_up.sh --ref task/p1-something      # default ref: HEAD
```

It checks memory, builds a worktree of the ref under `~/cryptonite-staging-data/worktree`, copies the
production `.env` files into it (read only, mode 600), starts staging Postgres and Redis, copies roles and
the data from production with `pg_dumpall --roles-only` / `pg_dump` (the only things it runs against
`infra-postgres-1`), runs Alembic against `127.0.0.1:5532`, then builds and starts the rest and prints URLs.

Overrides: `STAGING_DATA_DIR`, `STAGING_LAKE_DIR` (default `deploy-main/data`), `CRYPTONITE_ENV_SRC`
(where `.env` lives), `--skip-restore` (reuse the existing staging DB; still runs Alembic).

## Run a soak

1. Bring staging up on the ref to be deployed.
2. In the staging UI (http://127.0.0.1:3200) create or keep profiles with `dry_run = true`; do not turn
   dry-run off in staging.
3. Wait 24 h. Staging containers use `restart: on-failure:5`, so a crash loop shows up as restarts.
4. Read the report:

```bash
uv run python scripts/staging_soak_report.py --hours 24        # --json for machine output
```

It prints per service: error lines (ERROR/CRITICAL/FATAL/Traceback), tracebacks, restarts, OOM kills;
then `ai_profile_decisions` per stage/outcome (shown as unavailable before migration 050), command counts by
type/status, and the dry-run mandates' commands. **Exit 0 = pass. Exit 1 = any error line, restart or OOM
kill. Exit 2 = staging not found.** A deploy script gates on that exit code; do not deploy on 1.

## Tear down

```bash
scripts/staging_down.sh            # remove the cryptonite-staging containers and networks, keep volumes
scripts/staging_down.sh --purge    # also delete stg_* volumes, staging data dir and the worktree
```

The script refuses any project name other than `cryptonite-staging`.

## Safety rules

- Never stop, restart, recreate or `down` a production container or the project `infra`.
- Never touch `deploy-main` or its `data/` except the read-only lake bind. Never write to `infra-postgres-1`;
  reading it for `pg_dump` is allowed.
- Never use `pkill -f`. Stop staging only with `staging_down.sh`.
- Check `free -g` first: at least 15 GiB available.
- Run Compose for staging only with `-p cryptonite-staging` and both `-f` files.
- Prove isolation after editing the override: `docker compose -p cryptonite-staging -f infra/docker-compose.yml -f infra/docker-compose.staging.yml --profile monitoring --profile notifier config` shows only
  `stg_*` names, the ports above, `stg_*` volumes and networks, and every `deploy-main` mount ending `:ro`.

## Known limits

- The staging `.env` is a copy of production's, so the LLM keys (and daily budget) are shared; a soak spends real LLM budget.
- The web UI hard-codes its WebSocket to port 8000 (`apps/web/src/lib/ws.ts`), so the live stream on
  http://127.0.0.1:3200 connects to the production API. REST views use staging. Fix in the app before relying on the stream in staging.
- Restored production mandates run in staging against staging Redis and the paper account.
