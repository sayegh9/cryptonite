# AUDIT_STATUS

> **C4 status refresh (2026-10-06):** the tables below are a historical snapshot (graph/stub scan 2026-09-23, deterministic audit 2026-09-24, backlog as of 2026-10-04) and are partly stale — the deferred backlog LE-1…LE-10 is complete, and the product-review run since 2026-10-05 changed CI, migrations (head `038`), and the branch state. For current landing/runtime/deploy status use [`docs/HANDOVER-2026-10-06.md`](docs/HANDOVER-2026-10-06.md) (§0 status matrix, §5 open PRs, §7 hold-release gates). Keep this document as the point-in-time audit record; the final C4 refresh lands after the open PRs merge.

- Source: `graphify-out/GRAPH_REPORT.md` @ `51329c9`, stub-marker scan, gates run 2026-09-23
- Gates: typecheck ✅ · lint ✅ · pytest ⛔ not run (no `google-antigravity` wheel for macOS x86_64) · e2e ☐ not run
- Python "Complete" ratings are structural only until pytest runs

| Domain / Module | Status (Complete / Partial / Stub) | Missing Requirements | Risk / Notes |
|---|---|---|---|
| `packages/schemas` | Complete | — | Shared contract; changes ripple to all services |
| `packages/storage` | Complete | — | Hosts `NautilusCatalogManager` (god node, 45 edges) |
| `packages/scanner` | Complete | — | Golden indicator tests exist (unverified) |
| `packages/strategies` | Complete | — | `CryptoniteStrategy` bridges TS `types.ts` ↔ Python |
| `packages/backtester` | Partial | Forward-fill gap imputation in `cadence.py` | Discontinuous feeds rejected, not repaired |
| `packages/execution` | Partial | Live venue adapter; demo runs on `MockVenueAdapter` when `USE_MOCK_VENUE` is set | High risk: kill switch + risk invariants; live path unexercised |
| `packages/agents` | Partial | Real on-chain tool (mocked, `debate_fleet.py:43`) | `tools.py` cohesion 0.11 — split candidate |
| `services/api` | Complete | — | `OperatorAuthManager` god node (41 edges); `services/api/tests` has 1 file |
| `services/ingest` | Complete | — | — |
| `services/scanner` | Complete | — | — |
| `services/executor` | Partial | Live reconciliation skipped in mock mode | Mode is env-flag driven; pin in prod |
| `services/orchestrator` / `outbox` / `backtester` | Complete | — | Outbox relay has dedicated tests (unverified) |
| `services/debate_worker` | Partial | Real position-closed signal (`reflection.py:76` uses 1h age heuristic) | Reflection memory quality depends on heuristic |
| `services/agent_worker` | Stub | Package structure; single `server.py` | Unclear overlap with `debate_worker` |
| `apps/web` | Partial | Commit pending refactor; e2e run | typecheck + lint pass; `InteractiveModeToggle` deleted |
| Observability (cross-cutting) | Stub | OpenTelemetry tracing across workers | No distributed traces |

## Deferred backlog: marked for later (2026-09-29)

These were agreed to be done later, not in the current phase. Each item has an ID; when you pick one up, strike it here and put its commit next to it.

| ID | Item | Why it matters | Effort | Downloads |
|---|---|---|---|---|
| LE-1 | **Backfill market history**: at least 120 d of BTCUSDT/ETHUSDT, 1m and 1h, into the Nautilus catalog, and repair gaps. `scripts/backfill_gaps.py` writes the Analytics Lake; confirm or extend it so it also writes the catalog | The catalog holds about 19 d with gaps. Research needs 90 d in-sample + 30 d out-of-sample, and backtests over gappy days fail with `DataGapError` | about 2–3 h including checks | Binance public REST klines: about 350 requests of 1000 bars per symbol per 120 d at 1m, about 15–30 MB total, 5–15 min |
| LE-2 | **Run the exit monitor permanently** (`--profile monitoring`) and alert when it is down | Protects AI-owned positions. After the Phase 3 merge, Brain order approvals are refused while its heartbeat is missing | about 30 min (rebuild the scanner image with the heartbeat code, start it, verify the heartbeat) | none if the image cache holds; otherwise a PyPI re-download (see LE-7) |
| LE-3 | **Debate worker on redis-py 8**: watch for `Timeout reading` on its 5 s `XREADGROUP`; if it appears, set `socket_timeout` above the block time, as the research worker does | The research worker crashed on the same library behaviour | about 15–30 min | none |
| LE-4 | **Redis `requirepass`** across compose and all clients | Any container on the network can read and write queues and streams | about 1–2 h | none |
| LE-5 | **Least-privilege DB role for the backtester** (it connects as the owner `cryptonite`) | Limits what a compromised worker could do | about 1–2 h (migration, grants, compose) | none |
| LE-6 | **Research-report table** (reports are JSON files in `data/research_reports/`) | Queryable history for the digest and the Brain | about 2 h (migration after Phase 5's 025/026) | none |
| LE-7 | **Reproducible, faster images**: install from `uv.lock` (`uv sync --frozen`, as `ingest.Dockerfile` does) with a BuildKit cache mount, instead of unpinned `uv pip install` | Builds took about 45 min on slow PyPI and silently picked up redis-py 8 | about 2–3 h | one full dependency download to seed the cache (about 1–1.5 GB with nautilus_trader, pyarrow, polars, duckdb) |
| LE-8 | **Brain**: a per-turn cap on `stage_debate_proposal` (paid debates without approval, budget-capped); scope decision traces per operator if multi-user ever happens | Deferred Lows from the PR #1 review | about 1 h | none |
| LE-9 | **Known red tests**: `test_instruments_endpoint` (reference data) and `test_duckdb_query_p95_latency` (wall-clock flake) | Gates stay "N passed / 2 known failed" | about 1–2 h | none |
| LE-10 | **Dev-data cleanup**: probe strategy `ai-gen-b7342cd3` ("Sandbox Probe EMA") with its two backtest runs; the untracked `data/backtest_artifacts/77fa3037-…/`; test rows from `test_backtest_outbox_relay` / `test_worker_three_way_recovery_matrix`, which ran once against the dev DB on 2026-09-25 | Keeps the dev DB and the UI tidy | about 15 min | none |

### Backlog status (2026-10-04, workstation)

| ID | Status |
|---|---|
| LE-1 | **Done** `c2d906d`: `scripts/backfill_history.py` (catalog + lake, gaps only) + `repair_catalog_series.py`. BTC/ETH 1m and 1h 100.0% complete over 120 d; 324,631 bars; duplicates, an empty file pair and mixed-precision files repaired. |
| LE-2 | **Done** (workstation): exit monitor runs permanently; the fixed scanner image adds the same redis timeout fix to it (`c297d08`). |
| LE-3 | **Done** `f898a83` (debate worker), `c297d08` (scanner worker, exit monitor). |
| LE-4 | **Done** `c1622c5`: Redis `--requirepass ${REDIS_PASSWORD}`; all 15 client URLs carry it. Live after the next full redeploy; host tools need `redis-cli -a`. |
| LE-5 | **Done** `aed1b88`: migration 030, role `cryptonite_backtester`; compose uses it. |
| LE-6 | **Done** `dfec999`: migration 031 `research_reports`, API `/api/v1/research/reports`, digest reads it. |
| LE-7 | **Done** (see its commit): every Python image installs `uv sync --frozen --no-dev --all-packages` with uv 0.12.17 and a shared BuildKit cache. |
| LE-8 | **Done** `c0430ae`: `stage_debate_proposal` capped at 1 per turn. Per-operator trace scoping stays open (only for multi-user). |
| LE-9 | **Done** `b256ac6`: instruments test seeds its data; the two wall-clock/peak-RSS benchmarks are `performance`-marked. |
| LE-10 | **Done** (dev DB, 2026-10-04): 221 test strategies (+ their 11 runs), 92 test operator accounts, 12 E2E paper accounts and their rows, 2 untracked artifact dirs removed; core `ema-cross` renamed back. Backup of the touched tables: `~/backups/cryptonite-LE10-tables-2026-10-04.dump`. |

**New finding, fixed** `7469d9c`: `scan_runs` stored every lake file path + sha256 per run (~711 kB/row) and grew to 21 GB (14 GB on 2026-10-03 alone). Scanner now stores one digest per date partition and loads a bounded lookback; existing rows compacted, VACUUM FULL: 21 GB -> 251 MB.

## Deterministic Audit Findings (2026-09-24)

- Tools: `tsc --noEmit`, ESLint, `uvx ruff` 0.16.8 (repo config), `pnpm audit`, `uv lock --check`, `pip check` in the `infra-*` images, AST import scan, graphify @ `90427f7`

### Blockers
- None. Typecheck clean; 0 syntax errors (ruff `E9`); 0 ESLint errors

### Warnings
- Frontend: 100 `@typescript-eslint/no-unused-vars` warnings in 27 files (top: `brain/ActionInbox.tsx` 12, `app/page.tsx` 11)
- Python: ~180 unused imports/variables (F401 162, F841 15) of 598 ruff findings; ~370 others are auto-fixable style (`UP*`, `I001`)
- Bare `except:` at `packages/agents/cryptonite_agents/tools.py:264,269`
- Fixed: `tests/test_credentialed_binance_demo.py:334` referenced an undefined `logger` (F821); cleanup failures raised `NameError`
- Fixed (spot-safe): removed the unused `is_perpetual` flag from `packages/execution/cryptonite_execution/risk.py`. The SELL spot check applies to every instrument, so perpetual shorts stay rejected

### Architectural Debt
- 6 God Nodes (>30 edges): `NautilusCatalogManager` 47, `OperatorAuthManager` 42, `to_nautilus_bar_type()` 37, `DemoExecutionPipeline` 32, `TwoStoreCoordinator` 31, `LatchedKillSwitch` 30
- No import cycles; no packages→services or schemas→sibling imports; no dependency conflicts (`pnpm audit` clean, `pip check` clean)

## Paper matching known limitations (A0.4, 2026-09-25)

- Paper strategy position queries (`is_long`/`is_short`/`is_flat`) and `close_all` read the dispatcher's **account-level** ledger: strategies (and manual trades) sharing an instrument see and close the same position. Per-instance positions arrive with mandates (blueprint Phase 5).
- Working orders live in memory only; an orchestrator restart cancels them (their `paper_orders` rows are reconciled to CANCELED on startup).
- Fills are top-of-book, full size, no queue position; maker fills pay the taker fee (conservative). A capped exit (position shrank while resting) is recorded FILLED with `filled_qty < quantity`.
