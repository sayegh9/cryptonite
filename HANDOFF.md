# HANDOFF

## Current State
- Branch: `main` @ `aa7607b` + this Nautilus-fixture commit (uncommitted: web workspaces, e2e specs, docs, graphify refresh; staged: remaining compose changes — pgvector image, orchestrator `PAPER`, debate_worker replicas, `exit_monitor`)
- Graph: built at `494a893`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code): typecheck ✅ · lint ⚠️ warnings only · dependency/security ✅ · architecture ✅ · pytest ⚠️ full suite 159 passed / 1 failed / 0 errors, 12 deselected (2026-09-24; was 152 / 6 / 2 on 2026-09-23); only the memory benchmark fails · e2e ☐ not run
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; DSNs come from `tests/connections.py` (`DATABASE_URL` / `REDIS_URL`)

## Known pre-existing issues
- `test_full_stack_backtest_e2e::test_full_stack_backtest_lifecycle_e2e`: flaky — failed 1 of 2 full-suite runs on 2026-09-24, passes alone; not yet diagnosed
- `test_backtest_memory_benchmark::test_100k_bars_peak_rss_benchmark`: peak RSS 1055 MB vs 700 MB threshold (1024 MB container ceiling) — real regression

## Active Work / Blockers
- Done: `test` service keeps its uv cache and venv in the `test_uv_cache` / `test_venv` named volumes; warm run 3.8 s (cold 747 s)
- Uncommitted web refactor (`InteractiveModeToggle.tsx` deleted) pending commit — owner: Antigravity
- Patched: undefined `logger` in `tests/test_credentialed_binance_demo.py` cleanup (F821)
- Patched (spot-safe): removed the unused `is_perpetual` flag from `risk.py`; the SELL spot check applies to every instrument, so perpetual shorts stay rejected
- Resolved: `test_paper_risk_guardrails` bought 85% of equity and hit the 25% concentration limit; resized to 17.1% (4/4 pass)
- Resolved: `test_agent_worker_and_fleet` 401s came from a fixed HMAC nonce the server rejects as a replay (not `.env`); helper now generates a unique nonce (9/9 pass)
- Resolved: `test_full_stack_paper_trading_e2e` bought 25.6% of equity and hit the 25% concentration limit (no execution bug); starting equity raised to 20,000 so the 0.05 BTC buy is 12.8%
- Resolved: `test_nautilus_sandbox_spike.py` 2 setup errors; Redis host/port now come from `REDIS_URL` via `tests/connections.py` (5/5 pass)

## Next Steps
1. Resolve memory benchmark regression — acceptance: peak RSS < 700 MB
2. Diagnose flaky `test_full_stack_backtest_e2e` — acceptance: 5 consecutive full-suite runs pass it
3. Run `npx playwright test` — acceptance: all specs pass
