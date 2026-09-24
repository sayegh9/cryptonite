# Perpetual Futures: Phase 1 (Paper) Blueprint

- Status: APPROVED 2026-09-24 (decisions D1–D5 in §0.3). C0–C1 landed on `feat/perpetuals-phase1`; see HANDOFF for progress.
- Scope: USD-M linear perpetuals on Binance, one-way position mode, paper execution only. Live trading (Phase 2) is out of scope here.
- Written against HEAD `1100fa2` plus the uncommitted working tree.

---

## 0. Findings from the review and open decisions

### 0.1 How the code differs from the brief

| # | Brief assumes | Code today | Consequence |
|---|---|---|---|
| F1 | `packages/execution/paper_engine.py` | No such file. Paper execution is split between `PaperTradingEngine` (`packages/execution/cryptonite_execution/engine.py`) and the command loop in `services/orchestrator/cryptonite_orchestrator/server.py:270-440`, which fills MARKET and LIMIT orders immediately at the cached bid/ask. | Perp fill, funding, and liquidation logic belongs in a new module under `packages/execution`. The orchestrator only wires it up. |
| F2 | The dispatcher can hold shorts | `GuardedOrderDispatcher.positions` is `dict[str, Decimal]` with long-only qty. `handle_fill` clamps SELL fills to `max(0, …)` (`risk.py:283`), which silently drops any short. | Perp positions need their own signed-qty state. The spot path stays untouched. |
| F3 | The DB can store shorts | `paper_positions` has `CHECK side IN ('LONG','FLAT')` and `CHECK quantity >= 0` (`005_paper_trading_schema.py:175-176`). | A migration is required. |
| F4 | There is a 1–25% equity clamp | Only the upper bound exists (`max_position_concentration_pct=0.25`, `risk.py:57`), applied to post-trade position value ÷ equity. No 1% floor is enforced anywhere. | Decide whether the 1% floor is real (D2). |
| F5 | Mark price is available | The dispatcher reads only `QuoteTick` bid/ask. Liquidation must trigger on **mark** price, not last or bid. | A `MarkPriceCache` goes next to the node cache. |
| F6 | Funding is every 8h | Binance runs some symbols on 4h or 1h intervals. The `markPrice` stream carries `T` (next funding time). | Never hard-code 8h. Settle at `T` and show the countdown to `T`. |
| F7 | OI arrives with the WS feeds | Binance has no OI WebSocket stream. OI comes from REST only: `/fapi/v1/openInterest` (spot value) and `/futures/data/openInterestHist` (5m+, 30-day lookback). | Workstream 1 adds a REST poller. |
| F8 | A derivatives ingest exists | `services/ingest/cryptonite_ingest/worker.py` is **untracked**, hard-codes BTC and ETH, stamps `time.time_ns()` (wall clock) instead of event time `E`, drops `T` and the index price, and writes one Parquet file per symbol per minute (a small-file problem). | Replace it rather than extend it. |
| F9 | — | Migrations `017`–`019` were untracked, and 014/015 failed on a fresh DB. | Resolved in C0 (`dfc66c0`, `c63576e`). |

### 0.2 Latent spot-era assumptions that break once shorts exist

These must change in the same commit that enables perp shorts. Each one is a real-money bug in Phase 2.

1. `engine.py` `RESET_SWITCH` counts open positions with `q > 0`. **The kill switch would reset while short positions are still open.**
2. `engine.py` and `server.py` `CLOSE_POSITION` flatten only when `pos_qty > 0`, so **shorts are never flattened.**
3. `engine.py` `PAUSE` cancels BUY orders as "entry orders". For a short, SELL is the entry side.
4. `evaluate_intent` applies the conviction multiplier (up to 1.5×) **before** any check. On a reduce-only order this would oversize the close. The multiplier must skip reduce-only and kill-switch exits.
5. The equity loop in `evaluate_intent` values only `qty > 0` at the bid. Perp equity is wallet + Σ uPnL at mark.

### 0.3 Decisions (approved 2026-09-24)

| ID | Question | Decision |
|---|---|---|
| **D1** | Which quantity does the 25% clamp bound for perps? | **Notional at mark, not initial margin.** If the clamp bounds IM, 20× leverage × 25% IM = **500% of equity** in one instrument, and the invariant no longer means anything. With a notional clamp, leverage frees collateral and enables shorts but cannot raise exposure. The brief asks to adapt the clamp to IM; I am pushing back on that. |
| D2 | Enforce the 1% floor? | Yes, as a reject below 1% notional ÷ equity (`POSITION_BELOW_MIN_ALLOCATION`), only for non-reduce-only orders. Otherwise the documented invariant stays unenforced. |
| D3 | Liquidation price formula | Use Binance's exact isolated formula with bracket `cum` (§2.2). The brief's formula is a first-order approximation. For shorts it sits **above** the true liquidation price, so the engine would liquidate late. Keep the approximation only as a label check in tests. |
| D4 | Multi-asset collateral (BTC 0.9 / ETH 0.8 haircuts) | Defer to the last Phase 1 slice (C17). The paper account is single-currency today (`paper_accounts.currency`), and multi-asset collateral touches the whole cross-margin path. |
| D5 | Position flips (long 1 → sell 3 → short 2) | Allow, but only on non-reduce-only orders, and only as close-then-open inside one fill (§2.4). Each leg is risk-checked. |

---

## 1. Data models

The models are implemented in `packages/execution/cryptonite_execution/derivatives.py` (C1). The sketch below is the target shape: C1 shipped `MarginMode`, `OrderSide`, `PositionSide`, `PerpetualPosition`, and `FundingRateRecord`, and the rest land with the commits that use them. Money is `Decimal` everywhere and crosses JSON as a string.

```python
class MarginMode(StrEnum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"

class PositionSide(StrEnum):          # wire/display only; engine uses signed qty
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"

class LeverageBracket(BaseModel):     # from /fapi/v1/leverageBracket
    bracket: int
    notional_floor: Decimal
    notional_cap: Decimal
    max_leverage: int
    mmr: Decimal                      # maintMarginRatio
    cum: Decimal                      # maintenance amount (fast-calc offset)

class MarkPriceTick(BaseModel):       # !markPrice@arr@1s element
    instrument_id: str                # BTCUSDT-PERP.BINANCE
    ts_event_ns: int                  # from field E, not wall clock
    mark_price: Decimal               # p
    index_price: Decimal              # i
    est_settle_price: Decimal         # P
    funding_rate: Decimal             # r  (predicted/current)
    next_funding_time_ns: int         # T

class FundingRateRecord(BaseModel):   # one settled funding event
    instrument_id: str
    funding_time_ns: int              # settlement timestamp (== prior T)
    funding_rate: Decimal
    mark_price: Decimal               # mark at settlement
    source: Literal["BINANCE_REST", "STREAM_SETTLEMENT"]

class PerpetualPosition(BaseModel):
    position_id: UUID
    account_id: str
    instance_id: str
    instrument_id: str
    margin_mode: MarginMode
    leverage: int                     # 1..min(20, bracket.max_leverage)
    signed_qty: Decimal               # >0 long, <0 short, 0 flat
    entry_price: Decimal              # volume-weighted avg of opening fills
    isolated_margin: Decimal          # ISOLATED only; 0 for CROSS
    realized_pnl: Decimal
    cum_funding: Decimal              # Σ funding cash flows (signed)
    # derived at snapshot time, never persisted as source of truth:
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    liquidation_price: Decimal | None = None
    margin_ratio: Decimal | None = None

    @property
    def side(self) -> PositionSide: ...

class FundingSettlement(BaseModel):   # per-position ledger row
    account_id: str
    instrument_id: str
    funding_time_ns: int
    signed_qty: Decimal
    mark_price: Decimal
    funding_rate: Decimal
    cash_flow: Decimal                # = -signed_qty * mark * rate

class LiquidationEvent(BaseModel):
    account_id: str
    instrument_id: str
    margin_mode: MarginMode
    signed_qty: Decimal
    trigger_mark_price: Decimal
    bankruptcy_price: Decimal
    liquidation_price: Decimal
    realized_loss: Decimal
    ts_event_ns: int
```

`OrderIntent` (`risk.py`) gets three additive, defaulted fields, so existing callers and tests are unchanged:

```python
reduce_only: bool = False
leverage: int | None = None          # None ⇒ use account's per-symbol setting
margin_mode: MarginMode | None = None
```

Leverage and margin mode are **per-symbol account settings**, as on Binance, not per-order fields. The ticket fields set them via a `SET_LEVERAGE` / `SET_MARGIN_MODE` command. Margin mode cannot change while a position or open order exists on that symbol (reject `MARGIN_MODE_LOCKED`).

### 1.1 Persistence (migration `020_perpetuals_schema.py` + follow-ups)

C1 landed a subset: `paper_positions` side/leverage/margin columns plus `liquidation_price` / `maintenance_margin` snapshots, and a market-level `funding_rates` table. `reduce_only`, `paper_perp_settings`, the per-position funding ledger, and `paper_liquidations` follow in later migrations (C9–C11).

- `paper_positions`: add `margin_mode`, `leverage`, `isolated_margin`, `cum_funding`. Replace `chk_paper_position_side` with `side IN ('LONG','FLAT') OR (instrument_id LIKE '%-PERP.%' AND side = 'SHORT')`, so spot rows keep the old constraint. `quantity` stays `>= 0` (absolute), and `side` carries direction.
- `paper_orders`: add `reduce_only BOOLEAN NOT NULL DEFAULT false`.
- New `paper_perp_settings (account_id, instrument_id, leverage, margin_mode, PK(account_id, instrument_id))`.
- New `paper_funding_ledger`, shaped like `FundingSettlement`, with `UNIQUE(account_id, instrument_id, funding_time_ns)`. **That unique key is the idempotency guard: a replayed settlement is a no-op.**
- New `paper_liquidations`, shaped like `LiquidationEvent`.
- Extend `013_least_privilege_grants` roles and `014` anonymized views for the new tables in the same migration.

### 1.2 Lake and DuckDB schemas (`data/analytics_lake/derivatives/binance/perpetual/`)

Hive-partitioned as `…/<dataset>/symbol=<SYM>/date=<YYYY-MM-DD>/part-*.parquet`, rolled hourly (not per minute):

| Dataset | Columns | Source |
|---|---|---|
| `mark_price_1s` | `ts_event_ns i64, ts_init_ns i64, symbol, mark_price f64, index_price f64, est_settle_price f64, funding_rate f64, next_funding_time_ns i64` | WS `!markPrice@arr@1s` |
| `funding_rate` | `funding_time_ns i64, symbol, funding_rate f64, mark_price f64` | REST `/fapi/v1/fundingRate` backfill plus live settlement detection (a change in `T`) |
| `open_interest_5m` | `ts_ns i64, symbol, sum_open_interest f64, sum_open_interest_value f64` | REST `/futures/data/openInterestHist?period=5m` plus a 60s poll of `/fapi/v1/openInterest` |
| `depth20_100ms` (optional) | `ts_event_ns, symbol, bids list<struct<p,q>>, asks …` | WS `<sym>@depth20@100ms` |

Float64 is acceptable in the lake, which is analytics only. The engine never reads prices from the lake; it takes them from `MarkPriceTick` as `Decimal(str(...))`. DuckDB views and query helpers go in `packages/storage/cryptonite_storage/analytics_catalog.py`: `mark_price_asof(symbol, ts)`, `funding_history(symbol, start, end)`, `open_interest(symbol, start, end, period)`.

---

## 2. Formulas and `GuardedOrderDispatcher` invariants

All math lives in a pure, I/O-free module, `packages/execution/cryptonite_execution/margin.py`. The dispatcher, liquidation monitor, orchestrator, and golden-vector tests all call it. Notation: `Q = |signed_qty|`, `s = sign(signed_qty)` (+1 long, −1 short), `EP` = entry price, `M` = mark price, `L` = leverage, `(MMR, cum)` = the bracket selected by position notional `Q·M`.

### 2.1 Core quantities

```
Notional         N   = Q · M
Initial margin   IM  = Q · EP / L                     (+ any added isolated margin)
Maint. margin    MM  = N · MMR − cum
Unrealized PnL   uPnL = s · Q · (M − EP)
Margin balance   MB  = ISOLATED: isolated_margin + uPnL
                       CROSS:    wallet + Σ uPnL(cross positions) [× haircuts, C17]
Margin ratio     MR  = MM / MB          (liquidate when MR ≥ 1)
```

### 2.2 Liquidation and bankruptcy price (isolated, one-way)

Binance's exact isolated formula, with `WB = isolated_margin`:

```
P_liq,long  = (Q·EP − WB − cum) / (Q · (1 − MMR))
P_liq,short = (Q·EP + WB + cum) / (Q · (1 + MMR))

P_bk,long   = EP − WB/Q            (price at which isolated margin is exhausted)
P_bk,short  = EP + WB/Q
```

With `WB = Q·EP/L` and `cum = 0`, this reduces to `EP(1 − 1/L)/(1 − MMR)` for longs and `EP(1 + 1/L)/(1 + MMR)` for shorts. Worked example at EP = 60,000, L = 10, MMR = 0.4%:

| | Exact | Brief's approximation | Δ |
|---|---|---|---|
| Long | 54,216.87 | 54,240.00 | approx is **higher**, so it triggers early (conservative) |
| Short | 65,737.05 | 65,760.00 | approx is **higher**, so it triggers **late** (unsafe) |

Cross mode has no closed-form per-position liquidation price. The UI shows an **estimated** liquidation price, solved for this position with all other positions held at current mark (`MB_total − MM_others` substituted for `WB`), and labels it "est.".

### 2.3 Funding

At each settlement time `T` (from the stream, never assumed 8h), for every perp position open at `T`:

```
cash_flow = −signed_qty · M_T · rate_T
```

A positive rate means longs pay and shorts receive. ISOLATED applies the cash flow to `isolated_margin`, so funding can push a position into liquidation. CROSS applies it to wallet. Each settlement is written through `paper_funding_ledger`'s unique key before the in-memory balance changes (§2.5).

### 2.4 Fill accounting (signed qty)

On a fill of signed quantity `f` at price `p` with fee `c`:

- **Same direction or from flat:** `EP' = (|q|·EP + |f|·p) / (|q| + |f|)`, `q' = q + f`, lock `IM += |f|·p/L`.
- **Opposite direction:** `closed = min(|f|, |q|)`, `realized += s·closed·(p − EP)`, release `IM` pro rata. If `|f| > |q|` (a flip, D5), the remainder opens a new position at `EP = p`, and that remainder must have passed the opening checks.
- The fee always debits wallet (CROSS) or isolated margin (ISOLATED).

### 2.5 Dispatcher invariants

The perp branch is selected by `parse_instrument_id(intent.instrument_id)[2]`, the existing `is_perp`. **The spot branch is unchanged**, and `SPOT_INVARIANT_VIOLATION` keeps rejecting spot shorts. The perp branch still sits inside `GuardedOrderDispatcher`, which remains the only order path. It does no DB I/O and holds no async locks.

Checks run in this order. Each has a stable reject code:

| # | Invariant | Reject code |
|---|---|---|
| P0 | Kill switch, daily-loss latch, and freshness gates as today, with **mark** freshness added | existing codes, plus `MARK_PRICE_STALE` |
| P1 | Conviction multiplier is skipped when `reduce_only` or `is_kill_switch_exit` | — |
| P2 | `1 ≤ L ≤ min(20, bracket(N_post).max_leverage)` | `LEVERAGE_EXCEEDS_BRACKET` |
| P3 | **Reduce-only:** position is non-zero, the order side opposes it, and `qty ≤ |q| − Σ resting reduce-only qty`. It never flips or increases exposure. | `REDUCE_ONLY_WOULD_INCREASE`, `REDUCE_ONLY_NO_POSITION` |
| P4 | Price collar, as spot, but anchored on mark | `PRICE_COLLAR_REJECT` |
| P5 | **Exposure clamp (D1):** `0.01 ≤ |N_post| / equity ≤ 0.25`, where `equity = wallet + Σ uPnL at mark`. Skipped for reduce-only. | `CONCENTRATION_LIMIT_BREACHED`, `POSITION_BELOW_MIN_ALLOCATION` |
| P6 | **Margin sufficiency:** `IM_order + fee_est ≤ available_margin`, where `available_margin = MB − Σ IM(open positions) − Σ IM(reserved orders)`. Skipped for reduce-only. | `INSUFFICIENT_MARGIN` |
| P7 | **Pre-trade liquidation buffer:** the projected post-trade liquidation distance `|M − P_liq| / M` must be at least `max(3 × MMR, 1%)`, so no order opens already near liquidation. | `LIQ_DISTANCE_TOO_SMALL` |
| P8 | Reserve `IM_order + fee_est` atomically on dispatch and release it on reject, cancel, or fill, following the existing `reserved_cash` pattern | — |

Post-trade invariants, asserted in tests after every fill, funding tick, and liquidation:

- **I1** ISOLATED: realized loss on a position ≤ its IM plus fees. This holds because liquidation executes at `P_bk`, not beyond it.
- **I2** `Σ reserved ≥ 0`, and every reservation maps to exactly one open order.
- **I3** After a reduce-only fill, `|q'| ≤ |q|` and `sign(q') ∈ {sign(q), 0}`.
- **I4** Ledger conservation: `wallet_t = wallet_0 + Σ realized + Σ funding − Σ fees − Σ liquidation losses`, exact under `Decimal`.
- **I5** A funding settlement applies at most once per `(account, instrument, funding_time)`.

### 2.6 Liquidation monitor

`PerpRiskMonitor` in `packages/execution/cryptonite_execution/perp_monitor.py` runs on each mark tick in the node thread.

- ISOLATED: liquidate when `MR ≥ 1` (equivalently, mark crosses `P_liq`). The fill executes at `P_bk`, `LiquidationEvent` is enqueued on `EventBridge`, and resting orders on the symbol are cancelled.
- CROSS: when account `Σ MM ≥ MB_total`, liquidate the position with the largest MM first, then re-evaluate.
- Liquidations bypass the kill switch and all P-checks, because they are forced reductions. They are still recorded through the same fill path, so I1–I4 hold.
- `margin_alert` events fire when `MR ≥ 0.8` (WARN) and `MR ≥ 0.9` (CRITICAL), with hysteresis so they don't flap.

---

## 3. UI state specification

### 3.1 Transport

The app already has one WS client (`apps/web/src/lib/ws.ts`) and a Redis-backed `StreamHub`. Perp data reuses both; no SSE is added. Today `StreamHub._pubsub_loop` fans `CHANNEL_POSITIONS` out to every client unconditionally. That is fine at 1 Hz, but mark prices for N symbols must honour per-client instrument subscriptions (API commit C11).

Every server-to-client message uses the envelope `{ "type": string, "v": 1, "ts_ns": number, ... }`. Decimals arrive as strings; the client parses them once in the store.

| `type` | Channel to subscribe | Payload | Rate |
|---|---|---|---|
| `perp.mark` | `perp_mark` + instruments | `instrument_id, mark_price, index_price, funding_rate, next_funding_time_ns` | ≤ 1 Hz per symbol |
| `perp.open_interest` | `perp_oi` + instruments | `instrument_id, open_interest, open_interest_value` | 60 s |
| `positions_snapshot` | existing (`CHANNEL_POSITIONS`) | **additive**: each position gains `margin_mode, leverage, signed_qty, isolated_margin, liquidation_price, margin_ratio, cum_funding`, and `account` gains `margin_balance, maint_margin, available_margin` | 1 Hz |
| `perp.funding_settled` | `perp_account` | `FundingSettlement` | per event |
| `perp.liquidation` | `perp_account` | `LiquidationEvent` | per event |
| `perp.margin_alert` | `perp_account` | `instrument_id, margin_ratio, level: "WARN" \| "CRITICAL"` | on transition |

REST additions under `/api/v1`:

- `GET /perp/brackets/{symbol}`: `LeverageBracket[]`, cached server-side with a 1h TTL.
- `GET/PUT /paper/perp/settings/{instrument_id}`: `{leverage, margin_mode}`. Writes go through `execution_commands`.
- The order payload on the existing `MANUAL_ORDER` command gains `reduce_only`.

### 3.2 Stores

Per CLAUDE.md, all stores are persisted Zustand. Live telemetry must never be rehydrated as if it were current, so each store persists only preferences via `partialize`.

**`apps/web/src/lib/stores/usePerpTicketStore.ts`**

```ts
type MarginMode = "ISOLATED" | "CROSS";
interface PerpTicketState {
  // persisted (partialize)
  prefsBySymbol: Record<string, { leverage: number; marginMode: MarginMode }>;
  // ephemeral
  draft: { instrumentId: string; side: "BUY" | "SELL"; orderType: "MARKET" | "LIMIT";
           qty: string; price: string; reduceOnly: boolean };
  serverReject: { code: string; message: string } | null;
  setLeverage(symbol: string, l: number): void;     // also PUT settings
  setMarginMode(symbol: string, m: MarginMode): void; // rejected if position/orders exist
  setDraft(p: Partial<PerpTicketState["draft"]>): void;
}
```

**`apps/web/src/lib/stores/usePerpMarketStore.ts`**

```ts
interface PerpMarketState {
  // persisted: selectedInstrumentId only
  selectedInstrumentId: string;
  // ephemeral, fed by ws
  marks: Record<string, { mark: number; index: number; fundingRate: number;
                          nextFundingTimeNs: number; tsNs: number }>;
  openInterest: Record<string, { oi: number; oiValue: number; tsNs: number }[]>; // ring buffer
  positions: Record<string, PerpPositionView>;
  account: { marginBalance: number; maintMargin: number; availableMargin: number } | null;
  brackets: Record<string, LeverageBracket[]>;
  alerts: MarginAlert[];
  ingest(msg: StreamEnvelope): void;   // single reducer for all perp.* + positions_snapshot
}
```

Derived values (the ticket calculation box, gauge distance, dial) are computed with selectors over `apps/web/src/lib/perp/margin.ts`, never stored. `margin.ts` is a line-for-line TypeScript port of `margin.py` and is checked against the same golden vectors (§4). Client values are advisory; the dispatcher is authoritative, and its reject code is rendered verbatim in the ticket.

Cross-workspace: `window.dispatchEvent(new CustomEvent("cryptonite:perp-ticket-prefill", { detail: { instrumentId, side, qty? } }))` lets chart and scanner workspaces load the ticket. It is typed in `apps/web/src/lib/types.ts` next to the existing `cryptonite:*` events.

### 3.3 Components and behaviour

| Component (`components/terminal/perp/`) | Spec |
|---|---|
| `LeverageSlider.tsx` | Integer 1× to `min(20, bracket(N_draft).max_leverage)`. Recomputes IM and projected liquidation price on every change with no network round-trip, then debounces the `PUT` by 300 ms. Disabled when a position exists and the new leverage would raise IM beyond available margin. |
| `MarginModeSelect.tsx` | Isolated / Cross segmented control. Locked, with a tooltip, while a position or order exists on the symbol. |
| `PerpOrderTicket.tsx` | Reduce-Only checkbox. When checked, qty is capped at `|q| − resting reduce-only qty`, and the ticket shows "no position" and disables submit if flat. The calculation box shows Order Notional, Initial Margin, Fee est., Projected Liq. Price (with "est." in Cross), and Liq. Distance. |
| `LiquidationGauge.tsx` | `d = |M − P_liq| / M`: green above 20%, amber 5–20%, red below 5%. Colour is paired with a text label and icon, so it doesn't rely on colour alone. The red state pulses once per `margin_alert` transition, not continuously. |
| `MarginRatioDial.tsx` | `MR = MM / MB`, from 0 to 100%, with WARN and CRITICAL ticks at 80% and 90%. |
| `FundingCountdown.tsx` | `next_funding_time_ns − now`, rendered as `hh:mm:ss`, plus a badge showing the predicted rate and who pays ("Longs pay 0.0100%"). |
| OI pane (in `TradingViewChart.tsx`) | A Lightweight Charts histogram series on a separate price scale, fed by `openInterest` history plus live 60s points. |
| `CollateralBreakdown.tsx` (C17) | Asset, qty, price, haircut, and collateral value. Cross mode only. |

Every interactive element gets a `data-testid` for the Playwright spec.

---

## 4. Test plan

**`tests/test_perpetual_margin_and_liquidation.py`** is pure unit and deterministic. It uses a fake node cache and a fake mark cache, with no Nautilus node, Postgres, or Redis.

- Golden vectors in `tests/fixtures/perp_margin_vectors.json` cover IM, MM, liquidation, and bankruptcy price across L ∈ {1, 2, 5, 10, 20}, both sides, and bracket tiers 1–3. The Binance-documented example is included.
- Short open → mark up → uPnL negative → close → realized PnL, with the sign checked.
- Reduce-only: rejected when flat; rejected on the same side; rejected when qty exceeds position minus resting reduce-only qty; accepted exactly at the cap; never flips; conviction multiplier not applied.
- D1 clamp: 20× leverage with 25.01% notional is rejected, even when IM is only 1.25%.
- Funding: a positive rate debits longs and credits shorts; a flat position is unaffected; replaying the same `funding_time_ns` is a no-op (I5); an isolated funding debit can trigger liquidation.
- Liquidation: the mark path crosses `P_liq` → exactly one `LiquidationEvent`, fill at `P_bk`, loss ≤ IM plus fees (I1); a mark one tick short of `P_liq` does not liquidate; cross mode liquidates the largest-MM position first.
- Kill-switch regressions from §0.2: `RESET_SWITCH` is rejected with an open short; `CLOSE_POSITION` flattens a short.
- Spot regression: a spot SELL beyond the held quantity still returns `SPOT_INVARIANT_VIOLATION`.
- Property test: random fill, funding, and mark sequences preserve I2–I4.

Baseline: the existing 160 tests stay green, and the new tests add to the count. The suite runs through the Docker `test` service, as recorded in HANDOFF.

**`apps/web/e2e/perp_ticket.spec.ts`** covers:

- Moving the slider from 1× to 20× updates IM and projected liquidation price to the golden-vector values for a fixture draft.
- The slider max drops when draft notional crosses a bracket boundary.
- Reduce-Only disables submit when flat and caps qty when a long exists.
- The margin mode control is locked while a position is open.
- A stubbed WS `perp.mark` sequence moves the gauge through green, amber, and red, and a `perp.margin_alert` CRITICAL shows the alert.
- The funding countdown renders and rolls over when `T` advances.

---

## 5. Atomic commit roadmap

**C0, prerequisite, not perp work:** resolve F9. Commit or park 017–019 and the pending `server.py` and `bridge.py` edits, then branch `feat/perp-paper` from a clean `main`. No perp commit should carry unrelated tree changes.

Each commit keeps `pytest`, `typecheck`, and `lint` green on its own.

| # | Commit | Owner | Depends |
|---|---|---|---|
| C1 | `feat(schemas): add derivatives contracts (MarginMode, PerpetualPosition, FundingRateRecord, MarkPriceTick, LeverageBracket, LiquidationEvent)` + schema tests | software-architect | C0 |
| C2 | `feat(execution): add pure margin math module with golden vectors` (`margin.py`, fixture JSON, unit tests) | backend-architect | C1 |
| C3 | `feat(ingest): record !markPrice@arr@1s with event time and next funding time` (replaces the untracked `worker.py`; hourly Hive-partitioned Parquet) | database-optimizer | C1 |
| C4 | `feat(ingest): backfill funding-rate history and poll open interest` (REST, rate-limit aware) | database-optimizer | C3 |
| C5 | `feat(storage): add DuckDB views for mark price, funding, and open interest` | database-optimizer | C3, C4 |
| C6 | `feat(db): add perp paper schema (migration 020)` + grants and anonymized views | backend-architect | C0, C1 |
| C7 | `fix(execution): make kill-switch reset, flatten, and pause direction-aware` (§0.2 items 1–3; tests first; spot behaviour unchanged) | backend-architect | C0 |
| C8 | `feat(execution): add perp branch to GuardedOrderDispatcher` (P0–P8, reduce-only, D1 clamp, conviction skip) | backend-architect | C2, C7 |
| C9 | `feat(execution): add signed-qty perp fill accounting with flips` (§2.4; I2–I4 property test) | backend-architect | C8 |
| C10 | `feat(execution): add mark-driven liquidation and funding settlement monitor` (I1, I5; events via `EventBridge`) | backend-architect | C9 |
| C11 | `feat(orchestrator,api): wire mark cache, perp settings commands, brackets endpoint, and filtered perp stream channels` (persistence to 020; additive `positions_snapshot`) | backend-architect | C3, C6, C10 |
| C12 | `test(execution): add test_perpetual_margin_and_liquidation suite` (consolidates C2–C10 coverage per §4) | test-automation | C10 |
| C13 | `feat(web): add perp margin math, perp stores, and stream reducer` | frontend-developer | C11 |
| C14 | `feat(web): add derivatives order ticket (leverage, margin mode, reduce-only, calc box)` | frontend + ui-designer | C13 |
| C15 | `feat(web): add margin HUD (liquidation gauge, margin ratio dial, alerts)` | frontend + ui-designer | C13 |
| C16 | `feat(web): add funding countdown and open-interest chart pane` | frontend-developer | C5, C13 |
| C17 | `feat(execution,web): add multi-asset cross collateral with haircuts` (D4, deferrable) | backend + frontend | C10, C15 |
| C18 | `test(e2e): add perp ticket and margin HUD Playwright spec` | test-automation | C14–C16 |
| C19 | `docs: add liquidation runbook; update HANDOFF, AUDIT_STATUS, and USER_MANUAL` + `graphify update .` | code-reviewer | all |

Code-reviewer gate on every commit touching `risk.py`, `margin.py`, `perp_monitor.py`, or migrations:

- The spot branch diff is empty, or the change is justified.
- Every new reject path has a test.
- No float appears in the ledger path.
- No DB I/O happens in the node thread.

### Phase 1 exit criteria ("production-ready paper")

1. All §2.5 invariants are covered by tests. The property test is green over 10k seeded sequences.
2. A 72h soak on live Binance mark data runs with ≥ 1 funding settlement per symbol, no duplicate ledger rows, and I4 reconciling to the cent against Postgres.
3. Playwright is green, including the pre-existing specs (HANDOFF next step 2).
4. The flaky `test_full_stack_backtest_e2e` is diagnosed (HANDOFF next step 1), so the gate is trustworthy.
