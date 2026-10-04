# Remediation Execution (overnight run, 2026-10-04 → 10-05)

Plan: [remediation-plan.md](remediation-plan.md). This page is the goal, the decisions taken so the
run needs no approvals, and the rules every workstream follows.

## Goal

Every item of the remediation plan implemented on branch `remediation/integration`, merged from
parallel workstreams without conflicts, and verified:

1. `uv run ruff check .` clean for changed files; `pnpm typecheck`, `pnpm lint`, `pnpm build` clean.
2. Full pytest suite on a **throwaway** database: 0 failed (known `performance`-marked tests excluded as today).
3. Playwright: all specs pass (API container stopped while running).
4. Model evals `triage_v3`, `triage_lake_v1`, `summarize_v1` no worse than the current baselines (S6).
5. A PR `remediation/integration` → `main` with a test report. The merge into `main` is the operator's.

Not deployed to the running stack (D5): live-path items R3.1, R3.2, R3.5, R7.2, R7.3. The orchestrator
is not restarted (a restart cancels resting paper orders).

## Decisions taken (defaults from the plan)

| # | Taken |
|---|---|
| D1 | On-Chain Analyst removed from the full debate; `/agents/onchain` chat kept with an honest prompt |
| D3 | Old backtest runs badged "legacy metrics"; `ema-cross` runs re-run locally after merge |
| D4 | Untouched (operator's call on audit data) |
| D5 | Live-path items merged and tested, not deployed until after the L2 review (~10-31) |
| D6 | New gate checks informational (`GateCheck.informational = True`); no threshold changes |

## Invariants (every stream)

- Never touch `GuardedOrderDispatcher` logic, the 1–25% clamp, the mandate check, kill switch, TOTP
  approvals or the outbox.
- Tests never call paid models and never use the dev DB `cryptonite`.
- Keep each change minimal and in the surrounding style; no drive-by refactors outside your files.

## Workstreams and file ownership

| Stream | Where | Items | Owns |
|---|---|---|---|
| **W** web | cloud | R1.1–R1.7, R1.8 (UI), R2.3/R2.4 (UI), R4.4, R6.1–R6.7 | `apps/web/**` |
| **Q** quant | cloud | R2.1–R2.4 (backend), R3.3, R4.1–R4.3, R4.5–R4.7, R5.0 (guard code), R5.1–R5.5 | `packages/backtester/**`, `packages/execution/cryptonite_execution/{stats,calibration,autonomy,autonomy_metrics}.py`, `packages/agents/cryptonite_agents/research/**`, `services/api/.../routes/{backtests,research,ai}.py` (only gate/report fields), `scripts/{gate_status,rerun_backtests}.py`, `docs/adr/0004-*`, migration **032** |
| **D** decision + ops | cloud | R3.1, R3.2, R3.5, R7.1, R7.2 (Python), R7.4, R7.5, R1.8 (backend), R7.10 | `packages/scanner/**`, `services/scanner/**`, `services/debate_worker/**`, `packages/agents/cryptonite_agents/{debate_fleet,fleet,tools}.py` (tools: news only), `services/orchestrator/**` (publish only), `services/api/.../routes/control.py`, `services/api/.../auth.py`, `services/notifier/**`, `scripts/build_triage_eval_set.py`, migration **033** |
| **L** local | workstation | R7.9 backups, R7.6, R5.0 backfill (data), R7.3, R7.7, chart indicator parity, integration, evals, re-runs | everything else; merges |

Overlap rule: if you must edit a file another stream owns, keep the edit to the smallest hunk and
say so in your final report.

## Contracts between streams

**`GET /api/v1/control/enforced-limits`** (D builds, W consumes). Operator auth.
```json
{"as_of": "2026-10-05T01:00:00Z" | null,
 "paper": {"max_daily_loss_pct": "5", "min_order_equity_pct": "1", "max_order_equity_pct": "25",
           "taker_fee_bps": "10", "account_backstop_drawdown_pct": "20"} | null}
```
`paper: null` means unknown (orchestrator has not published `risk:effective_limits` yet). The
orchestrator publishes that Redis key at start-up and every 60 s from its dispatcher's attributes.

**`POST /api/v1/control/risk-limits`** → `501` `{"detail": "Limits are not configurable here; edit the mandate"}` after auth + CSRF.

**Backtest metrics** (Q builds, W consumes): `metrics.metrics_version = 2`;
`metrics.benchmark = {"total_return", "sharpe_ratio", "max_drawdown", "alpha_vs_hold"}`;
`metrics.fees_paid`, `metrics.turnover`. Missing `metrics_version` = legacy.

**Gate report** (Q builds, W consumes): each check dict gains `"informational": bool` and optional
`"interval": [lo, hi]` (strings); `passed` of the report ignores informational checks.

**Research report** (Q): new fields `folds`, `dsr`, `holdout`, `robustness` (migration 032 adds
`holdout jsonb`, `dsr numeric`, `folds jsonb`, `robustness jsonb` to `research_reports`).

**Redis keys** (D): `risk:effective_limits` (JSON above, `paper` part), `sentiment:last_ok` (unix
seconds), `<brief_key>:data` (structured brief, same TTL as the brief).

**Migrations:** Q = `032_research_validation.py` (down_revision `031`), D = `033_decision_trace_id.py`
(down_revision `032`). D must set `down_revision = "032"` even though it does not have Q's file; the
integration merge brings both.

## Branches

Base `remediation/base` (origin/main + these docs). Streams push `remediation/w-web`,
`remediation/q-quant`, `remediation/d-decision`. Integration on `remediation/integration` (local).
