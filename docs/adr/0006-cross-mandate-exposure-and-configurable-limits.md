# ADR 0006: Cross-mandate exposure (F4) and configurable risk limits (F7)

- Status: Proposed (2026-10-05). Built and tested on `feat/f4-f7-risk`; **not deployed until after the L2 review** (D5).
- Context: [remediation plan](../remediation-plan.md) Phase 8 F4, F7; R1.8, N9, N18; [ADR 0004](0004-mandates-and-autonomy.md).

## Decisions

### F4: one cap on the combined BTC risk of all mandates

1. **Where it runs.** In the orchestrator's AI order path, inside `mandate_gate.gate_ai_order`, right after
   `check_mandate` accepts a risk-adding order and before `GuardedOrderDispatcher`. It never replaces
   either check, never resizes, and only refuses. Risk-reducing orders (spot SELL, reduce-only perps,
   closes) are never refused by it.
2. **What it measures.** `Σ |notional| × |β|` over every non-archived mandate's open positions (spot fills
   from `paper_orders`, instance-owned perps in the dispatcher) and working entry orders, plus the new
   entry's worst-case notional (market buffer and conviction resize, as the mandate check prices it).
   HALTED and PAUSED mandates count: their open positions are still account risk. Absolute values: a short
   or a negative beta does not offset another mandate's risk.
3. **Beta.** OLS beta of the instrument's 1h returns on BTCUSDT's over the last 30 days of the analytics
   lake (`cross_exposure.beta_from_closes`, pure, tested); only consecutive, aligned bars form returns, and
   fewer than a week of aligned returns gives no estimate. Refreshed hourly by the orchestrator's ops
   monitor. The orchestrator gets a read-only mount of `data/analytics_lake` for this.
4. **Missing data → β = 1.0.** For BTC itself β is 1 by definition. For ETH, the only other instrument the
   mandates trade, 1.0 counts the position at its full notional; ETH's measured beta to BTC is often a
   little above 1 (≈1.0–1.3), so 1.0 is close to, not strictly above, its real value; the margin is in the cap.
5. **Cap.** 25% of account equity (`DEFAULT_CROSS_MANDATE_EXPOSURE_PCT`), the same number as the
   single-order clamp: all mandates together carry no more BTC risk than one maximal order. It can only be
   lowered (F7).
6. **Refusal.** `CROSS_MANDATE_EXPOSURE: …` with the numbers. It is **not** a `MandateReject` and not a
   BREACH: no mandate overstepped its own limits, so nobody is demoted.

### F7: tighten-only overrides

1. **Storage.** `risk_limit_overrides` (migration 035), one row per (environment, account):
   `max_daily_loss_pct`, `max_order_equity_pct`, `cross_mandate_exposure_pct` (percent, NULL = default),
   `updated_by`, `updated_at`, `reason`. CHECKs repeat the ceilings; the API role writes, the executor
   role may read, the agents' read-only role has nothing.
2. **Rules** (`risk_limits.validate_change`, pure, tested): no value above its code default (5% daily
   loss, 25% order clamp, 25% F4 cap); the order cap not below the 1% minimum order; tightening needs no
   TOTP; loosening (a higher value, or clearing an override back to the default) needs the operator's TOTP
   (`require_totp`, lockout included). The 20% account backstop and the 1% minimum are not configurable.
3. **API.** `POST /api/v1/control/risk-limits` (operator, CSRF; replaces the R1.8 501) takes the full override
   set and a reason, answers 422 with one error per field, 401 when TOTP is missing or wrong, and writes an
   audit row (`RISK_LIMITS_UPDATE` / `RISK_LIMITS_UPDATE_FAILED_AUTH`). `GET` returns defaults, overrides and
   effective values.
4. **Orchestrator.** `ops_monitor` re-reads the row at start-up and every 60 s (`risk_overrides`) and sets
   the dispatcher's `max_daily_loss_pct` and `max_position_concentration_pct` to `min(baseline, override)`,
   where the baseline is the dispatcher's own value capped at the code default. Removing an override
   restores the baseline, never more. A failed read keeps what is applied. Nothing else on the dispatcher
   is touched; its check logic is unchanged. `risk:effective_limits` then publishes the applied values plus
   `cross_mandate_exposure_pct`. `server.py` is not modified.
5. **UI.** The Enforced limits card shows current, default and new value per limit; tightening saves
   directly, loosening asks for TOTP, and the server's errors are shown.

## Deploy (operator, after the L2 review)

`alembic upgrade head` (adds 035); rebuild/restart `api` and `orchestrator` (the compose file adds the
lake mount; a restart cancels resting paper orders, so do it when none are working); web from the new
tree. Until then nothing changes: without the new orchestrator no override is applied and no F4 refusal happens.
