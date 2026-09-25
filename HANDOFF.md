# HANDOFF

## AI-First Program — Phase 1 complete (2026-09-25)

| Item | Status | Commits |
|---|---|---|
| A1.1 Pydantic AI spike | PASS — Flash 40/40, Pro 15/15; 100% tool chains, 100% valid structured output; Flash p50 1.2 s vs 4.2 s antigravity baseline; $0.20 total. [ADR 0001](docs/adr/0001-ai-provider-layer.md) | `a5896ae` |
| A1.2 gateway + migration 022 | `cryptonite_agents.gateway` (routes, pricing, registry, permission, budget, ledger, pydantic-ai backend) | `8dd3399` |
| A1.3 budget guard | $25/day (D12), priority stop order, exits never blocked, unknown spend blocks optional/interactive | `8dd3399` |
| A1.4 port | chat personas + debate fleet + reflection on the gateway; typed verdicts, invalid → FAILED | `6893c38` |
| A1.5 tool registry + permission gate | tools tagged READ/REDUCE/ADD; SQL tool read-only, single statement, NullPool | `8dd3399` |
| A1.6 evals v1 | `python -m cryptonite_agents.evals run --set v1 --model …`; migration 023 | `dcb968b` |
| A1.7 remove antigravity | removed from pyproject, uv.lock and both worker images (rebuilt; import check passes) | `8dd3399` + next commit |

- Independent review: FAIL → fixed → PASS. The critical finding: the model-callable SQL tool could write the cost ledger (disabling the budget cap) and, in the debate worker, reroute trade-decision models. Fixed with the `cryptonite_ai_telemetry` role, a read-only `cryptonite_agent_ro` (`default_transaction_read_only=on`), and protected routes.
- Gates: pytest **409 passed / 1 failed** on an isolated DB (same `test_instruments_endpoint` reference-data case); AI suites 53/53; no test can call Gemini (conftest unsets the key).
- Dev DB at `023`; LLM cap now 25.00; roles verified (agent_ro cannot write telemetry or read traces).
- **Deploy notes:** set `AI_TELEMETRY_DB_PASSWORD` before running migration 022 outside dev; agent_worker/debate_worker need `AI_TELEMETRY_DATABASE_URL` + `AGENT_TOOLS_DATABASE_URL` (compose has dev values). Without the telemetry URL, spend counts as unknown and only debates/exits run.
- Known, deliberate: spend can be ~10 s stale across processes (no reservation); debate worker handles entry and exit queues in one consumer (bounded by the 90 s model timeout); `exit.verdict` fails closed.
- Next: Gate 1 sign-off, then Phase 2 (cost controls).

## AI-First Program — Phase 0 complete (2026-09-25)

Plan: [docs/ai-first-blueprint.md](docs/ai-first-blueprint.md) (approved; goal §13, assignments §14). Branch: `feat/ai-first-phase0` (from `main`, **not pushed**; `main` fast-forwarded locally to `eebd747`, not pushed).

| Item | Status | Commits |
|---|---|---|
| A0.1 triage of unowned work | Done. Committed in topic groups; the debate approval was fixed (side mapping, refuses HOLD, paper equity, 1–25% bound, atomic) and the new endpoints got operator auth + CSRF; fake scanner presets dropped; scratch files → ignored `scratch/`; `base.py` kwargs change reverted (a no-op on Nautilus rc4, which takes config kwargs in `__new__`) | `f4748f0`..`f0cf84e` |
| A0.2 dev DB at head | Already done on 2026-09-24 (was at 020) | — |
| A0.3 `agent_safe_positions` contract | Migration `021`: open positions only, signed `size`, `unrealized_pnl_pct`/`weight_pct` in percent; applied to the dev DB | `4d87961` |
| A0.4 paper fidelity | Working orders matched against live quotes (limits respected, rested limits fill at their limit, no quote = no fill, strategy orders now fill); latch/exit/retry safety; funding history REST backfill. Independent review: PASS after 4 rounds | `a4eea77`, `26ec837` |
| A0.5 kill switch → PAPER | Operator consoles default to PAPER (+ explicit DEMO panel) | `4063e74` |
| A0.6 freeze autonomous mode | Debate worker ignores `DEBATE_EXECUTION_MODE=autonomous` | `598b4ee` |
| A0.7 gates | pytest **370 passed / 1 failed** on an isolated migrated DB (`test_instruments_endpoint` needs reference instruments a fresh DB lacks); no hang. Web typecheck + lint ✅, Playwright **16/16** ✅ (Research Lab spec fixed `3e53632`) | — |

**Important for anyone running tests:** the compose `test` service points at the **dev DB**, and `test_alembic_roundtrip_downgrade_and_upgrade` downgrades that DB to `base`, wiping operator users and reference data. Run the full suite against a throwaway DB instead:
```
docker exec infra-postgres-1 psql -U cryptonite -d cryptonite -c "CREATE DATABASE cryptonite_ci"
docker compose -f infra/docker-compose.yml run --rm -e DATABASE_URL=postgresql://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e ASYNC_DATABASE_URL=postgresql+asyncpg://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite_ci -e REDIS_URL=redis://redis:6379/15 test sh -c "uv run python -m alembic -c infra/migrations/alembic.ini upgrade head && uv run pytest tests -q"
```

- **Running stack:** the orchestrator and derivatives_feed containers still run the code from before A0.4 until they are restarted (`docker compose -f infra/docker-compose.yml restart orchestrator derivatives_feed`). On restart, working orders from before are reconciled to CANCELED.
- **Still uncommitted:** `exit_monitor.py` (threshold now in percent to match migration 021; still needs `debate_runs.type` or a switch to `trigger_source`), `.agents/`, the `graphify-out/*.json` ignore rule, graphify cache housekeeping.
- Paper-matching limitations: see AUDIT_STATUS.md.
- **Next: Gate 0 sign-off by the operator, then Phase 1** (A1.1 Pydantic AI spike).

## Paper Trading Readiness — SIGNED OFF (2026-09-24, with the limits below)

Architecture reference: [docs/perpetuals-architecture.md](docs/perpetuals-architecture.md). Branch `feat/perpetuals-phase1` pushed to `origin` for review (PR pending); `main` itself is still unpushed.

**Status of the pre-flight streams**
1. Daily drawdown breaker + UTC rollover — done (`0b536a3`). The orchestrator risk monitor (1 s) trips + persists the PAPER kill switch on a ≥5% drawdown from the day's high-water mark, writes `paper_risk_events`, and alerts on `channel:system_alerts`. The base is re-set at 00:00 UTC.
2. Trade-sidebar kill switch → PAPER — done (`350b249`, `0b536a3`). `/control` trip for PAPER latches the engine via DB sync (≤1 s); `/control` reset (step-up auth) is claimed by the paper orchestrator (expiry + generation checks, refused while exposure is open). Reduce-only perp orders pass the latches; entries do not.
3. Quote coalescing — done (`3738305`, spot follow-up committed right after). Perp and spot quotes are capped at 50 Hz/symbol. Measured live: ~8–10 quotes/s per instrument, newest < 100 ms old, 0 WS reconnects. Unthrottled spot (~160/s) had starved ingest (perp ~2/s, reconnects every ~20 s).
4. Single-owner perps — done (`0b536a3`). Another strategy instance cannot trade a perp position it does not own (`PERP_POSITION_OWNED_BY_OTHER_INSTANCE`).
5. Certification round trip — PASSED on the live stack (postgres, redis, ingest, derivatives_feed, orchestrator, api), BTCUSDT-PERP, 10x ISOLATED:
   - 0.01 BTC rejected by the approved 1% floor (844 < 1,000 USDT); 0.02 BTC short filled → row SHORT / 0.02 / 10 / ISOLATED, liq 92,479.10 > entry 84,408.20.
   - Trade-sidebar trip (the exact SQL `/control` runs) → engine latched → entries `KILL_SWITCH_ENGAGED`; reset refused with the position open (`RESET_REJECTED: open orders=0, open positions=1`).
   - A reduce-only BUY under the latch closed it (row FLAT / 0 / closed_at / realized −1.69); `/control` reset then succeeded and the PAPER row unlatched.
   - Reopened the short, restarted the orchestrator → `Rehydrated 1 open perp positions`. An oversize reduce-only was rejected; a reduce-only BUY 0.02 filled at the live ask (0.0000% drift) → FLAT.
   - Ledger reconciled to the cent: cash 100,000 + gross 0.264 − fees 6.752392 = 99,993.511608 = DB cash = realized; equity = cash.
   - Live gateway check: `/ws/stream` subscribe `perpetuals` → snapshot + live `perp_mark_tick`.
   - Afterwards `PAPER-ACCOUNT-001` was reset to 100,000 via `packages/database/seeds/seed_paper_trading.sql`; certification rows are kept as history.

**Operating limits for the paper test (known, accepted)**
- Perps: BTCUSDT / ETHUSDT only (reference margin tiers, not live Binance brackets). Leverage 1–20; notional ≥ 1% and ≤ 25% of equity per position.
- Perps are manual-only per symbol: one owning instance per position (don't let the AI debate worker trade the same perp).
- Fees: taker 0.1% for perps too (Binance USD-M ≈ 0.05%, so perp PnL is understated). Liquidation clearance 0.5% (> BTC tier-1 MMR 0.4%), so isolated liquidations lose the full margin.
- Fills: MARKET and LIMIT fill immediately at the current bid/ask (limit price not honoured). Hardcoded 60,000 fallbacks apply if a quote is missing (quotes are live now).
- Funding settles only while the feed/orchestrator are up; missed settlements aren't backfilled. Cumulative funding isn't a separate DB column.
- The daily high-water mark resets on orchestrator restart (the kill-switch latch persists).
- The orchestrator bind-mounts the working tree, so it also runs other authors' uncommitted `packages/execution` edits (`demo_pipeline.py`).
- Don't run pytest while the stack runs: outbox relay tests share the DB/Redis and fail (5 failures live; 331/331 with services stopped).
- Ingest after a long outage replays gap recovery in 1-minute REST steps, and closed-bar commits run inline in the WS reader. Expect ~1–2 min of degraded quotes after a restart with a data gap.

**How to run it**: `docker compose -f infra/docker-compose.yml up -d postgres redis ingest derivatives_feed orchestrator api`, then `cd apps/web && pnpm dev` (or `pnpm build && pnpm start`), log in as an operator (users restored from the pre-rebuild backup), open **Trade** in the terminal Panels bar.
- Dev DB rebuilt from migrations at `020` (reference data restored: operator_users, instruments, strategies, strategy_versions). The pre-rebuild backup `cryptonite_pre_rebuild_20260924_182038.dump` (898 MB) is in the session scratchpad under `/private/tmp` — move it somewhere permanent if you want to keep it.

## Current State
- Active branch: `feat/perpetuals-phase1` (perpetual futures Phase 1, paper). Plan: `docs/perpetuals-phase1-blueprint.md`. Roadmap C0–C8 done (user numbering; C4 = dispatcher perp guards / blueprint C8, C5 = perp fill accounting + direction-aware controls / blueprint C7+C9, C6 = perp-aware bridge persistence, C7 = liquidation engine / blueprint C10 minus funding, C8 = orchestrator mark/param/liquidation wiring, C9 = `derivatives_feed` compose service, C10 = funding settlement, C11 = WebSocket perp relay, C12 = web store/HUD/ticket). `aae1b96` (ingest futures stream split) cherry-picked onto the branch. C12.5 fixed web-order routing to the paper orchestrator; C13 added the Playwright spec. Next: decide the `/paper/*` default-environment question (Active Work), bring the dev DB to head, and fix the stale Research Lab e2e spec
- `main` @ `c63576e`, 14 commits ahead of `origin/main`, NOT pushed. Working tree NOT clean: 28 modified, 212 deleted, 20 untracked (backend packages/services, docs, graphify cache, `exit_monitor.py`, 015's LLM budget change 10→18 USD)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web at C13: typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright (`cd apps/web && npx playwright test`, needs `pnpm build` first because `webServer` runs `pnpm start`): 13 passed / 1 failed. `perpetuals-cockpit.spec.ts` 2/2, 3/3 repeat runs green. The failure is pre-existing: `research_lab.spec.ts` expects "RSI Period:", which no longer exists anywhere in `apps/web/src`. Dev-server smoke test (API offline): Trade sidebar renders the HUD and ticket; the PERP toggle, 10x chip/slider, and reduce-only validation work; no React/hydration errors; only leverage + margin mode persist
  - Backend at C12.5 (committed `paper.py`): pytest ✅ 314 passed / 0 failed, 12 deselected (312 at C12 + 2 environment-routing tests)
  - C3 live smoke test (2026-09-24): BTCUSDT/ETHUSDT mark ticks and open interest cached from the public Binance endpoints
  - Backend on **HEAD** (clean checkout): ☐ not re-run since `c63576e` committed the orchestrator `server.py` change the old hang depended on
  - Migrations: `alembic upgrade head` ✅ 001→020 on a fresh database; 020 downgrade/upgrade round trip ✅; 020 constraints exercised (spot SHORT, spot margin mode, spot leverage, CROSS with isolated margin, non-perp funding rows all rejected)
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; the `test` service mounts the working tree, so it tests uncommitted code too
- Alembic config lives at `infra/migrations/alembic.ini` (not `infra/alembic.ini`)

## Known pre-existing issues
- Dev DB `cryptonite` is stamped at `013`, but its 014–016 tables were created by hand (`run_migration.sql`). Its `agent_safe_positions` view (`size`, `weight_pct`) differs from migration 014's, so `alembic upgrade head` against it will fail at 014 (`CREATE OR REPLACE VIEW` cannot drop columns). Recreate it from migrations or reconcile the view before upgrading. The test suite does not need 017–020 applied yet
- `agent_safe_positions` has three incompatible contracts: the dev DB view, migration 014, and the consumers `exit_monitor.py` (`unrealized_pnl_pct`) and `debate_worker` (`size`)
- `test_benchmarks.py::test_duckdb_query_p95_latency`: wall-clock threshold; flaky under Docker VM load
- `test_100k_bars_peak_rss_benchmark` headroom is thin in the full suite (573–648 MB vs 700 MB)

## Active Work / Blockers
- Resolved (C12.5): web manual orders are enqueued as `PAPER` / `PAPER-ACCOUNT-001`, so the paper orchestrator claims them (verified on a migrated DB with the orchestrator's own claim query).
- DECISION PENDING (other author's uncommitted `routes/paper.py`): it changes `_enqueue_command`'s **defaults** to `PAPER` / `PAPER-ACCOUNT-001`. Manual orders (C12.5) and the /paper kill switch (pre-flight fix) are now explicitly PAPER. The default change would additionally move `DEPLOY`/`PAUSE`/`RESUME`/`STOP`/`CLOSE_POSITION`/`CANCEL_ORDER` from the DEMO executor to the orchestrator (which claims all of them) — owner: unassigned
- The terminal's `KillSwitchPanel` (`/control/kill-switch/*`) still targets `DEMO` / `DEMO-ACCOUNT-001` (hard-coded in the component), so from the Trade sidebar it does **not** stop the paper engine. Use the Portfolio console's kill switch (`/paper/kill-switch`) for paper, or point the panel at PAPER — owner: unassigned
- Perpetuals C1: migration `020` (SHORT side for perp rows, leverage/margin columns, `funding_rates`) and `cryptonite_execution.derivatives` models. `chk_paper_position_margin_mode` still allows NULL on perp rows because `EventBridgeWorker` doesn't write `margin_mode`; tighten in C9
- Perpetuals C2: `cryptonite_execution.margin`, pure functions for IM, tiered MM, uPnL, exact liquidation price (isolated + cross, tier chosen self-consistently at the trigger notional), margin ratio, and liquidation distance (a fraction, 0.05 == 5%). BTCUSDT/ETHUSDT brackets are a **reference snapshot** of Binance's published tables, not live data. `cum` is derived from tier floors and MMRs. Replace with `GET /fapi/v1/leverageBracket` in C11 before any live use
- Perpetuals C3: `services/scanner/cryptonite_scanner_service/derivatives_feed.py` (`python -m cryptonite_scanner_service.derivatives_feed`; env `PERP_SYMBOLS`, default BTCUSDT,ETHUSDT).
  - Mark ticks go to `perp:mark:{symbol}` (15s TTL) and `channel:perp_mark_ticks`.
  - Open interest is polled every 30s into `perp:oi:{symbol}` (90s TTL).
  - Funding settlements are detected from `T` rollovers and written to `funding_rates` (`STREAM_SETTLEMENT`, deduplicated).
  - Not yet wired: no compose service, and `StreamHub` does not forward `channel:perp_mark_ticks` (C11). Settlements missed while disconnected need REST backfill (C4).
  - Binance USD-M market streams only deliver on `wss://fstream.binance.com/market/...`; the legacy `/ws` and `/stream` paths connect but stay silent. `services/ingest/cryptonite_ingest/stream.py` still uses the legacy futures URL for klines/bookTicker (flagged as a separate task).
  - The untracked `services/ingest/cryptonite_ingest/worker.py` it replaces was removed from the tree; a copy is kept outside the repo in the session scratchpad.
- Perpetuals C4: `GuardedOrderDispatcher` perp branch (`risk.py`).
  - Leverage 1–20 (default 1) and margin mode ISOLATED/CROSS (default CROSS). Spot rejects leverage > 1, `margin_mode`, and `reduce_only`.
  - Reduce-only: `REDUCE_ONLY_NO_POSITION`, `REDUCE_ONLY_INVALID_DIRECTION`, `REDUCE_ONLY_OVERSIZE`. The size cap also subtracts resting reduce-only orders. Reduce-only orders skip the conviction multiplier, the 1% floor, the concentration check, and margin reservation.
  - Entry orders: 1% of equity minimum notional (`ORDER_BELOW_MIN_NOTIONAL`), 25% cap on |position + order| × mark, and initial margin + taker fee reserved on the exposure the order opens.
  - Equity = cash + spot longs at bid + perp uPnL at mark.
  - C5 replaced the C4 interlock. `perp_trading_enabled` now defaults to True and acts as a kill flag (`PERP_TRADING_DISABLED` when False).
- Perpetuals C5: signed-quantity perp fill accounting in `GuardedOrderDispatcher._handle_perp_fill`.
  - Open/add uses a weighted average entry; reduce/close realizes PnL.
  - A flip is a close leg followed by an open leg at the fill price. A reduce-only overfill never opens the opposite side.
  - Cash moves only by realized PnL and fees. Initial margin moves from the order reservation into `locked_margin` and is released pro rata. `available_cash` = cash − reserved − locked margin.
  - Direction-aware controls:
    - `RESET_SWITCH` counts perp positions of either sign (`open_position_count`).
    - `CLOSE_POSITION` (engine + orchestrator) closes perps with an opposing reduce-only order (`flatten_perp_position`).
    - `PAUSE` cancels perp entries on both sides and keeps reduce-only exits (`is_entry_order`).
    - `dispatch()` now records `instance_id`, so targeted PAUSE/STOP actually match orders; before this they cancelled nothing.
- **Perp gaps before production use (in priority order):**
  1. ~~Bridge persistence spot-only~~ — resolved in C6 (below). Still open: tighten `chk_paper_position_margin_mode` to NOT NULL for perps (the bridge now writes it; left for a later migration because C6 had to stay within 020's constraints). `paper_trades` has no realized-PnL column; realized PnL is on the position row only.
  2. ~~No liquidation / funding~~ — liquidation since C7/C8, funding settlement since C10.
  3. ~~Orchestrator mark/param wiring~~ — resolved in C8; `derivatives_feed` runs as a compose service since C9.
  4. The dev DB is still at 013, so `funding_rates` (020) doesn't exist there. Each funding settlement the feed detects will fail its insert, drop that row, and make the mark stream reconnect once. Bring the dev DB to head (see Known pre-existing issues) before relying on funding history.
- Perpetuals C6: `EventBridgeWorker._persist_perp_fill` (`packages/execution/cryptonite_execution/bridge.py`; there is no orchestrator `bridge.py`).
  - Perp `ORDER_FILLED` events carry the dispatcher's post-fill snapshot (`dispatcher.fill_event_fields` → `realized_pnl`, `position`: signed qty, entry, leverage, margin mode, isolated margin, exact liquidation price, maintenance margin), and the bridge writes that snapshot rather than re-deriving positions in SQL.
  - Rows are LONG/SHORT with absolute quantity. A close or flip marks the open row FLAT/CLOSED with realized PnL (net of the fill's fee); a flip then opens a new row.
  - Perp cash moves only by realized PnL and fees. Account equity = cash + spot value + perp uPnL.
  - The mark-to-market projector and orchestrator position publisher now value shorts with the correct sign.
  - A perp fill without a snapshot logs an error and leaves `paper_positions` alone (fee still settles).
  - Spot path is unchanged.
  - `tests/migrated_db.py` is the shared throwaway-DB-at-head helper (used by the feed and bridge tests).
- Perpetuals C7: liquidation engine.
  - `GuardedOrderDispatcher.check_liquidations(mark_prices)` triggers on long mark ≤ P_liq and short mark ≥ P_liq, using the exact tiered liquidation price. Positions are checked largest MM first, re-evaluated after each close (cross shares the wallet).
  - A breach cancels resting orders on the symbol and closes the full size with an opposing reduce-only order at mark. ISOLATED fills at the bankruptcy price if mark gapped through it. Taker fee plus a 0.5% clearance fee (`liquidation_clearance_pct`) are charged.
  - Losses stop at the isolated margin (ISOLATED) or zero cash (CROSS). The shortfall is credited back (`shortfall_covered`), and shortfall + gap cost accrue to `insurance_fund_deficit`. It runs regardless of the kill switch, and symbols without a bracket table are skipped with a warning.
  - With BTC tier-1 MMR 0.4% < 0.5% clearance, an isolated liquidation at P_liq always loses exactly its margin.
  - `PaperTradingEngine.process_liquidations` enqueues an `ORDER_FILLED` event with `liquidation: True` and `shortfall_covered`; the bridge creates the missing `paper_orders` row and closes the position row. It also publishes `perp.liquidation` on `channel:perp_liquidations`, which `StreamHub` does not forward yet (C11).
  - Positions now remember their `instance_id` so liquidation rows land on the right `paper_positions` row.
- Perpetuals C8: orchestrator wiring (`services/orchestrator/cryptonite_orchestrator/server.py`).
  - `consume_perp_marks` seeds from `perp:mark:*`, follows `channel:perp_mark_ticks`, applies ticks with their exchange event time (`update_mark_price(inst, px, ts_event_ns)`), and every 1s calls `engine.process_liquidations` with **fresh marks only**, so a stalled feed never liquidates on stale prices. It resubscribes after errors.
  - `MANUAL_ORDER`/`SUBMIT_ORDER` forward `leverage`, `margin_mode`, and `reduce_only` into `OrderIntent`. Malformed values reject the command (`INVALID_ORDER_PARAMETERS`) instead of raising and leaving it RUNNING for lease retry.
  - Startup rehydration (`rehydrate_dispatcher_state`) no longer loads perp rows into the spot `positions` map (a short would have come back as a spot long). It reloads them with `set_perp_position` (signed qty, leverage, margin mode, instance, locked IM).
  - `risk.py`: marks carry timestamps, and perp entry orders are rejected with `STALE_MARK_PRICE` when the mark is older than 10s (`max_mark_age_s`). Reduce-only orders are exempt. A mark without a timestamp counts as stale.
- Perpetuals C9: `derivatives_feed` service in `infra/docker-compose.yml`.
  - Scanner image, `python -m cryptonite_scanner_service.derivatives_feed`, `PERP_SYMBOLS=BTCUSDT,ETHUSDT`, depends on healthy postgres/redis, `unless-stopped`, default network (needs outbound internet).
  - **No `env_file`**: public market data only, so the `.env` exchange/LLM/TOTP secrets are not injected.
  - `orchestrator` depends on it (`service_started`).
  - Verified 2026-09-24: image built, the container (uid 1000) cached live BTC/ETH `perp:mark:*` and `perp:oi:*` from Binance, then it was removed again. The stack was left as found (postgres + redis only).
- Perpetuals C10: funding settlement.
  - `margin.calculate_funding_payment(size, mark, rate) = -size * mark * rate`.
  - `GuardedOrderDispatcher.apply_funding_settlement(instrument, mark, rate, funding_time_ns)`:
    - Moves cash (and daily realized PnL) and tracks `cumulative_funding` per position.
    - For ISOLATED positions it also moves the isolated margin (Binance semantics), so a funding debit pulls the liquidation price closer.
    - Idempotent and monotonic per instrument (`last_funding_time_ns`). Timestamps are ns like every other funding/mark time.
  - `PaperTradingEngine.process_funding_settlement` enqueues `FUNDING_SETTLED`, publishes `perp.funding` on `channel:perp_funding_events` (not forwarded by `StreamHub` yet), then runs a liquidation sweep on fresh marks.
  - `EventBridgeWorker` books it into account cash/realized PnL and the open position's realized PnL, isolated margin, and liquidation levels. The journal row's run-independent idempotency key `FUNDING:{account}:{instrument}:{T}` makes this exactly-once across restarts without a new migration.
  - The orchestrator's `consume_perp_marks` settles at the previous `T` with the last pre-rollover rate and mark when a tick's `next_funding_time_ns` rolls forward. Settlements missed while disconnected are not reconstructed.
  - Cumulative funding is not a separate DB column (020 has none); it is folded into `paper_positions.realized_pnl`.
- Perpetuals C11: `StreamHub._perp_pubsub_loop` (`services/api/cryptonite_api/stream_gateway.py`).
  - Relays `channel:perp_mark_ticks` / `channel:perp_liquidations` / `channel:perp_funding_events` to `/ws/stream` clients as `{"type": "perp_mark_tick" | "perp_liquidation" | "perp_funding", "data": {...}}`.
  - Client protocol: `{"action": "subscribe", "channels": ["perpetuals"], "instruments": ["BTCUSDT-PERP.BINANCE"]}`. Instruments are optional (empty = all), perp instruments are allow-listed (`DEFAULT_PERP_INSTRUMENTS`), and unsubscribe with instruments narrows the filter while unsubscribe without them stops perps.
  - The instrument filter applies to mark ticks only; liquidation and funding notices reach every perpetuals subscriber.
  - On subscribe, cached `perp:mark:*` snapshots are sent (`"snapshot": true`).
  - Mark ticks are droppable under backpressure (`enqueue_droppable`, which never evicts queued reliable events); liquidation/funding use the reliable path.
  - It runs on its own pub/sub connection and resubscribes after errors.
  - The other author's uncommitted `_pubsub_loop` change in the same file (agent heartbeat + debates) was left unstaged. C11 was committed as HEAD + perp hunks only.
- Perpetuals C12: web.
  - `apps/web/src/lib/stores/usePerpetualStore.ts` (project convention, not `src/stores/`). It holds marks, positions, the liquidation alert, and funding history, and persists only `leverage` + `marginMode` (`reduceOnly` deliberately not persisted).
    - `usePerpetualStream(wsConnected)` is mounted in `app/page.tsx`, so liquidation toasts arrive with the sidebar closed.
    - `usePerpetualPositionsPoll` polls `/paper/positions` + `/paper/accounts` every 5s, because margin fields are not on the WS snapshot.
  - `apps/web/src/lib/perp/margin.ts`: a TS port of `margin.py` (same reference brackets). Parity was checked against the Python golden vectors to 1e-8 (ad hoc Node script); there is no web unit runner in the repo.
  - `components/terminal/MarginRiskHUD.tsx`:
    - Liquidation distance meter (>20% SAFE green, 5–20% CAUTION amber, <5% DANGER pulsing red, text labels + `role=meter`).
    - Margin ratio (MM / isolated balance or account equity).
    - Funding rate (who pays) with an HH:MM:SS countdown and the liquidation banner.
  - `components/terminal/OrderEntryPanel.tsx`: a SPOT/PERP ticket.
    - Perp: 1–20x slider + 1/5/10/20 chips, margin mode (locked while a position is open), reduce-only with the dispatcher's rules mirrored client-side.
    - Pre-flight notional / IM / projected liquidation price, computed for the post-fill position including adds and flips.
    - Spot payload is unchanged.
  - Both are mounted in the terminal's operator sidebar, which now has a **Trade** toggle in the Panels bar (previously no control opened it).
  - `lib/ws.ts`: `addSubscription`/`removeSubscription` keep the `perpetuals` subscription across `updateSubscriptions` and reconnects.
  - API: `ManualOrderRequest` accepts `leverage` (1–20), `margin_mode`, and `reduce_only` and forwards them into the MANUAL_ORDER payload. Only these hunks were committed; the other author's `paper.py` changes stay unstaged.
  - Pre-existing web issues noticed: `app/page.tsx` subscribes SOL/BNB, which the gateway allow-list rejects (the whole subscribe message errors). `operator/ManualTradeModal.tsx` is unused.
- Perpetuals C13: `apps/web/e2e/perpetuals-cockpit.spec.ts` (API mocked with `page.route`, gateway mocked with `page.routeWebSocket`, no backend needed).
  - Ticket test: Trade sidebar → PERP → 10x chip syncs the slider → ISOLATED → reduce-only with no position shows the error and disables submit → LIMIT 0.05 @ 50,000 shows notional 2,500 / IM 250 / projected liq 45,180.72. The intercepted POST body is exactly `{…, leverage: 10, margin_mode: "ISOLATED", reduce_only: false}`.
  - HUD test: mocked isolated long + mark ticks → `CAUTION · 9.64%` then `DANGER · 3.87%` (meter `aria-valuetext`), margin ratio 4.00%, funding countdown `HH:MM:SS`, and a `perp_liquidation` banner that can be dismissed.
  - Both fail on React hydration/render errors.
- Paper pre-flight fixes (2026-09-24):
  - /paper kill switch → paper engine (see the commit `fix(orchestrator): route /paper kill switch…`).
  - Perp quotes: ingest ran spot only and keyed every bookTicker `{SYM}.BINANCE`, so `BTCUSDT-PERP.BINANCE` never had a quote or a freshness heartbeat, and every perp order would have been rejected (`NO_LIVE_QUOTE_IN_CACHE` / `DATA_STALE`). Ingest now also runs a quotes-only futures client (`INGEST_PERP_SYMBOLS`, `/public` bookTicker) keyed `{SYM}-PERP.BINANCE`. The orchestrator's lookup needed no change. Deliberately **not** mapped to the spot quote (spot/perp basis would skew fills and collars). Live check: 5.5k perp quotes in 6s.
- Uncommitted backend work (packages/agents, `demo_pipeline.py`, scanner, storage, api auth/routes, debate_worker, 015 budget change) has no recorded owner
- `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, untracked new cache, dated backup folders, and an uncommitted `.gitignore` rule are left for a decision

## Next Steps
1. Perpetuals C4: funding-rate history backfill (`/fapi/v1/fundingRate`, source `BINANCE_REST`) and the mark-price Parquet series per blueprint §1.2. Acceptance: gaps from feed downtime are filled idempotently
2. Re-run pytest on a clean checkout of `main` — acceptance: no hang; then push
3. Triage the remaining uncommitted backend changes into owned commits
4. Run `npx playwright test` — acceptance: all specs pass
