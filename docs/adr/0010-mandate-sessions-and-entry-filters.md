# ADR 0010: Per-mandate daily sessions and entry filters (auto-trade Phase A)

- Status: Accepted (2026-10-21, operator decisions below). Built on `feat/auto-trade-phase-a`, awaiting local
  verification before merge (CI does not run).
- Context: [ADR 0004](0004-mandates-and-autonomy.md) (mandates, the autonomy ladder, approvals),
  [ADR 0009](0009-leveraged-perpetuals.md) (leveraged perps at L1). Progress and what was not verified:
  [auto-trade-progress.md](../auto-trade-progress.md).
- Amends: nothing. ADR 0004's autonomy rules and ADR 0009's perp rules are unchanged.

## Context

An external proposal added an `auto_trade_mode` switch that let AI orders bypass the operator's approval. It was
rejected: autonomy is earned per mandate through the promotion gate (ADR 0004), and perps stay at L1 where every
risk-adding order is approved with TOTP (ADR 0009 D3). The operator approved a corrected three-phase plan instead.
Phase A, this ADR, adds per-mandate **daily session guardrails** and **refuse-only entry filters**. Everything runs
at the mandate's current level; nothing here adds an approval bypass, changes a level, or resizes an order.

## Decisions

| ID | Decision | Notes |
|---|---|---|
| S-1 | Five mandate fields (migration 049): `min_conviction` (0.50–0.95), `min_rr` (≥ 1.00), `daily_loss_limit_pct` (> 0, ≤ the mandate's hard stop, and ≥ its risk per trade), `daily_profit_target_pct` (> 0, ≤ 100), `profit_lock_pct` (0–100, default 50) | NULL is off. The first four are NULL by default |
| S-2 | A mandate with perps enabled gets `daily_loss_limit_pct` = 3 × `max_risk_per_trade_pct` (capped at its hard stop) when none is given | Applies on create, on an edit that turns perps on, and (migration 049) to a mandate that already has perps on |
| S-3 | The session is the UTC day. Session P&L = the mandate's equity now − its equity at 00:00 UTC (the last `ai_mandate_equity` snapshot before midnight, else the equity at the first tick of the day). Limits are % of the mandate's budget | A budget edit during the day is not P&L: the row keeps the budget it opened with (`start_budget`) |
| S-4 | Daily loss: `pnl ≤ −budget × limit` latches the day: pending ADD_RISK approvals cancelled, the mandate flattened reduce-only (`CLOSE_POSITION`, capped at its own quantity; a perp close is an opposing reduce-only fill), one Critical alert (`MANDATE_DAILY_LOSS`). New entries refused until 00:00 UTC | Operator decision |
| S-5 | Profit target: `pnl ≥ budget × target` refuses new entries for the day, keeps the positions, cancels pending ADD_RISK approvals, sends an Info alert (`MANDATE_DAILY_TARGET`) and adds the mandate's instance to the Redis set `mandate:session:target_met` (expires at the next 00:00 UTC) | The set is the contract with A4 (the perp resting-stop ratchet), which tightens stops for those instances; Phase A does not tighten stops |
| S-6 | Profit lock: once the target was hit, `pnl ≤ target_usd × profit_lock_pct / 100` flattens the mandate, Critical alert (`MANDATE_PROFIT_LOCK`) | Default 50%: give back at most half the target |
| S-7 | The verdict is derived, never stored: `session_verdict` (pure, `cryptonite_schemas.session`) from today's `ai_mandate_daily_sessions` row and the current limits. Precedence LOSS_LATCHED > LOCK_FIRED > TARGET_MET > ACTIVE. A recorded latch time is sticky for the day: neither the P&L nor an edit of the limits clears it; the next UTC day starts a new row | No `session_state` column anywhere |
| S-8 | The orchestrator's mandate gate refuses ADD_RISK with `MANDATE_DAILY_LOSS_LATCHED` or `MANDATE_DAILY_TARGET_MET` (also for LOCK_FIRED) while the verdict is not ACTIVE, **including an approved command executed later**. REDUCE_RISK always passes. Neither code is in `BREACH_CODES` | The check still only rejects and never edits the intent; `GuardedOrderDispatcher` is untouched |
| S-9 | Entry filters are refuse only. Conviction below `min_conviction` and R:R = (target − entry) / (entry − stop) below `min_rr` refuse the entry. The stop must sit on the loss side of the entry and the target on the profit side for the order's side (long: stop < entry < target; short: target < entry < stop), else "stop/target on the wrong side". **Brain cards** must carry what a filter the mandate sets measures: with `min_rr` set an entry card without both a stop and a target is refused ("add a stop and a target"), with `min_conviction` set one without a confidence is refused. **Debates** carry a conviction but no stop or target today: `min_conviction` always applies, `min_rr` is skipped, logged at INFO and recorded as "min_rr skipped: no stop/target" in the approval payload (`filters_skipped`) and summary. A missing entry price (no limit price, mark or last price) still skips R:R, logged. An exit is never refused by any filter or by the session, including a perp reduce card that closes a short (side BUY) | The debate worker ends the run `NO_TRADE` (no new status strings); the Brain card's dry run and approve, and the Inbox debate approval, refuse with the same text |
| S-10 | Setting or tightening a filter or limit is free. Lowering or removing a minimum, raising or removing a daily limit, and lowering the profit lock are risk-adding edits and need TOTP (`risk_adding_change`) | As every other loosening (ADR 0004 §7.1) |

## Difference from the account daily-loss latch

`GuardedOrderDispatcher` (`cryptonite_execution/risk.py`, `check_daily_drawdown`) already has an **account-wide**
daily latch: it measures the account's intraday drawdown **from the day's equity peak** (the high-water mark re-based
at the UTC day roll) and latches at 5% of account equity (or a macro-ATR override), refusing new entries for every
strategy, manual or AI, until the next UTC day. It does not flatten.

The mandate session is separate and narrower: it is per mandate, measured **from the mandate's equity at 00:00 UTC**
(not from the day's peak), as a % of the **mandate's budget**, set by the operator per mandate, and it flattens that
mandate. Both apply; whichever trips first refuses the entry.

## Consequences

- A latch never changes a mandate's status (ACTIVE / PAUSED / HALTED) or its autonomy level, records no `BREACH`,
  and does not touch the hard stop, the half-stop demotion or the account backstop. A HALTED or PAUSED mandate with a
  limit is still tracked, so its session reads correctly in the UI.
- Each transition is recorded once (`UPDATE … SET <latch> = now WHERE <latch> IS NULL RETURNING`) and only the tick
  that recorded it alerts and cancels approvals. While LOSS_LATCHED or LOCK_FIRED, each tick asks the existing
  flatten path again, which queues nothing while a close is pending or just completed (the hard stop's behaviour).
- The alerts go to the notifier outbox and the alerts channel; their kinds are in the notifier's `OUTBOX_KINDS`, so
  Telegram gets each once.
- Setting a daily loss limit in the middle of a losing day measures from that day's 00:00 UTC equity, so it can latch
  and flatten at once. That is what the limit means; the editor says the limit is measured from 00:00 UTC.
- The Brain sees each mandate's session verdict in its System state (`agent_safe_mandate_health` gains the limits and
  today's session, migration 049) and is told not to propose entries for a mandate whose session is not ACTIVE.
- Known limits: start equity is the last `ai_mandate_equity` snapshot before 00:00 UTC taken since the mandate's
  equity was last re-based (`hwm_rebased_at`: a budget edit or a resume); an older snapshot is valued at the old budget
  and is not used, so a budget edit never reads as P&L. With no such snapshot (the edit was today, or the mandate is
  new) the day starts from the equity at the first tick that opens the session row. A budget edit between that first
  tick's snapshot and the tick itself is still absorbed by `start_budget`. If the monitor is down, no session is
  tracked, so a limit is not enforced during the outage and the row catches up on the first tick after it (the gate
  still reads whatever latch was recorded). The R:R filter needs a stop and a target: Brain cards must carry them when
  the mandate sets `min_rr` (S-9); debates routed by the worker carry neither today, so there it is skipped and
  recorded.
