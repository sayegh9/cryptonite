# AUDIT_STATUS

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
