# Leverage trading plan (2026-10-20)

- **Status:** approved by the operator on 2026-10-20 with decisions D1–D6 as recommended (§3,
  [ADR 0009](adr/0009-leveraged-perpetuals.md)). Stages 1–4 are built on branch `feat/leverage-perps`
  (migration `048`), **awaiting local verification** by the operator (full Postgres/Redis pytest and
  Playwright) before merge. Per-item progress, tests and what was and was not verified:
  [leverage-progress.md](leverage-progress.md). Stage 0 (replay study), Stage 5 (time-gated paper run) and
  Stage 6 (testnet executor, apart from the Stage 1 adapter methods) are not started.

**Goal.** Cryptonite can open leveraged USD-M perpetual positions (long or short). The leverage is chosen by the operator, or proposed by the Brain from how strong the setup is, and every order is held inside limits that the system computes and the operator controls.

**Order of rollout.** Paper first, then the Binance futures testnet (DEMO), then live. Live money is a separate go/no-go decision at the end, not part of this plan's delivery.

## 1. Starting point (checked against the code on 2026-10-20, main `3bc1047`)

| Area | What exists | What is missing |
|---|---|---|
| Margin math | `margin.py`: initial and maintenance margin, Binance liquidation price, funding, margin ratio. Mirrored in `apps/web/src/lib/perp/margin.ts` | Bracket tables only for BTCUSDT and ETHUSDT (`REFERENCE_BRACKETS`); every other symbol raises. `get_leverage_brackets` exists in the adapter but nothing calls it |
| Paper engine | Perp fills with signed quantity and flips, reduce-only, ISOLATED/CROSS, liquidation every 1 s, funding settlement, insurance-fund shortfall (`risk.py`, `engine.py`, `bridge.py`). Certified 2026-09-24 with a 10x ISOLATED BTC short | No resting stop order: stops exist only as exit-monitor checks. Default margin mode is CROSS (`risk.py:478`). Paper taker fee is the spot 0.10%, not the USD-M 0.05% |
| Manual (operator) leverage | `OrderEntryPanel` SPOT/PERP ticket: leverage slider 1–20x, margin mode, reduce-only, preview of notional, initial margin and liquidation price. `MarginRiskHUD` shows liquidation distance | Stop is not required and not attached to the order. 20x maximum is above this plan's 10x |
| Mandates | `allow_perps`, `max_leverage` (1–5), perps only at L3 (schema validator, DB CHECK in migration 025, ADR 0004/0007) | All 5 active mandates are L1 with `allow_perps=false`, so no AI perp order can pass today |
| Orchestrator sizing | Allocation % of the mandate budget; mandate room sizing for entries | Room sizing runs only for `side == "BUY"` (`services/orchestrator/cryptonite_orchestrator/server.py:1222`), so a perp short entry is not sized to the mandate's room |
| Brain | `get_derivatives_context` (funding, OI, mark for any Binance symbol), `scan_swing_setups` with structural stops and TP1, card dry run | `OrderProposal` (`packages/agents/cryptonite_agents/brain/widgets.py`) has no leverage, no margin mode, `stop_loss` is optional, the prompt says shorting is impossible, and the card's mandate remedy (`services/api/cryptonite_api/routes/brain.py`) refuses perps |
| Exit monitor | Hard, trailing and structural stops, TP1 50% scale-out, REST fallback, blind-time tile, NO_FRESH_QUOTE alert | No leverage or liquidation price in `Position`; no liquidation-proximity alert. No partial perp close (a perp reduce closes the whole position) |
| Exchange adapter | `BinanceFuturesAdapter` (`packages/execution/cryptonite_execution/binance_adapter.py`): order submit, query, cancel, open orders, leverage brackets | `submit_order` sends no `reduceOnly`, no `stopPrice`, no `closePosition`, no `workingType`. No `POST /fapi/v1/leverage` or `/fapi/v1/marginType`. No position-mode check or `positionRisk` reconciliation. `_is_futures` is a string guess |
| Data | Perp quotes (`INGEST_PERP_SYMBOLS`) and marks (`PERP_SYMBOLS`) for BTCUSDT and ETHUSDT | No perp quotes or marks for the other onboarded symbols, so the dispatcher cannot price or fill them |
| Backtester | Spot only | No perp support (not needed for Brain cards) |

## 2. How leverage is decided

### 2.1 The model
An order is three choices plus limits.
- **Margin**: how much of the mandate budget is posted (the card's `allocation_pct`, now meaning margin for a perp).
- **Leverage**: chosen by the operator, or proposed by the Brain from the setup grade (§2.3).
- **Stop**: required for every leveraged entry. It is a structural level from the setup, not a fixed percent.

Notional = margin × leverage. The system caps leverage at the lowest of these ceilings, and the card says which one is binding:

| Ceiling | Rule | Why |
|---|---|---|
| Risk at stop | margin × L × (stop distance + slippage allowance + fees) ≤ `max_risk_per_trade_pct` of the mandate budget (default 1%) | Bounds the loss if the stop fills, including measured slippage (an XRP stop filled 0.28% past its level) |
| Liquidation buffer | stop distance + slippage allowance ≤ 50% of the liquidation distance (liquidation price from live brackets via margin.py) | The stop always fires well before liquidation, even with a gap |
| Exposure | notional ≤ the mandate's position limit and ≤ 25% of account equity (D1) | Keeps the existing invariant |
| Mandate maximum | L ≤ `mandate.max_leverage` | Operator's per-mandate choice, raised only with TOTP |
| Setup grade | L ≤ the grade's ceiling (§2.3) | The "potential of the setup" input |
| Exchange | L ≤ the bracket's `max_leverage` for that notional | Binance's own limit |

One pure function computes all of it, `plan_leveraged_order(...)` in a new `packages/execution/cryptonite_execution/leverage.py`. It returns a `LeveragePlan`: quantity, notional, margin, liquidation price and distance, loss at stop in $ and %, funding per 8 h, the allowed maximum leverage, the binding ceiling, or a reject code. The card dry run, the orchestrator sizing and the dispatcher all call this function (the #86 lesson: sizing and checking must value an order the same way). Default slippage allowance 0.3%, an operator setting.

### 2.2 Worked example (use as test cases)
Mandate budget $10,000, position limit 25% ($2,500 notional), max leverage 10, risk per trade 1% ($100). BTC long, margin 2.5% ($250), grade A (ceiling 10x). Slippage allowance 0.3%, fees 0.1% round trip.

| Stop distance | Risk ceiling | Liquidation ceiling | Exposure ceiling | Result |
|---|---|---|---|---|
| 2% | 250 × L × 2.4% ≤ 100 → 16x | 2.3% ≤ ½(1/L − 0.4%) → 20x | 2,500 / 250 → 10x | 10x, notional $2,500, loss at stop ≈ $60 |
| 5% | 250 × L × 5.4% ≤ 100 → 7x | 5.3% ≤ ½(1/L − 0.4%) → 9x | 10x | 7x, notional $1,750, loss at stop ≈ $95 |
| 9% | 4x | 5x | 10x | 4x, even with grade A |
(The liquidation column is the approximation; the implementation uses margin.py's exact liquidation price.)

### 2.3 Setup grade (the "potential")
Computed by the system, not by the model. LLM confidence stays on the card for later scoring but never raises leverage.
New `packages/scanner/cryptonite_scanner/setup_grade.py`, deterministic, inputs already in the stack:
- Timeframe alignment from the `htf_*` columns (4h, 1h, 15m in the trade's direction).
- Reward to TP1 versus risk to the stop (≥ 2.0 for A, ≥ 1.5 for B).
- Market regime (`get_market_regime`): trend in the trade's direction, not range or opposite.
- Volatility: ATR percentile; above the 90th percentile lowers the grade one step.
- Crowding: funding above +0.03% per 8 h on a long (below −0.03% on a short), or open interest falling sharply, lowers the grade one step.
- Liquidity: spread and 24 h futures volume; thin books cap the grade at C.

| Grade | Default leverage ceiling | Meaning |
|---|---|---|
| A | 10x (never above the mandate maximum) | Every input aligned |
| B | 5x | Most aligned, one weak input |
| C | 2x | Tradeable but weak |
| NO_TRADE | no perp card | Fails the minimum |

Ceilings are operator settings shown with their source. Grade and reasons are stored with every card. After 30 graded cards are closed, a report checks A beats B and B beats C; if not, every ceiling falls to the B value (lowering is automatic; raising back needs the operator). The check repeats every further 30 cards.

### 2.4 Who decides
- Operator, manual ticket: chooses leverage; the same `LeveragePlan` preview and ceilings apply (account maximum instead of mandate maximum). Attaching a stop places a resting stop.
- Brain card: the Brain may propose a leverage; if not, the system fills in the highest allowed value for the grade. The operator can lower it freely or raise it up to the allowed maximum; Approve needs TOTP as today.
- Raising limits: `max_leverage`, `allow_perps` and `max_risk_per_trade_pct` on a mandate change only through the mandate routes with TOTP (the #100 card remedy gets "Allow perps" and "Raise max leverage" options).

## 3. Decisions (approved by the operator on 2026-10-20: "Recommended D1–D6")

Recorded in [ADR 0009](adr/0009-leveraged-perpetuals.md).

| ID | Decision |
|---|---|
| D1 = A | The perp notional clamp stays at ≤ 25% of account equity (and ≤ the mandate's position limit) |
| D2 | Schema ceiling 10x (migration 048 changes `chk_ai_mandates_leverage` from ≤ 5 to ≤ 10); new mandates default to 3x; the account cap for the manual ticket and the dispatcher drops from 20x to 10x |
| D3 | Perps allowed at L1 in PAPER (every risk-adding order is approved with TOTP). Leaving the allowed level still withdraws the opt-in |
| D4 | Perp shorts allowed; spot stays long only |
| D5 | ISOLATED only for mandate orders; the manual ticket keeps the choice but defaults to ISOLATED |
| D6 | Risk per trade 1% default, 2% cap, of the mandate budget |

## 4. Stages

### Stage 1 — Safety plumbing
1. Futures adapter: `reduceOnly`, `STOP_MARKET` and `TAKE_PROFIT_MARKET` with `stopPrice`, `closePosition` and `workingType=MARK_PRICE`; `set_leverage`, `set_margin_type`; refuse to trade if the account is in hedge position mode; `positionRisk` read. Route by the `-PERP` instrument suffix, not the string guess.
2. Live brackets: fetch `/fapi/v1/leverageBracket` (signed) with a read-only mainnet key (paper trades on mainnet prices; testnet brackets can differ). Cache daily in Redis with the fetch time; `REFERENCE_BRACKETS` only as fallback for BTC/ETH. No brackets = perp refused (`NO_LEVERAGE_BRACKETS`). Stale brackets refuse new perp entries.
3. Perp quotes and marks for every onboarded symbol that has a USD-M perpetual (same DB-driven list and cap as spot ingest), not only BTC and ETH. A reconnect recovery hook for the futures bookTicker socket.
4. Paper resting stops: a stop order that rests in the paper engine and triggers on mark, filling at the worse of the stop and the mark (gap aware). Works when the exit monitor is blind.
5. Partial perp close: the close path accepts a quantity, so the TP1 50% scale-out works on perps.
6. Mandate perp orders default to ISOLATED. Paper perp taker fee set to the USD-M rate (0.05%).
7. Exit monitor: perp `Position` carries leverage and liquidation price, uses the mark; `LIQUIDATION_PROXIMITY` Critical alert when the stop-to-liquidation buffer is half used; emergency reduce-only close at 75% as a backup to the resting stop.
- Exit: tests for each piece, including a flash-crash test (mark gaps through the stop and towards the liquidation price in one tick: the resting stop fills at the gap price, the liquidation engine does not fire first); a paper round trip where the resting stop fires with the exit monitor stopped.

### Stage 2 — Mandate and sizing rules
1. Migration 048: `ai_mandates` CHECK `max_leverage ≤ 10`; new `max_risk_per_trade_pct` (default 1, CHECK ≤ 2); perps allowed at L1 (D3). Schema: `MAX_PERP_LEVERAGE_CAP = 10`, validator follows D3; new mandates default max_leverage 3 when perps are enabled.
2. `leverage.py` `plan_leveraged_order` (§2.1), with property tests (hypothesis if already a dependency, else parametrised): for any inputs the plan never puts the stop beyond 50% of the liquidation distance and never exceeds the loss cap.
3. Dispatcher perp checks: `STOP_REQUIRED` (AI entries), `RISK_AT_STOP_EXCEEDED`, `LIQUIDATION_BUFFER_TOO_THIN`, `LEVERAGE_ABOVE_BRACKET`. The 25% notional clamp stays. Account maximum leverage 10 (dispatcher `max_perp_leverage` and API `le=20` → 10).
4. Orchestrator: perp entries in either direction are sized by `plan_leveraged_order` and the mandate's room (fix the BUY-only branch at `server.py:1222`). The mandate check gets the same plan.
5. Cross-mandate exposure (ADR 0006) already counts perp notional by |notional| × β; add tests that a perp short and a spot long on the same coin both count, and that 10x positions on BTC and ETH perps together hit the cap.
6. Mandate equity and drawdown include perp funding paid; the daily-loss latch and the 20% hard stop apply unchanged.
7. Settings (ADR 0007 guardrails list): the new ceilings shown with their source; grade ceilings and slippage allowance editable by the operator.
- Exit: full pytest green; dispatcher and orchestrator agree on every sizing in tests.

### Stage 3 — Setup grade
1. `setup_grade.py` (§2.3), deterministic, inputs stored so a grade can be recomputed.
2. Brain tool `grade_setup(symbol, side, entry, stop, target)`; `scan_swing_setups` returns the grade per setup.
3. Grade and LLM confidence stored on each card (`agent_safe_*` view extended, read-only; any view change goes in migration 048 or a 049).
- Exit: the same inputs always give the same grade.

### Stage 4 — Brain and UI
1. `OrderProposal`: perp instruments; BUY/SELL meaning long/short on perps; optional `leverage`; `stop_loss` required for perp entries. Validation refuses a perp card without a stop.
2. Brain prompt: perps only in mandates with `allow_perps`; always a structural stop; state the grade and the binding ceiling; shorting is possible on perps only; never claim a leverage the dry run did not allow.
3. Order card (`apps/web/src/components/brain/BrainWidgets.tsx`): `7x ISOLATED` pill, margin, notional, liquidation price (also on the chart overlay with Entry, Stop, TP1), stop-to-liquidation buffer bar, loss at stop in $ and % of the mandate, funding per 8 h, the grade with its reasons, and "7x (risk at stop)" when a ceiling cut the request. Leverage selector: pills `1x 2x 3x 5x 7x 10x` plus the recommended value when it is not a pill (e.g. `6x ★`). Pills above the allowed maximum are disabled with the binding ceiling as the reason. Margin stays fixed when the operator changes the leverage; notional, loss at stop, liquidation price and buffer are recomputed by the dry run before Approve. Leverage is locked once the order fills. The manual ticket uses the same pills (its 20x chip goes).
4. Card mandate remedy (#100) for perps: "Allow perps on this mandate", "Raise max leverage to N", with TOTP.
5. Manual ticket (`OrderEntryPanel`): `LeveragePlan` preview, 10x account cap, "attach stop" places a resting stop.
6. Mission Control: perp exposure and liquidation distance per position; Telegram/ntfy for `LIQUIDATION_PROXIMITY`.
7. "Flatten perps" in the header while any perp is open: closes every perp with reduce-only market orders and leaves spot alone (TOTP, like the kill switch).
8. Approval step for leveraged cards: the TOTP dialog states the loss at the stop and the loss if liquidated (the posted margin) in dollars; at 8x and above it needs a second confirmation.
9. Leverage colour tokens (low / mid / high, liquidation) defined for both light and dark themes in the existing token set, not as raw hex in components.
- Exit: Playwright specs for each card state; `pnpm typecheck && pnpm lint`; live Brain checks (paid model) listed for the operator in the progress file.

### Out of scope
Perp support in the Nautilus backtester; CROSS margin for AI mandates; multi-asset collateral; hedge position mode; L2/L3 autonomous perps; D1-B (exposure above 25% of equity); the Stage 6 executor flow (only the Stage 1 adapter methods).
