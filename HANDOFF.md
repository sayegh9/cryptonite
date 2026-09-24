# HANDOFF

## Current State
- Active branch: `feat/perpetuals-phase1` (perpetual futures Phase 1, paper). Plan: `docs/perpetuals-phase1-blueprint.md`. Roadmap C0–C7 done (user numbering; C4 = dispatcher perp guards / blueprint C8, C5 = perp fill accounting + direction-aware controls / blueprint C7+C9, C6 = perp-aware bridge persistence, C7 = liquidation engine / blueprint C10 minus funding). `aae1b96` (ingest futures stream split) cherry-picked onto the branch. Next: wire mark prices into the orchestrator (calls `update_mark_price` + `process_liquidations`), then funding settlement in the engine
- `main` @ `c63576e`, 14 commits ahead of `origin/main`, NOT pushed. Working tree NOT clean: 28 modified, 212 deleted, 20 untracked (backend packages/services, docs, graphify cache, `exit_monitor.py`, 015's LLM budget change 10→18 USD)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web (committed; `apps/web` has no uncommitted changes): typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright ☐ not run
  - Backend on the **working tree** at C7: pytest ✅ 279 passed / 0 failed, 12 deselected (268 at C6 + 11 liquidation engine tests)
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
  1. ~~Bridge persistence spot-only~~ — resolved in C6 (below). Still open: tighten `chk_paper_position_margin_mode` to NOT NULL for perps (the bridge now writes it; left for a later migration because C6 had to stay within 020's constraints). `paper_trades` has no realized-PnL column; realized PnL is on the position row only.
  2. ~~No liquidation~~ — resolved in C7 (below), but nothing calls it until gap 3 is wired. Funding settlement in the engine is still missing.
  3. The orchestrator never calls `update_mark_price`, so orchestrator perp orders are rejected `NO_MARK_PRICE`. It also doesn't pass `leverage`/`margin_mode`/`reduce_only` from the command payload into `OrderIntent`. Wiring marks makes gap 1 live.
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
- Uncommitted backend work (packages/agents, `demo_pipeline.py`, scanner, storage, api auth/routes, debate_worker, 015 budget change) has no recorded owner
- `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, untracked new cache, dated backup folders, and an uncommitted `.gitignore` rule are left for a decision

## Next Steps
1. Perpetuals C4: funding-rate history backfill (`/fapi/v1/fundingRate`, source `BINANCE_REST`) and the mark-price Parquet series per blueprint §1.2. Acceptance: gaps from feed downtime are filled idempotently
2. Re-run pytest on a clean checkout of `main` — acceptance: no hang; then push
3. Triage the remaining uncommitted backend changes into owned commits
4. Run `npx playwright test` — acceptance: all specs pass
