# HANDOFF

## Current State
- Active branch: `feat/perpetuals-phase1` (perpetual futures Phase 1, paper). Plan: `docs/perpetuals-phase1-blueprint.md`. Roadmap C0–C5 done (user numbering; C4 = dispatcher perp guards / blueprint C8, C5 = perp fill accounting + direction-aware controls / blueprint C7+C9). Next: perp-aware bridge persistence, then liquidation/funding in the engine (blueprint C10)
- `main` @ `c63576e`, 14 commits ahead of `origin/main`, NOT pushed. Working tree NOT clean: 28 modified, 212 deleted, 20 untracked (backend packages/services, docs, graphify cache, `exit_monitor.py`, 015's LLM budget change 10→18 USD)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web (committed; `apps/web` has no uncommitted changes): typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright ☐ not run
  - Backend on the **working tree** at C5: pytest ✅ 256 passed / 0 failed, 12 deselected (160 baseline + 9 perp model + 44 margin math + 10 derivatives feed + 21 perp dispatcher + 12 perp fill accounting tests)
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
  1. `EventBridgeWorker` persistence is spot-only. A perp SELL with no position writes no `paper_positions` row, and a BUY that covers a short creates a LONG row. Make it perp-aware (and write `margin_mode`, then tighten the 020 constraint) before the orchestrator can approve perp orders.
  2. No liquidation or funding settlement in the engine yet (blueprint C10). A losing perp position is never force-closed.
  3. The orchestrator never calls `update_mark_price`, so orchestrator perp orders are rejected `NO_MARK_PRICE`. It also doesn't pass `leverage`/`margin_mode`/`reduce_only` from the command payload into `OrderIntent`. Wiring marks makes gap 1 live.
- Uncommitted backend work (packages/agents, `demo_pipeline.py`, scanner, storage, api auth/routes, debate_worker, 015 budget change) has no recorded owner
- `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, untracked new cache, dated backup folders, and an uncommitted `.gitignore` rule are left for a decision

## Next Steps
1. Perpetuals C4: funding-rate history backfill (`/fapi/v1/fundingRate`, source `BINANCE_REST`) and the mark-price Parquet series per blueprint §1.2. Acceptance: gaps from feed downtime are filled idempotently
2. Re-run pytest on a clean checkout of `main` — acceptance: no hang; then push
3. Triage the remaining uncommitted backend changes into owned commits
4. Run `npx playwright test` — acceptance: all specs pass
