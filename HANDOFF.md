# HANDOFF

## Current State
- Branch: `main` @ `494a893` + this test-fix commit (uncommitted: web workspaces, e2e specs, docs, graphify refresh; staged: remaining compose changes — pgvector image, orchestrator `PAPER`, debate_worker replicas, `exit_monitor`)
- Graph: built at `494a893`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code): typecheck ✅ · lint ⚠️ warnings only · dependency/security ✅ · architecture ✅ · pytest ⏸️ full suite not re-run (last run 2026-09-23: 152 passed / 6 failed / 2 errors); `test_paper_risk_guardrails` + `test_agent_worker_and_fleet` 13/13 ✅ · e2e ☐ not run
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; DSNs come from `tests/connections.py` (`DATABASE_URL` / `REDIS_URL`)

## Known pre-existing issues
- `test_nautilus_sandbox_spike.py` (2 errors): Redis hardcoded as `host="127.0.0.1", port=6379`; derive from `REDIS_URL`
- `test_full_stack_paper_trading_e2e`: `assert False is True` — undiagnosed
- `test_backtest_memory_benchmark::test_100k_bars_peak_rss_benchmark`: peak RSS 1055 MB vs 700 MB threshold (1024 MB container ceiling) — real regression

## Active Work / Blockers
- Done: `test` service keeps its uv cache and venv in the `test_uv_cache` / `test_venv` named volumes; warm run 3.8 s (cold 747 s)
- Uncommitted web refactor (`InteractiveModeToggle.tsx` deleted) pending commit — owner: Antigravity
- Patched: undefined `logger` in `tests/test_credentialed_binance_demo.py` cleanup (F821)
- Patched (spot-safe): removed the unused `is_perpetual` flag from `risk.py`; the SELL spot check applies to every instrument, so perpetual shorts stay rejected
- Resolved: `test_paper_risk_guardrails` bought 85% of equity and hit the 25% concentration limit; resized to 17.1% (4/4 pass)
- Resolved: `test_agent_worker_and_fleet` 401s came from a fixed HMAC nonce the server rejects as a replay (not `.env`); helper now generates a unique nonce (9/9 pass)

## Next Steps
1. Fix Nautilus Redis host via `tests/connections.py` — acceptance: 2 errors cleared
2. Diagnose `test_full_stack_paper_trading_e2e` assertion — acceptance: root cause logged or fixed
3. Resolve memory benchmark regression — acceptance: peak RSS < 700 MB
4. Run `npx playwright test` — acceptance: all specs pass
