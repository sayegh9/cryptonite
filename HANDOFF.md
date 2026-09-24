# HANDOFF

## Current State
- Branch: `main` @ `90427f7` (uncommitted: web workspaces, e2e specs, docs, graphify cache purge; staged: compose `test` service, test DSN refactor)
- Graph: built at `90427f7`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code): typecheck ✅ · lint ⚠️ warnings only · dependency/security ✅ · architecture ✅ · pytest ⏸️ not re-run (host is x86_64; use the Docker `test` service; last run 2026-09-23: 152 passed / 6 failed / 2 errors) · e2e ☐ not run
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; DSNs come from `tests/connections.py` (`DATABASE_URL` / `REDIS_URL`)

## Known pre-existing issues
- `test_nautilus_sandbox_spike.py` (2 errors): Redis hardcoded as `host="127.0.0.1", port=6379`; derive from `REDIS_URL`
- `test_agent_worker_and_fleet.py` (3): `401 == 200`; likely auth enabled by `../.env` in container — unconfirmed
- `test_full_stack_paper_trading_e2e`, `test_paper_risk_guardrails::test_conservative_cash_reservation_and_fill_reconciliation`: `assert False is True` — undiagnosed
- `test_backtest_memory_benchmark::test_100k_bars_peak_rss_benchmark`: peak RSS 1055 MB vs 700 MB threshold (1024 MB container ceiling) — real regression

## Active Work / Blockers
- `test` service re-downloads ~1 GB of deps per run (uv cache in ephemeral `/tmp`); persist cache/venv in named volumes — owner: unassigned
- Uncommitted web refactor (`InteractiveModeToggle.tsx` deleted) pending commit — owner: Antigravity
- Patched: undefined `logger` in `tests/test_credentialed_binance_demo.py` cleanup (F821)
- Patched (spot-safe): removed the unused `is_perpetual` flag from `risk.py`; the SELL spot check applies to every instrument, so perpetual shorts stay rejected

## Next Steps
1. Fix Nautilus Redis host via `tests/connections.py` — acceptance: 2 errors cleared
2. Diagnose 401s and paper-trading assertions — acceptance: root cause logged or fixed
3. Resolve memory benchmark regression — acceptance: peak RSS < 700 MB
4. Run `npx playwright test` — acceptance: all specs pass
5. Persist the `test` service uv cache/venv in named volumes — acceptance: second run skips the dependency download
