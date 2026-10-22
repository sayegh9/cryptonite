# ADR 0011: Auto-Trade Profiles: user-chosen execution mode, exclusive coins, spot margin

- Status: Accepted (2026-10-22, operator decisions recorded in `reports/autotrade-profiles-plan.md`)
- Amends:
  - [ADR 0004](0004-mandates-and-autonomy.md): the earned autonomy ladder becomes advisory.
  - [ADR 0009](0009-leveraged-perpetuals.md) D3: perps were L1 only.
  - [ADR 0010](0010-mandate-sessions-and-entry-filters.md) P-10: `debate.perp_routing` is retired in P1.
- Contract: `packages/schemas/cryptonite_schemas/profile.py` (spec: `docs/specs/profile-contract.md`)

## Context

Cryptonite's mandates were designed as risk containers for the AI: what it may trade, how much, and at which autonomy level. A level was earned through a promotion gate (ADR 0004) and perps were pinned to L1 (ADR 0009 D3).

The operator wants an AI-first auto-trade product instead:
- the user configures a profile (markets, style, direction, leverage, risk, exits) and presses Start;
- Cryptonite scans by those settings and executes trades that meet them, in the mode the user chose;
- several profiles run at once;
- the Brain can configure and explain them.

## Decisions

| ID | Decision |
|---|---|
| D1 | **A profile is an extended mandate** (`ai_mandates`). Every existing mandate guardrail keeps applying. "Profile" is the product name; the code keeps `mandate` names where they exist |
| D2 | **Execution mode is the user's choice, in PAPER and LIVE.** `ASK` (every entry approved with TOTP), `VETO` (veto window; proceeds only inside active hours), `AUTO` (executes). Stored as `autonomy_level` 1/2/3. Raising the mode is a risk-adding change (TOTP); lowering it is free |
| D3 | **The promotion gate is advisory.** The gate report (days, trades, Sortino vs BTC, breaches, calibration) is shown as a health score and never blocks a mode. **Automatic level demotions are removed.** A breach is still recorded and alerted |
| D4 | **Perps and spot margin are allowed at any mode.** `chk_ai_mandates_perps_level` is dropped |
| D5 | **Run state uses `status`:** ACTIVE = running, PAUSED, STOPPING (no new entries, held until flat), STOPPED, HALTED (system halt: hard stop or backstop; resume needs TOTP), ARCHIVED |
| D6 | **Exclusive coins.** A base asset (BTC, ETH, …) belongs to at most one profile in ACTIVE, PAUSED or STOPPING. This is enforced by the primary key of `ai_profile_coin_locks`: Start inserts the locks in the same transaction that sets ACTIVE, and STOPPED/ARCHIVED delete them. AI entries on a locked coin from any other source (Brain cards, inbox debates) are refused `COIN_LOCKED`. Manual trades are unaffected |
| D7 | **Spot margin is isolated per pair**, modelled on Binance isolated margin. Instruments `<SYMBOL>-MARGIN.BINANCE` are priced and freshness-checked at the spot instrument. Perps stay USD-M, ISOLATED |
| D8 | **Market selection is deterministic** (`select_market`): LONG at 1x → SPOT; LONG above 1x → PERP, else SPOT_MARGIN; SHORT → PERP, else SPOT_MARGIN. No eligible market → no trade |
| D9 | **A running profile requires a daily loss limit.** Migration 050 backfills `3 × max_risk_per_trade_pct` (capped at `max_drawdown_pct`) on every mandate without one (operator decision O0) |
| D10 | **Master switch:** system setting `autotrade.enabled`, default off. While off, Start is refused and no profile runs in VETO or AUTO, so code can ship dark |
| D11 | **Capital allocation:** the sum of the budgets of ACTIVE, PAUSED and STOPPING profiles is at most `autotrade.max_allocated_pct` (default 90) of account equity, checked at Start and on budget raises. Each profile's cash is reserved |
| D12 | **Single-operator tool** (O6). Lifecycle actions and risk-adding edits require a human operator session, never a machine token |
| D13 | **Exits never stop.** PAUSED, STOPPING, HALTED, dry-run and breaker-paused profiles keep their stops, ratchet, take-profit, liquidation management and exit-monitor coverage. Only new entries stop |

## Invariants (hold in every mode and environment)

- Every order passes `GuardedOrderDispatcher`. The mandate check runs before it and can only reject.
- The 1–25% equity clamp (notional for leveraged positions); the 20% account backstop; the kill switch.
- Every entry has a structural stop on the correct side, resting where the venue supports it.
- ISOLATED margin only.
- Leverage is the lowest of the deterministic ceilings (risk at stop, liquidation buffer, exposure, profile maximum, setup grade, exchange bracket). The model never sets it above them.
- Each profile's daily loss limit and hard stop; exits pass every latch.

## Consequences

- ADR 0004 decision 9 (promotion only from a system proposal) and its automatic demotions no longer apply; the gate code stays as the health score.
- ADR 0009 D3 no longer applies. D1, D2, D4–D6 stand.
- The global scanner presets keep running. Their candidates route to the coin-lock owner, which applies its own direction and style.
- Live trading (P5) still needs the live executor, wallet transfers, a dead-man switch and the operator's hardening of the workstation (UPS, wired link, 4G failover) before any LIVE profile starts.
