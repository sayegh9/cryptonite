# HANDOFF

Current state of Cryptonite and what is left to do. Updated 2026-10-13 evening (main `c378285`); Brain upgrade Wave 1 on feat/brain-upgrade.
Earlier handovers, status reports and review plans were removed from the tree on 2026-10-13; they are in git
history (`git log --all -- docs/HANDOVER-2026-10-06.md` and similar).

## What runs now

- **Code:** everything is on `main`; no pull requests are open. CI on GitHub does not run (see Operator items),
  so every merge since #78 was verified locally with the commands below.
- **Database:** dev DB `cryptonite` at Alembic `045` (single head). `feat/brain-upgrade` has head `046` (read-only
  `agent_safe_*` views for the Brain); it is not on `main` yet.
- **Services (Docker Compose, project `infra`):** all running and healthy. `api`, `agent_worker`, `orchestrator`,
  `ingest`, `scanner`, `learner`, `debate_worker` ×3, `backtester`, `strategy_sandbox` and `web` run `main`'s code;
  `research_worker`, `exit_monitor`, `derivatives_feed`, `outbox` and `notifier` differ from `main` only in shared
  package files they do not import. No deploy hold is in force.
- **Feed:** ingest streams BTCUSDT, ETHUSDT, XRPUSDT and WLDUSDT (1m, 15m, 1h, 4h), with REST gap recovery on
  start and on spot reconnect (#55), rate-limit handling (#52) and per-instrument price precision in the
  scanner (#49). XRP and WLD history reaches back to 2026-04-15. Ingest also starts from active onboarded spot
  instruments with status `TRADING` (cap `INGEST_MAX_SYMBOLS`=12, off with `INGEST_ONBOARDED_FROM_DB=0`). Removing a
  symbol now also needs `is_active=false` on its `instruments` row. Onboarded symbols that Binance no longer lists
  for trading are dropped at start.
- **AI Brain:** conversations are stored and can be searched, reopened and continued, renamed and archived
  (History column, open by default on wide screens; #82–#84). The Brain can read klines for any Binance symbol
  (`get_klines`, #46). SELL cards are refused for spot the mandate does not hold (#76, #78). Wave 1 on
  `feat/brain-upgrade`: one order card per turn; a held card is answered "Held for approval", which is the expected
  result, not an error; a broken reply after a card shows MODEL_INCOMPLETE and keeps the card. BUY cards for symbols
  the feed does not cover offer "Onboard X"; their approval is refused with 422, or 503 while the universe is
  unknown. SELL is never blocked by the feed. The Brain sees the UTC time. Debates that the Brain or the research
  chat starts pass the account safety gate and a cap of 6 a day (`BRAIN_DEBATE_DAILY_CAP`).
- **Web:** the 2026-10-12 UI critique is implemented ([docs/ui-ux-critique-2026-10-12.md](docs/ui-ux-critique-2026-10-12.md), #81):
  unknown states never read as healthy or empty, one session store, one kill-switch poller per account,
  PAPER and DEMO kill switches told apart.
- **Mandates:** `Brain XRPUSDT.BINANCE` is ACTIVE at L1 (created 2026-10-13 from a Brain order card; $10,000
  budget, 10% position limit, one open XRP position). `BTC-ETH spot L1` and `WLDUSDT.BINANCE` are PAUSED
  (reduce-only); the older XRP mandate is ARCHIVED. Every risk-adding order waits for the operator's approval.
- **Order outcomes (#86):** an approved Brain card follows its order to WORKING (a resting limit), FILLED or NOT
  EXECUTED with the reason, and writes the change into the conversation once, so the Brain knows. Whatever path
  approved it, an approved order that is not executed also sends an INFO notification (Telegram).
- **Mandate sizing (#86):** sizing and the mandate check value an order at the same price (its limit, else the
  live ask/bid). If they ever disagree, the order is refused as `MANDATE_SIZING_CONFLICT` and logged as an error,
  never recorded as the AI's breach. Root cause of the 2026-10-13 18:47 UTC refusal of a limit buy above the ask.
- **Breaches:** none count. The four on record (#2–#4 from 2026-10-04, #18 from 2026-10-13) were all
  system-sized and were voided by the operator on 2026-10-13 (`BREACH_VOIDED` events #19–#22, migration 045);
  the BREACH rows stay in the audit log. Voiding: `scripts/void_mandate_breach.py` (dry run by default), only for
  breaches the system caused.
- **DEMO account:** the 10 SUBMIT_ORDER commands from 2026-09-24/25 that never reached Binance were settled on
  2026-10-13, and all 62 stale DEMO reservations were released (`OPERATOR_ABORTED_UNSENT`). Script and the backup
  taken before it: `~/backups/cryptonite/settle_demo_unsent-2026-10-13.sql`, `db/cryptonite-2026-10-13T1407Z.dump`.
  The DEMO kill switch for `BINANCE-SPOT-DEMO` is still latched; the executor is not running.

## Start and stop

Desktop icons **Cryptonite** (start) and **Stop Cryptonite** (`scripts/launcher/`, installed by
`scripts/launcher/install.sh`; re-run it after moving the repo). Start brings up Postgres and Redis, checks the schema
(never migrates), starts every service without recreating any, waits until each reports healthy, lists them, and opens
the web UI. Stop gives each service up to 30 s to shut down cleanly and confirms nothing is left running; data is
kept. Both keep their window open on an error and log to `~/.local/state/cryptonite/launcher.log`. The launchers run
the scripts in `repo/`, so keep `repo/` on `main`.

## Deploy

Deploys run from the `deploy-main` worktree (detached at `main`; `.env` and `data/` are symlinks into `repo/`).

```bash
git -C ~/Documents/projects/cryptonite/repo fetch && git -C ~/Documents/projects/cryptonite/deploy-main checkout --detach origin/main
cd ~/Documents/projects/cryptonite/deploy-main && set -a && . ./.env && set +a
docker compose -f infra/docker-compose.yml build <services>
docker compose -f infra/docker-compose.yml up -d --no-deps <services>
```

- Always `--no-deps`: never recreate `postgres` or `redis`. Load `.env` with `set -a` first; a prefix assignment
  leaves the Redis password empty in the URL.
- Migrations: take a backup (`scripts/backup.sh db`), then follow [docs/operator-release-checklist.md](docs/operator-release-checklist.md).
- Which services a change needs: compare the code in each container with `main` (the files under `/app/packages`
  and `/app/services`); a shared package change only matters to the services that import it.

## Test (CI does not run, so run these before merging)

Python, against the throwaway containers `ci-pg-budget` (:15432) and `ci-redis-budget` (:16379), never the dev DB:

```bash
docker start ci-pg-budget ci-redis-budget
docker exec ci-pg-budget psql -U cryptonite -d postgres -c "CREATE DATABASE cryptonite_ci_local"
export CRYPTONITE_ENV=test CRYPTONITE_TEST_REDIS_DEDICATED=1 CRYPTONITE_PG_CONTAINER=ci-pg-budget \
  DATABASE_URL=postgresql://cryptonite:cryptonite_dev_pass@localhost:15432/cryptonite_ci_local \
  ASYNC_DATABASE_URL=postgresql+asyncpg://cryptonite:cryptonite_dev_pass@localhost:15432/cryptonite_ci_local \
  REDIS_URL=redis://localhost:16379/0
uv sync --frozen --extra dev --all-packages
uv run python -m alembic -c infra/migrations/alembic.ini upgrade head
uv run pytest tests -q && uv run pytest tests -q -m destructive
uv run ruff check $(git diff --name-only origin/main -- '*.py')   # whole-tree ruff is informational
docker stop ci-pg-budget ci-redis-budget
```

`CRYPTONITE_PG_CONTAINER` keeps the backup-drill tests off the live `infra-postgres-1`.

Web (`apps/web`): `pnpm typecheck && pnpm lint`. Playwright without stopping the live API: build with the API
proxy pointed at a closed port, as in CI, and serve it on :3100 with a local config that overrides `baseURL`
and `webServer`:

```bash
API_ORIGIN=http://127.0.0.1:8999 API_BASE=http://127.0.0.1:8999/api/v1 pnpm build
# playwright.local.config.ts: ...base, baseURL http://127.0.0.1:3100,
#   webServer "API_ORIGIN=http://127.0.0.1:8999 pnpm exec next start -p 3100"
pnpm exec playwright test -c playwright.local.config.ts
```

Last full results (2026-10-13, #86): pytest 1957 passed / 0 failed plus 2 destructive; Playwright 148/148.

## Operator items (only the operator can do these)

1. **GitHub Actions:** every job fails before it starts because of a billing or spending-limit block on the
   account. Fix it in GitHub billing; until then CI is local only.
2. **Admin password:** it was pasted into a chat; change it. TOTP re-enrolment: `scripts/enroll_admin_totp.py`
   (run inside the api container from your own terminal; it prints the new secret once).
3. **DEMO kill switch:** reset the `BINANCE-SPOT-DEMO` latch (Operator page) before starting the executor.
4. **Live checks not yet done:** a SELL refusal in a live Brain chat (calls a paid model); Telegram foreign-chat
   ignore, veto, and the kill-switch Critical alert on Telegram and ntfy.
5. **Credentials:** rotate the Redis password (a URL with it appeared in a report).
6. **Sudo and model work:** `python3.12-dev`; Ollama upgrade (`~/Downloads/ollama-v0.35.1/README-resume.txt`), which
   also unblocks the certified distilled triage model (import with `scripts/distill_triage_export.py`, about
   48 GB free needed; then re-run the triage and summarize evals before routing to it; background in
   [docs/ai-first-program.md](docs/ai-first-program.md)).
7. **Backups:** nightly DB dumps run; the scheduled restore drill needs three consecutive passes, and a restore from
   the off-machine copy has not been proven. Keep `~/.config/cryptonite-backup/passphrase` off this machine too.
8. **Decisions:** whether to resume a mandate (BTC exposure was 9.6% of a 10% limit, plus a manual 0.03 BTC long);
   the WLD mandate and the wider symbol onboarding list; deleting strategy `ai-gen-62f49193` in the UI.

## Waiting on time

- Gate 5: 14 days of L1 paper trading with 0 breaches. The 2026-10-04 breaches were voided on 2026-10-13, so the
  breach count is 0; the time requirement still applies.
- L2 candidacy of `BTC-ETH spot L1`: earliest 2026-10-31, needs 30 closed trades (`scripts/gate_status.py`).

## Known risks to watch

- **Stop-loss blindness (fixed 2026-10-15, `fix/stop-blindness`):** on the Wi-Fi extender the spot socket
  reconnected ~480 times a day, and each reconnect's gap recovery froze spot quotes for ~14 s (#55 lock); the exit
  monitor skipped 522 checks in 24 h and the XRP hard stop filled at -5.28%. Now closed bars wait off the receive
  loop, a reconnect with no missed bar makes no REST call, and a stale quote falls back to Binance's REST book
  ticker. Mission Control's Exit monitor tile shows the price source and blind time per 24 h; a CRITICAL
  NO_FRESH_QUOTE alert goes out after two blind checks in a row. Ethernet instead of the extender is still the
  real fix for the reconnects.
- **Futures bookTicker socket** reconnects about twice a minute (keepalive ping timeout). No effect seen; it has no
  recovery hook.
- **Scanner `/health`** answers in 7–10 s while a scan runs (timeout 20 s).
- **`/api/v1/bars`** still reads the whole 1-minute history of a symbol per request (critique, "Not fixed" §2).

## Where things are

- `repo/`: the main checkout, on `main`, clean. `deploy-main/`: deploy worktree.
- Browser QA: [docs/runbooks/browser-qa-agent.md](docs/runbooks/browser-qa-agent.md) (instructions for an agent that
  tests every page, places paper trades and runs a strategy instance; reports go to `~/Documents/projects/cryptonite/qa/`).
- Backups: `~/backups/cryptonite/` (`db/`, `data/`, `redis/`, `secrets/`, drill logs, `backup.log`); runbook
  [docs/runbooks/backup.md](docs/runbooks/backup.md).
- Reference: [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), [docs/ai-first-program.md](docs/ai-first-program.md),
  [docs/USER_MANUAL.md](docs/USER_MANUAL.md), [docs/adr/](docs/adr/), [docs/runbooks/](docs/runbooks/).
