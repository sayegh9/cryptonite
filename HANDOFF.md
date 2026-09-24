# HANDOFF

## Current State
- Active branch: `feat/perpetuals-phase1` (perpetual futures Phase 1, paper). Plan: `docs/perpetuals-phase1-blueprint.md`. Roadmap C0–C8 done (user numbering; C4 = dispatcher perp guards / blueprint C8, C5 = perp fill accounting + direction-aware controls / blueprint C7+C9, C6 = perp-aware bridge persistence, C7 = liquidation engine / blueprint C10 minus funding, C8 = orchestrator mark/param/liquidation wiring, C9 = `derivatives_feed` compose service). `aae1b96` (ingest futures stream split) cherry-picked onto the branch. Next: funding settlement in the engine, StreamHub forwarding of perp channels (C11), and bringing the dev DB to head
- `main` @ `c63576e`, 14 commits ahead of `origin/main`, NOT pushed. Working tree NOT clean: 28 modified, 212 deleted, 20 untracked (backend packages/services, docs, graphify cache, `exit_monitor.py`, 015's LLM budget change 10→18 USD)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web (committed; `apps/web` has no uncommitted changes): typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright ☐ not run
  - Backend on the **working tree** at C8: pytest ✅ 287 passed / 0 failed, 12 deselected (279 at C7 + 8 orchestrator perp wiring tests; wiring file 5/5 repeat runs green)
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
  2. ~~No liquidation~~ — resolved in C7 and driven by the orchestrator since C8. Funding settlement in the engine is still missing.
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
- Uncommitted backend work (packages/agents, `demo_pipeline.py`, scanner, storage, api auth/routes, debate_worker, 015 budget change) has no recorded owner
- `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, untracked new cache, dated backup folders, and an uncommitted `.gitignore` rule are left for a decision

## Next Steps
1. Perpetuals C4: funding-rate history backfill (`/fapi/v1/fundingRate`, source `BINANCE_REST`) and the mark-price Parquet series per blueprint §1.2. Acceptance: gaps from feed downtime are filled idempotently
2. Re-run pytest on a clean checkout of `main` — acceptance: no hang; then push
3. Triage the remaining uncommitted backend changes into owned commits
4. Run `npx playwright test` — acceptance: all specs pass
