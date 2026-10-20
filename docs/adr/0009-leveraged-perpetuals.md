# ADR 0009: Leveraged perpetuals for mandates and the operator (D1–D6)

- Status: Accepted (2026-10-20, operator decision "Recommended D1–D6"). Built on `feat/leverage-perps`,
  awaiting local verification before merge.
- Context: [leverage trading plan](../leverage-trading-plan.md), [perpetuals architecture](../perpetuals-architecture.md),
  [perpetuals Phase 1 blueprint](../perpetuals-phase1-blueprint.md), [ADR 0004](0004-mandates-and-autonomy.md),
  [ADR 0006](0006-cross-mandate-exposure-and-configurable-limits.md), [ADR 0007](0007-settings-module.md).
- Amends: ADR 0004 O1 ("perps only after L3, capped at 5x") and decision 9 ("leaving L3 withdraws the perps
  opt-in"); ADR 0007 decision 7 (the fixed guardrails list: "5× leverage, perps only at L3").

## Context

The paper perpetuals engine (margin math, signed positions, liquidation, funding) was certified on 2026-09-24,
but no AI mandate can use it: perps need L3, and every mandate is L1. The operator wants to trade leveraged
USD-M perpetuals, long or short, with the leverage chosen by the operator or proposed by the Brain from the
quality of the setup, inside ceilings the system computes. The plan's §2 sets the model: an order is margin ×
leverage with a required structural stop, and leverage is capped by the lowest of six ceilings (risk at stop,
liquidation buffer, exposure, mandate maximum, setup grade, exchange bracket), computed by one pure function
(`plan_leveraged_order`) that the card dry run, the orchestrator sizing and the dispatcher all call.

## Decisions

| ID | Decision | Notes |
|---|---|---|
| D1 = A | Perp notional stays at ≤ 25% of account equity and ≤ the mandate's position limit | Unchanged from the perps blueprint D1: leverage frees collateral, it never raises exposure. D1-B (exposure above 25%) is out of scope |
| D2 | Mandate ceiling 10x (`MAX_PERP_LEVERAGE_CAP = 10`; migration 048 changes `chk_ai_mandates_leverage` from ≤ 5 to ≤ 10). A mandate that enables perps defaults to 3x. The account cap for the manual ticket and the dispatcher drops from 20x to 10x (`max_perp_leverage`, API `le=10`) | Raising a mandate's maximum is a risk-adding edit and needs TOTP |
| D3 | Perps are allowed at **L1** in PAPER, where every risk-adding order is approved by the operator with TOTP. Leaving the allowed level withdraws the opt-in | Replaces "perps only at L3". L2/L3 autonomous perps stay out of scope, so the allowed level is L1 only: a promotion off L1 clears `allow_perps` (see Consequences) |
| D4 | Perp shorts are allowed; spot stays long only | `SPOT_INVARIANT_VIOLATION` is unchanged |
| D5 | Mandate perp orders are ISOLATED only. The manual ticket keeps the ISOLATED/CROSS choice but defaults to ISOLATED | CROSS for AI mandates is out of scope |
| D6 | Risk per trade: `max_risk_per_trade_pct` per mandate, default 1%, cap 2%, of the mandate budget | Raising it needs TOTP |

Further rules that follow from the plan (not operator options):

- Every leveraged entry has a stop. AI perp entries without one are refused (`STOP_REQUIRED`). The stop must sit
  inside 50% of the liquidation distance including the slippage allowance (`LIQUIDATION_BUFFER_TOO_THIN`), and the
  loss if it fills must stay within the risk budget (`RISK_AT_STOP_EXCEEDED`). Leverage above the exchange bracket
  for the notional is refused (`LEVERAGE_ABOVE_BRACKET`); a symbol without brackets is refused
  (`NO_LEVERAGE_BRACKETS`), and so are stale brackets for new entries.
- Brackets come from Binance's signed `/fapi/v1/leverageBracket` with a read-only mainnet key, cached daily in
  Redis. `REFERENCE_BRACKETS` is a fallback for BTCUSDT and ETHUSDT only.
- The setup grade (A/B/C/NO_TRADE) is computed by deterministic code, never by the model. LLM confidence is stored
  for scoring but never raises leverage.

## Consequences

- ADR 0004 O1 is amended: perps are no longer "only after L3"; they are L1-only in PAPER, capped at 10x by the
  schema, and still per-mandate opt-in (`allow_perps`, TOTP to enable). ADR 0004 decision 9 now reads "leaving L1
  withdraws the perps opt-in": any level change (promotion or demotion) away from L1 clears `allow_perps`, and the
  DB CHECK `chk_ai_mandates_perps_level` (migration 048, replacing `chk_ai_mandates_perps_l3`) allows perps only at
  autonomy level 1.
- ADR 0007 decision 7's fixed guardrails become: 25% per-order equity clamp (perps: notional), 20% account
  backstop, 1% minimum order, mandate ceilings (25% position, 20% hard stop, **10× leverage, perps only at L1 with
  TOTP approval, 2% risk per trade**), the stop-to-liquidation buffer (stop ≤ 50% of the liquidation distance),
  and the 10x account leverage cap. The grade ceilings (A 10x, B 5x, C 2x) and the slippage allowance (0.3%) are
  operator settings (lowering free, raising with TOTP).
- Spot order behaviour does not change. Every order still passes `GuardedOrderDispatcher`; the mandate check still
  runs before it for AI orders.
- Downgrading migration 048 is refused while any mandate has `max_leverage > 5` or perps enabled below L3.
- Stage 0 (replay study), Stage 5 (time-gated paper run) and Stage 6 (testnet executor) are separate work. Live
  money is a separate go/no-go decision.
