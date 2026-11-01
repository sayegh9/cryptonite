# HANDOFF

Current state of Cryptonite and what is left to do, as of **2026-11-01**. Main `f94b117`, database at Alembic **056**,
everything on `main` is deployed. Earlier handovers are in git history (`git log -- HANDOFF.md`); plans, reports and
evidence for each release are in `~/Documents/projects/cryptonite/reports/` (finished ones under `reports/archive/`).

## What runs now

- **Code:** `main` `f94b117`, no open pull requests. Every PR runs GitHub Actions (ruff, web, pytest, e2e-smoke,
  verify) and is merged only when green. Deploys run from `deploy-main/` (detached at `main`), one script per release.
- **Database:** production `cryptonite` at `056`. P5 (live profiles) will add `057`.
- **Services:** the whole Compose stack (`infra`) is healthy: `api`, `agent_worker`, `research_worker`, `orchestrator`,
  `ingest`, `scanner`, `learner`, `debate_worker` ×3, `exit_monitor`, `derivatives_feed`, `outbox`, `notifier`,
  `backtester`, `strategy_sandbox`, `lake_maintenance`, `sentiment`, `web`, `postgres`, `redis`.
- **Feed:** BTCUSDT, ETHUSDT, XRPUSDT and WLDUSDT spot and perps (1m, 15m, 1h, 4h), plus onboarded symbols (cap 12).
- **Auto-Trade profiles** (ADR 0011): the Auto-Trade workspace runs profiles, each with its coins (exclusive coin
  locks), markets (spot, isolated spot margin, perps), presets and timeframes, mode (Ask or Auto), risk and exits,
  daily session limits and circuit breakers. Master switch `autotrade.enabled` is **off**. Active profiles:
  - **BTC/ETH Intraday:** ACTIVE, Ask, **dry run**, long and short, perps and spot, all 14 presets on 15m/1h/4h.
  - **WLD/XRP Perps Scalp:** ACTIVE, Ask, **dry run**, long and short, perps, default presets.
  - Both at 10% max position and 1% risk per trade. In dry run an approved order is recorded (`dry_run_plan`) and
    the card reads "Not executed · Dry run", but no paper order is placed.
- **Decision pipeline:** scanner candidate → gate (direction-aware timeframe rule, cooldown) → triage → two-turn
  debate (advocate, then doubter) → PM and risk panel → grade-sized entry (A/B full cap, C half, NO_TRADE refused)
  → approval (Ask) → orchestrator. Every candidate gets a stop/target outcome label (`candidate_outcomes`, backfill
  in progress) for the Phase 2 analysis.
- **AI Brain:** the one chat on both the Brain and Debates views (History, New, Recent/Archived, permanent delete).
  Swing scan returns only real setups: the long and short swing presets plus the scanner's setups from the last 4 h,
  each labelled with its preset, timeframe and source. Order cards are sized by setup grade on spot and margin (perp
  cards keep the leverage plan's size) and warn on NO_TRADE. A saved reply is the text after the last tool call.

## Releases since the last handover (20 Oct – 1 Nov 2026)

| PR | What | Migration |
|---|---|---|
| #98, #99 | Lake compaction and bounded DuckDB (host OOM fix); nightly `lake_maintenance`; one live data folder | – |
| #100, #101, #108 | Brain cards offer the mandate fix when refused, choose the mandate when two allow the coin | – |
| #102, #103 | Three stop failure modes fixed; phantom manual sells reconciled (−$1,366.64, applied 20 Oct) | – |
| #104–#106 | Leverage replay (Stage 0); leveraged perpetuals Stages 1–4 (ADR 0009); futures executor steps (not wired) | 048 |
| #109–#111, #115 | Strategy ids keep their venue; resting perp stops survive restarts; stale tabs reload before approving | – |
| #112–#114, #116 | Inbox mandate choice; perp stop ratchet; Auto-trade Phase A (sessions, entry filters) and Phase B | 049 |
| #117, #120, #122, #123 | Full-page Settings; Operator P&L; Vibe-Trading UI redesign; Crystal Candle logo | – |
| #118 | Auto-Trade Profiles P0: profiles, lifecycle, coin locks, master switch (ADR 0011) | 050 |
| #121 | P1: profile-aware autotrading with mandate safety | – |
| #125, #126 | P4 isolated spot margin; H1 approval stops and exit-decision grants | 051, 052 |
| #127 | P2: Auto-Trade workspace and profile read API | – |
| #128–#133 | P3 Brain integration (profile tools, proposal card, profile-aware debates), auto-resume, P3b strategy picker | 053, 054 |
| #134 | AI Phase 1: direction-aware gate, two-turn debate, grade sizing, candidate outcome labels, Brain-only chat | 055 |
| #135 | H2: dry-run cards, Debates history, conversation delete, stale inbox reconcile, grade-sized Brain cards | 056 |
| #136 | Brain swing scan: no fabricated setups, named coin only, one reply per turn, no HOLD lessons on cards | – |
| #137 | Brain swing scan: short presets and the scanner's recent setups | – |

Note on #136: before it, 177 of 179 Swing Radar setups shown in the two weeks to 31 Oct were invented by a fallback
(always LONG at the live price), and most approved Brain trades in late October came from them. Judge earlier Brain
trade results with that in mind.

## Open items

1. **Gate L checks (operator):** Phase 1 and H2 in the UI: the brief's direction/grade/replay line, "grade X · size Y%"
   on entry cards, Debates History/New, conversation delete, the dry-run card text, the swing radar after #137.
2. **Dry run:** both profiles are in dry run; turn it off per profile (TOTP) to place paper orders.
3. **Presets:** tick `swing-squeeze-breakdown-short-1h` on WLD/XRP Perps Scalp (the one short preset with a
   positive replay); consider unticking the losing presets on BTC/ETH Intraday (replay in `reports/preset-replay/`).
4. **AI Phase 2** (after the outcome backfill finishes, ~14.7k candidates): funnel by realized R, preset pruning,
   triage and full-debate decisions, a neutral prompt in shadow mode, a Setups panel, removal of the old chat
   endpoints and `fleet.py` personas, and the `recall_lessons` fallback that still includes HOLD lessons.
   Plan: `reports/ai-phase1-plan.md` (Phase 2 list) and the expert review in `reports/ai-expert-review/`.
5. **Optional:** grade sizing for perp Brain cards (they keep the leverage plan's size today).
6. **P5 live profiles** (migration 057): see `reports/autotrade-profiles-plan.md`; needs Binance keys and the
   hosting decision (O5).
7. **Automatic trading volume:** most candidates are refused by triage or the timeframe rule and debates lean HOLD;
   Phase 2 measures which filters cost winners before any is loosened.

## Start and stop

Desktop icons **Cryptonite** (start) and **Stop Cryptonite** (`scripts/launcher/`, installed by
`scripts/launcher/install.sh`; re-run it after moving the repo). Start brings up Postgres and Redis, checks the schema
(never migrates), starts every service without recreating any, waits until each reports healthy, lists them, and opens
the web UI. Stop gives each service up to 30 s to shut down cleanly and confirms nothing is left running; data is
kept. Both keep their window open on an error and log to `~/.local/state/cryptonite/launcher.log`. The launchers run
the scripts in `repo/`, so keep `repo/` on `main`.

## Deploy

Deploys run from the `deploy-main` worktree (detached at `main`). Its `data/` is the **one live data folder** (a real folder, not a link; host-stability runbook §7); `.env`, `.env.notifier` and `infra/security/*.pem` are symlinks into `repo/`. The launchers and `backup.sh data` use `deploy-main` too. The `lake_maintenance` service compacts the lake and prunes its quarantine every night (runbook §6.3).

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
- `agent_worker` has `SYSTEM_REDIS_URL` (Redis DB 0, the system keys: ingest universe, heartbeats, perp marks). A
  container created before this change does not have it: recreate `agent_worker`
  (`up -d --force-recreate --no-deps agent_worker`) to pick it up.

## Test (CI runs on every PR; run the focused tests locally first)

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

GitHub Actions runs ruff, web, pytest, e2e-smoke and verify on every PR; a PR is merged only when all are green.

## Operator items (only the operator can do these)

Carried over from the 17 Oct handover; not re-checked since, except where noted.

1. **Admin password:** it was pasted into a chat; change it. TOTP re-enrolment: `scripts/enroll_admin_totp.py`.
2. **DEMO kill switch:** reset the `BINANCE-SPOT-DEMO` latch (Operator page) before starting the executor.
3. **Credentials:** rotate the Redis password (a URL with it appeared in a report).
4. **Sudo and model work:** `python3.12-dev`; Ollama upgrade (`~/Downloads/ollama-v0.35.1/README-resume.txt`), which
   also unblocks the distilled triage model ([docs/ai-first-program.md](docs/ai-first-program.md)).
5. **Backups:** the scheduled restore drill needs three consecutive passes, and a restore from the off-machine copy
   has not been proven. Keep `~/.config/cryptonite-backup/passphrase` off this machine too.
6. **Network:** the Wi-Fi extender still drops the feed (stale-data breaker paused both profiles 1 Nov 04:40–05:00
   UTC); Ethernet is the real fix. GitHub Actions billing was fixed (CI runs again).

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

- `~/Documents/projects/cryptonite/repo/`: the main checkout, on `main`, clean (the launchers run from it).
  `deploy-main/`: the deploy worktree and the one live data folder. New work goes in a separate worktree
  (`git worktree add ../wt-<name>`), removed after merge.
- `~/Documents/projects/cryptonite/reports/`: current plans and the latest deploy scripts; `reports/archive/` holds
  finished plans and reports, evidence, old deploy scripts and the Oct 14 QA run. See `reports/README.md`.
- `~/cryptonite-staging-data/`: the staging stack's data and worktrees ([docs/runbooks/staging.md](docs/runbooks/staging.md)).
- Backups: `~/backups/cryptonite/`; runbook [docs/runbooks/backup.md](docs/runbooks/backup.md).
- Reference: [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), [docs/adr/](docs/adr/) (0011 = Auto-Trade
  profiles), [docs/runbooks/](docs/runbooks/), [docs/USER_MANUAL.md](docs/USER_MANUAL.md).
