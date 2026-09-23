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
