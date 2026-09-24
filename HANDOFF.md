# HANDOFF

## Current State
- Branch: `main` @ `1100fa2` + this docs commit; 12 commits ahead of `origin/main`, NOT pushed (see blocker below). Index clean. Working tree NOT clean: 37 modified, 212 deleted, 24 untracked (backend packages/services, migrations 014–019, docs, graphify cache, `exit_monitor.py`)
- Graph: built at `1100fa2`; run `graphify update .` after committing
- Gates (2026-09-24, Claude Code):
  - Web (committed; `apps/web` has no uncommitted changes): typecheck ✅ · lint ✅ 0 errors / 0 warnings · `pnpm build` ✅ · Playwright ☐ not run
  - Backend on the **working tree**: pytest ✅ 160 passed / 0 failed / 0 errors, 12 deselected (4 full runs today; 1 run had a timing-only DuckDB p95 miss, see below)
  - Backend on **HEAD** (clean checkout): pytest ⛔ hangs at `test_orchestrator_pipeline.py::test_command_poller_processes_deploy_command` (57%)
  - dependency/security ✅ · architecture ✅
- `test_full_stack_backtest_e2e` stress test: 5/5 isolated passes and 3/3 full-suite passes after the fix in `1100fa2`
- Pytest runs via `docker compose -f infra/docker-compose.yml run --rm test uv run pytest tests -q`; the `test` service mounts the working tree, so it tests uncommitted code too

## Known pre-existing issues
- `test_benchmarks.py::test_duckdb_query_p95_latency`: wall-clock threshold; failed 1 of 4 full runs today (66.8 ms vs 50 ms) while the Docker VM was busy
- `test_100k_bars_peak_rss_benchmark` headroom is thin in the full suite (573–648 MB vs 700 MB) because earlier tests leave ~480–550 MB resident

## Active Work / Blockers
- BLOCKER (push): HEAD's backend suite hangs in `test_command_poller_processes_deploy_command`. It passes only with the uncommitted `services/orchestrator/cryptonite_orchestrator/server.py` change (+489/−76), which needs its owner to review and commit — owner: unassigned
- Uncommitted backend work (packages/agents, execution `bridge.py`/`demo_pipeline.py`, scanner, storage, api auth/routes, migrations 014–019, debate_worker) has no recorded owner and is what the working-tree test gate is actually exercising
- `exit_monitor` is gated behind the `monitoring` compose profile. `exit_monitor.py` stays uncommitted until it lands with a P&L column on `agent_safe_positions`, `debate_runs.type` (or a switch to `trigger_source`), migrations 017–019, and the `debate_worker` exit-queue consumer — owner: unassigned
- graphify housekeeping: 212 deleted old-version cache files, an untracked new cache (`cache/ast/v0.9.66-s4`), dated backup folders, and an uncommitted `.gitignore` rule (`graphify-out/*.json`) are left for a decision on whether graph artifacts stay tracked

## Next Steps
1. Commit the orchestrator `server.py` change (or fix the hang at HEAD) — acceptance: full suite passes on a clean checkout of HEAD; then push
2. Triage the remaining uncommitted backend changes into owned commits — acceptance: working tree matches what the test gate covers
3. Run `npx playwright test` — acceptance: all specs pass
4. Decide graphify artifact tracking (`.gitignore` rule, cache, backups)
