# HANDOFF

Current state of Cryptonite and what is left to do. Main `05e7af2` = Brain upgrade Waves 1–3 (#92, #94, #95),
the DuckDB security hotfix (#93), and post-Wave-3 fixes (#96, #97), deployed 2026-10-17.
Earlier handovers, status reports and review plans were removed from the tree on 2026-10-13; they are in git
history (`git log --all -- docs/HANDOVER-2026-10-06.md` and similar).

## Releases summary (15–17 Oct 2026)

| PR | What | Merged | Migration | Tests | Status |
|---|---|---|---|---|---|
| #92 | Wave 1: SOLUSDT card fixes, onboarded symbols persist, gated Brain debates, read-only AI-state views | 15 Oct | 046 | 2,122 · PW 159 | deployed |
| #93 | Security: DuckDB file read/write holes (model SQL tool, unauthenticated `/api/v1/bars`) | 16 Oct | – | 2,257 | security deployed |
| #94 | Wave 2: the Brain sees its system, market context, safe degraded mode, truthful order cards | 16 Oct | – | 2,378 · PW 165 | deployed |
| #95 | Wave 3: card dry run, past lessons, debate follow-through, trusted system events, trade post-mortems | 16 Oct | 047 | 2,672 · PW 194 | deployed |
| #96 | Derivatives & regime for any Binance symbol; actionable "no trade" answers; range setups | 17 Oct | – | 2,765 | deployed |
| #97 | Order queue guard: failing commands cannot block queue; stuck-command alerts; conflict dry run; calm context | 17 Oct | – | 2,815 · PW 64 | deployed |
| Swing (`WS-S0`–`WS-S5`) | AI-First Swing Trading: MTF `htf_*` scanner engine & presets, per-position structural stops & 50% `TP1` scale-out, partial `SELL` (`reduce_pct`), `scan_swing_setups` & `SwingRadarCard`, inline chart Entry/Stop/TP1 overlays, `/swing` chat command, and `ScannerBuilder` v2 ([docs/swing-trading-wave-plan.md](docs/swing-trading-wave-plan.md)) | 17 Oct | – | 2,843 · tsc 0 | deployed |
| Settings & Header (`v2.1`) | 8-category Settings Command Center (Desk Preferences with Light/Dark/System theme & Web Audio chimes, AI Gateway & Shadow Paid toggle, interactive Exit Constraint Gauge, Risk & Guardrails, Scanner/Universe, Alerts, Security with `Tighter.HIGHER` TOTP fix, System Health & Audit trail) and App Header revamp (`⌘K` search, icons, tooltips) | 18 Oct | – | 55 pytest · PW 51 | deployed |

## What runs now

- **Code:** everything is on `main` (`906c550`); no pull requests are open. CI on GitHub does not run (see Operator items),
  so every merge was verified locally with the test commands below.
- **Database:** dev DB `cryptonite` at Alembic `047` (single head on main). `046` adds the read-only `agent_safe_*`
  views for the Brain; `047` adds the read-only view `agent_safe_trade_timeline` (the trade post-mortem) and two
  lookup indexes.
- **Services (Docker Compose, project `infra`):** all running and healthy. `api`, `agent_worker`, `research_worker`,
  `orchestrator`, `ingest`, `scanner`, `learner`, `debate_worker` ×3, `backtester`, `strategy_sandbox` and `web` run
  `main`'s code; `exit_monitor`, `derivatives_feed`, `outbox` and `notifier` differ from `main` only in shared
  package files they do not import. No deploy hold is in force.
- **Feed:** ingest streams BTCUSDT, ETHUSDT, XRPUSDT and WLDUSDT (1m, 15m, 1h, 4h), with REST gap recovery on
  start and on spot reconnect (#55), rate-limit handling (#52) and per-instrument price precision in the
  scanner (#49). XRP and WLD history reaches back to 2026-04-15. Ingest also starts from active onboarded spot
  instruments with status `TRADING` (cap `INGEST_MAX_SYMBOLS`=12, off with `INGEST_ONBOARDED_FROM_DB=0`). Removing a
  symbol now also needs `is_active=false` on its `instruments` row. Onboarded symbols that Binance no longer lists
  for trading are dropped at start.
- **AI Brain:** conversations are stored and can be searched, reopened and continued, renamed and archived (History
  column, open by default on wide screens; #82–#84). The Brain can read klines for any Binance symbol (`get_klines`,
  #46). SELL cards are refused for spot the mandate does not hold (#76, #78). Wave 1: one order card per turn; a held
  card is answered "Held for approval", which is the expected result, not an error; a broken reply after a card shows
  MODEL_INCOMPLETE and keeps the card. BUY cards for symbols the feed does not cover offer "Onboard X"; their approval
  is refused with 422, or 503 while the universe is unknown. SELL is never blocked by the feed. The Brain sees the UTC
  time. Debates that the Brain or the research chat starts pass the account safety gate and a cap of 6 a day
  (`BRAIN_DEBATE_DAILY_CAP`). Wave 2: every turn starts with a System state block (mandates, kill switches and the
  daily-loss latch, pending approvals, feed coverage, exit monitor). The Brain declines cards under the kill switch, the
  daily-loss latch or a stopped exit monitor, and BUYs on PAUSED or HALTED mandates. It has tools for news, funding,
  open interest and mark, and the market regime; ticker rows carry `as_of`. `brain.plan` output is capped at 8000 tokens
  (thinking 4096). A read-only answer from the backup model is labelled `_[degraded: answered by the backup model,
  read-only]_`. A BUY card on a PAUSED or HALTED mandate is refused with a link to Mission Control before the TOTP code
  is spent. A used TOTP code is answered `TOTP_REUSED` and does not count toward the lockout. A SELL closes a stopped
  mandate's holding. `in_feed` and the card feed notes now work (Redis DB 0). Wave 3: an order card shows a dry run
  before Approve ("Would size to…", "Would sell…", "Would be refused: …"; the button then reads "Approve anyway…"), plus
  thesis, evidence, invalidation, horizon and confidence, and up to three past lessons for the same instrument from the
  last 90 days; lessons are never put in the prompt. The dry run does not check the kill switch, so approval still
  refuses under it. A debate card follows its run to the verdict and the approval, and a Brain debate that the safety
  gate refuses is recorded as `GATE_REFUSED` ("Not run (safety gate)"). Only server-written `<system_event>` blocks are
  authoritative for the Brain. A proposal that cannot become a card says why. `/postmortem [SYMBOL]` asks for a trade
  review, which the Brain reads with `get_trade_timeline`.
  Post-Wave-3 fixes (#96, #97): derivatives and regime work for any symbol with Binance USD-M (with public API
  fallback when not in live feed); missing data is neutral; range setups allowed with reward >= 1.5x risk; every
  "no trade" ends with trigger, stop and target; text after tool call starts a new paragraph; per-turn state no
  longer dumps ticker list (fixes "Mubarak" bug); order card dry run checks whether another instance holds the instrument.
  AI-First Swing Trading (`WS-S0`–`WS-S5`, [docs/swing-trading-wave-plan.md](docs/swing-trading-wave-plan.md)):
  `ScannerEngine.evaluate` joins closed higher-timeframe indicator columns (`htf_*`) via backward `join_asof` with zero
  lookahead bias; `presets.py` provides 3 gate-aligned MTF swing presets (`swing-trend-pullback-1h`,
  `swing-sniper-pullback-15m`, `swing-squeeze-breakout-1h`); `ExitMonitor` enforces per-position `STRUCTURAL_STOP` and
  mechanical 50% scale-out at `TP1` (`REDUCE_TP1` / `TAKE_PROFIT_1`); Brain `SELL` cards support `reduce_pct` (`25`,
  `50`, `100`); the Brain has `scan_swing_setups` (`SwingRadarCard` with 1-click Chart/Debate/Order actions, plus
  optional `symbol` parameter with Binance Spot REST fallback and 6-decimal precision for sub-\$0.10 tokens),
  `show_price_chart` Entry/Stop/Target level lines, the `/swing [SYMBOL]` slash command, and dot-separated quote
  normalization (`MUBARAK.USDT` / `MUBARAK` $\rightarrow$ `MUBARAKUSDT`) across `get_klines`, `get_live_market_data`,
  `get_derivatives_context`, `get_market_regime`, and `scan_swing_setups`.
- **Web:** the 2026-10-12 UI critique is implemented ([docs/ui-ux-critique-2026-10-12.md](docs/ui-ux-critique-2026-10-12.md), #81):
  unknown states never read as healthy or empty, one session store, one kill-switch poller per account,
  PAPER and DEMO kill switches told apart. In #97, Pause/Resume/Stop strategy commands are hidden for AI-mandate
  instances in the UI blotter (Flatten is kept). `BrainWidgets.tsx` renders `SwingRadarCard`, `OrderCard` R:R geometry
  bars and scale-out badges, and `PriceChartCard` Entry/Stop/TP1 overlays; `ScannerBuilder.tsx` includes 1-click Swing
  Preset templates and valid `ScanSpecCreate` submission.
- **Mandates:** `Brain XRPUSDT.BINANCE` is ACTIVE at L1 (created 2026-10-13 from a Brain order card; $10,000
  budget, 10% position limit, one open XRP position). `BTC-ETH spot L1` and `WLDUSDT.BINANCE` are PAUSED
  (reduce-only); the older XRP mandate is ARCHIVED. On 2026-10-17 12:04 UTC two poisoned RESUME commands were marked
  FAILED, unblocking the WLD trailing stop which sold 1,871 WLD at 0.5131. At 12:08 UTC the manual 0.03 BTC position
  was closed by the operator. Every risk-adding order waits for the operator's approval.
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
- `agent_worker` has `SYSTEM_REDIS_URL` (Redis DB 0, the system keys: ingest universe, heartbeats, perp marks). A
  container created before this change does not have it: recreate `agent_worker`
  (`up -d --force-recreate --no-deps agent_worker`) to pick it up.

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

Last full results (2026-10-17, #97): pytest 2815 passed / 0 failed; Playwright 64 passed (on throwaway DBs).

## Operator actions taken (15–17 Oct 2026)

| When (UTC) | Action |
|---|---|
| 15 Oct 18:37 | DB backup `cryptonite-2026-10-15T1837Z.dump`; migration 045 → 046; Wave 1 (#92) deployed 18:47 |
| 16 Oct | Security hotfix (#93) and Wave 2 (#94) deployed; `agent_worker` recreated with `SYSTEM_REDIS_URL` |
| 16 Oct 12:17 | DB backup `cryptonite-2026-10-16T1217Z.dump`; migration 046 → 047; Wave 3 (#95) deployed |
| 17 Oct 12:04 | Two poisoned RESUME commands marked FAILED (operator-approved); WLD trailing stop then sold 1,871 WLD at 0.5131 |
| 17 Oct 12:08 | Manual 0.03 BTC position closed by operator |
| 17 Oct | #96 and #97 deployed and verified (no poller errors; derivatives and regime live for WLD, XRP, SOL, BTC) |

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
8. **Decisions:** whether to resume a mandate (BTC exposure was 9.6% of a 10% limit; manual 0.03 BTC long closed);
   the WLD mandate and the wider symbol onboarding list; deleting strategy `ai-gen-62f49193` in the UI.
9. **Live Brain checks (paid model):** the System state block in a Brain reply, and a BUY card on a PAUSED mandate
   (it must be refused, with the link to Mission Control, before the TOTP code is spent).
10. **Live Brain checks for Wave 3 (paid model):** a structured order card (thesis, evidence,
    invalidation), `/postmortem XRP`, and an old debate card from before Wave 3 (it must not read "Debate failed").

## Open items and follow-ups

### Follow-ups
- A transient error after a compensated effect still retries (pre-existing, pinned by a test).
- Declare `cryptonite-orchestrator` as a dependency of the API package.
- The spot klines path ignores `Retry-After` after a 429.
- Fill lines echo a stored card's instrument without the new format check (low risk).
- Descoped: trace-id linking, historic stop blind time, semantic lesson recall.

### Next
- **Wave 4:** score the Brain's cards against outcomes, check its numbers come from tools, Brain shadow comparisons,
  Mission Control panels, offline model review.
- **Wave 5:** needs ADR 0008 approved first (operator feed, briefings, chat-set alerts, memory across conversations).

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
- Command queue runbook: [docs/runbooks/command-queue.md](docs/runbooks/command-queue.md) (handling stuck, failed, or unknown commands).
- Browser QA: [docs/runbooks/browser-qa-agent.md](docs/runbooks/browser-qa-agent.md) (instructions for an agent that
  tests every page, places paper trades and runs a strategy instance; reports go to `~/Documents/projects/cryptonite/qa/`).
- Backups: `~/backups/cryptonite/` (`db/`, `data/`, `redis/`, `secrets/`, drill logs, `backup.log`); runbook
  [docs/runbooks/backup.md](docs/runbooks/backup.md).
- Reference: [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), [docs/ai-first-program.md](docs/ai-first-program.md),
  [docs/USER_MANUAL.md](docs/USER_MANUAL.md), [docs/adr/](docs/adr/), [docs/runbooks/](docs/runbooks/).
