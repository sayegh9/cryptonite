# Perpetual Futures (Paper) — System Architecture

Status: Phase 1 complete and certified on the live stack (2026-09-24). Scope: Binance USD-M linear
perpetuals, one-way position mode, **paper execution only**. Design history and decisions: see
[perpetuals-phase1-blueprint.md](perpetuals-phase1-blueprint.md). Operating limits: see [HANDOFF.md](../HANDOFF.md).

```
Binance fstream /market ──► derivatives_feed ──► Redis perp:mark:* , channel:perp_mark_ticks ──┐
Binance fstream /public ──► ingest (perp quotes, 50 Hz) ──► ui_quote_tick + ingress_health ──┐ │
Binance stream (spot)   ──► ingest (spot, 50 Hz)       ─┘                                   │ │
                                                                                           ▼ ▼
 web ticket ──► POST /api/v1/paper/orders ──► execution_commands (PAPER) ──► orchestrator (PaperTradingEngine)
                                                                              │  GuardedOrderDispatcher
                                                                              │  risk monitor · liquidations · funding
                                                                              ▼
                                           EventBridgeWorker ──► paper_positions / paper_trades / paper_accounts
                                           Redis channel:perp_liquidations / perp_funding_events / system_alerts
                                                                              ▼
                               API StreamHub /ws/stream ──► perp_mark_tick · perp_liquidation · perp_funding ──► web store / HUD
```

## 1. Market data ingest

| Feed | Producer | Endpoint | Output |
|---|---|---|---|
| Mark price, index, funding rate, next funding time | `services/scanner/cryptonite_scanner_service/derivatives_feed.py` (compose service `derivatives_feed`) | `wss://fstream.binance.com/market/stream?streams=<sym>@markPrice@1s` | `perp:mark:{SYMBOL}` (15 s TTL), `channel:perp_mark_ticks`, settled funding → `funding_rates` |
| Open interest | same service (REST, 30 s) | `GET /fapi/v1/openInterest` | `perp:oi:{SYMBOL}` (90 s TTL) |
| Perp top of book | `services/ingest` (`INGEST_PERP_SYMBOLS`) | `wss://fstream.binance.com/public/stream?streams=<sym>@bookTicker` | `cryptonite:stream:ui_quote_tick`, `cryptonite:stream:ingress_health`, bus topic `data.quotes.BINANCE.<SYM>-PERP` — keyed **`<SYM>-PERP.BINANCE`** |
| Spot klines + top of book | `services/ingest` (`INGEST_SYMBOLS`) | `wss://stream.binance.com:9443/stream` | bars (DB + catalog), quotes keyed `<SYM>.BINANCE` |

- **Category-routed futures WebSockets.** Binance delivers USD-M klines/marks only on `/market/...` and book
  tickers only on `/public/...`; the legacy `/ws` and `/stream` paths accept connections and stay silent.
- **Event time, not wall clock.** Mark ticks carry the exchange `E` and `T` (ns); the dispatcher's staleness
  check and funding settlement use them.
- **Top-of-book coalescing (50 Hz).** `BinanceStreamClient(min_quote_interval_ms=...)` publishes a symbol only
  when its book changed and at most once per interval (`INGEST_PERP_QUOTE_HZ`, `INGEST_SPOT_QUOTE_HZ`, both 50
  in compose; code default for spot is 0 = unthrottled). The publish sequence advances only for published quotes,
  so the ingress freshness monitor never sees a gap. Unthrottled BTC spot (~160 quotes/s × 3 Redis writes)
  previously starved the ingest event loop.
- Perp quotes are never mapped onto spot quotes: spot/perp basis would skew fills and price collars.

## 2. Margin math (`packages/execution/cryptonite_execution/margin.py`, mirrored in `apps/web/src/lib/perp/margin.ts`)

Pure `Decimal` functions (TS port verified against the Python golden vectors to 1e-8):

```
IM   = notional / leverage
MM   = notional × MMR − cum                     (tier chosen by notional)
uPnL = side × qty × (mark − entry)              (side +1 long, −1 short)
LP   = (WB − TMM + UPNL + cum − side·qty·entry) / (qty·MMR − side·qty)
MR   = MM / margin_balance                      (liquidation at MR ≥ 1)
funding cash flow = −signed_size × mark × rate  (positive rate: longs pay)
```

- **Tiered brackets.** BTCUSDT/ETHUSDT use a labelled *reference snapshot* of Binance's published tables
  (floor, cap, max leverage, MMR); `cum` is derived per tier so MM is continuous at every floor. Live
  `/fapi/v1/leverageBracket` data is Phase 2 work.
- **Self-consistent tier.** The liquidation price is solved in the tier that contains the notional *at the
  liquidation price*, not at entry (e.g. 1 BTC 10× short from 50,000 liquidates at 54,776.12 in tier 2, not the
  entry-tier 54,780.88).
- ISOLATED: `WB` = isolated margin, other-position terms 0. CROSS: `WB` = cross wallet (cash minus isolated
  margins), with the other cross positions' MM and uPnL.
- Bankruptcy price (ISOLATED): `entry ∓ isolated_margin / qty`.

## 3. Order risk (`GuardedOrderDispatcher`, `risk.py`)

All orders still pass the single dispatcher. Perp instruments (`-PERP`) take the perp branch; spot is unchanged.

| Check | Rule | Reject code |
|---|---|---|
| Instrument flags | Perp leverage 1–20 (default 1), margin mode ISOLATED/CROSS (default CROSS); spot rejects leverage > 1, margin mode, reduce-only | `LEVERAGE_OUT_OF_RANGE`, `INVALID_MARGIN_MODE`, `SPOT_*` |
| Latches | Kill switch and daily-loss latch block everything except kill-switch exits and **perp reduce-only** orders | `KILL_SWITCH_ENGAGED`, `DAILY_LOSS_LIMIT_BREACHED` |
| Mark price | Present and ≤ 10 s old for entries (reduce-only exempt) | `NO_MARK_PRICE`, `STALE_MARK_PRICE` |
| Single owner | One strategy instance per perp position (DB rows are per instance) | `PERP_POSITION_OWNED_BY_OTHER_INSTANCE` |
| Reduce-only | Position must exist, order must oppose it, qty ≤ position − resting reduce-only; never flips | `REDUCE_ONLY_NO_POSITION`, `REDUCE_ONLY_INVALID_DIRECTION`, `REDUCE_ONLY_OVERSIZE` |
| Minimum size | Entry notional ≥ **1%** of equity | `ORDER_BELOW_MIN_NOTIONAL` |
| Concentration | \|position + order\| × mark ≤ **25%** of equity (notional, not margin) | `CONCENTRATION_LIMIT_BREACHED` |
| Margin | IM on newly opened exposure + taker fee ≤ available cash (reserved on dispatch) | `INSUFFICIENT_MARGIN` |

Equity = cash + spot longs at bid + perp uPnL at mark. Reduce-only orders skip the conviction multiplier,
floor, cap, and margin reservation.

## 4. Fill accounting, liquidation, funding

- **Dual-directional fills** (`_handle_perp_fill`): signed quantity. Same direction or from flat → weighted
  average entry, lock IM. Opposite direction → realize `direction × closed × (fill − entry)`, release IM pro rata.
  **Flip** (non-reduce-only, fill > position) = close leg to flat, then open the remainder at the fill price. A
  reduce-only overfill never opens the opposite side. Cash moves only by realized PnL and fees; `available_cash`
  = cash − reserved − locked margin.
- **Liquidation** (`check_liquidations`, run every second on fresh marks): long mark ≤ P_liq / short mark ≥ P_liq
  (largest MM first, re-evaluated after each close). Cancels resting orders on the symbol, closes the full size
  with an opposing reduce-only order at mark (ISOLATED fills at the bankruptcy price if mark gapped through it),
  charges taker fee + **0.5% clearance**. Loss stops at the isolated margin (ISOLATED) or zero cash (CROSS); the
  shortfall is booked to `insurance_fund_deficit`. Runs regardless of the kill switch.
- **Funding** (`apply_funding_settlement`): settled when a tick's next-funding time rolls forward, at the previous
  funding time with the last pre-rollover rate and mark. ISOLATED payments also move isolated margin (so funding
  can trigger liquidation; a sweep runs immediately). Idempotent per instrument and, in the DB, exactly-once across
  restarts via the journal key `FUNDING:{account}:{instrument}:{T}`.

## 5. Operational safety (orchestrator `risk_monitor_once`, every 1 s)

- **Daily drawdown circuit breaker.** Equity below the day's high-water mark by ≥ **5%** latches the daily-loss
  latch, trips and persists the PAPER kill switch (`DAILY_DRAWDOWN_LIMIT_BREACHED`, via
  `trip_execution_kill_switch`), writes `paper_risk_events`, and publishes `channel:system_alerts` — once per breach.
- **UTC midnight rollover.** The high-water mark is re-based on current equity; daily PnL and the daily-loss latch
  clear (`paper_accounts.daily_realized_pnl`, `utc_trading_day`). The kill switch still needs an operator reset.
- **Kill switch.** The PAPER row in `execution_kill_switch` is the durable copy. Trips from `/paper/kill-switch`
  (command) or `/control/kill-switch/trip` (DB row, synced into the engine within 1 s) latch the engine; the latch
  is restored on restart. Resets (`RESET_SWITCH`, or the step-up-authenticated `KILL_SWITCH_RESET_REQUEST` with
  expiry + generation checks) are refused while any order or position is open.
- Orders from `/paper/orders` and the `/paper` kill switch are enqueued with `environment = 'PAPER'` (the
  orchestrator claims only PAPER; the DEMO executor claims DEMO).

## 6. Persistence

- **Migration 020** (`infra/migrations/versions/020_perpetuals_schema.py`): `paper_positions` gains `leverage`
  (1–125; spot pinned to 1), `margin_mode` (perp rows only), `isolated_margin` (≥ 0; 0 unless ISOLATED),
  `liquidation_price` and `maintenance_margin` (snapshots), and `side = 'SHORT'` for `-PERP` rows (quantity stays
  absolute). New `funding_rates` table keyed `(instrument_id, funding_time_ns)`. Downgrade refuses while SHORT rows
  exist. (`closed_at` predates 020.)
- **Bridge** (`EventBridgeWorker._persist_perp_fill`): perp `ORDER_FILLED` events carry the dispatcher's post-fill
  snapshot (single source of truth). A close or flip marks the open row FLAT / CLOSED with realized PnL (net of the
  fill's fee); a flip then opens a new row. Liquidation events create their missing `paper_orders` row and credit
  the shortfall. Account equity counts perp uPnL, not notional. Restart rehydration reloads perp rows as signed
  positions (not spot longs).

## 7. Web terminal

- **`/ws/stream` perpetual protocol** (API `StreamHub._perp_pubsub_loop`): subscribe with
  `{"action": "subscribe", "channels": ["perpetuals"], "instruments": ["BTCUSDT-PERP.BINANCE"]}` (instruments
  optional; allow-listed). Messages: `{"type": "perp_mark_tick" | "perp_liquidation" | "perp_funding", "data": …}`;
  cached marks are sent on subscribe with `"snapshot": true`. The instrument filter applies to marks only; mark ticks
  are droppable under backpressure, liquidation/funding use the reliable path.
- **`usePerpetualStore`** (`apps/web/src/lib/stores/`): marks, positions (margin fields polled from
  `/paper/positions` + `/paper/accounts` every 3 s), liquidation alert, funding history. Persists only leverage and
  margin mode. `usePerpetualStream` is mounted at page level so liquidation toasts arrive with panels closed.
- **`MarginRiskHUD`**: liquidation distance meter (> 20% SAFE, 5–20% CAUTION, < 5% DANGER, labelled +
  `role=meter`), margin ratio, funding rate with who pays and an HH:MM:SS countdown, liquidation banner.
- **`OrderEntryPanel`**: SPOT/PERP ticket; leverage slider + 1/5/10/20× chips, margin mode (locked while a position
  is open), reduce-only with client-side mirrors of the dispatcher's rules, pre-flight notional / IM / projected
  liquidation price. Mounted with the PAPER-bound `KillSwitchPanel` in the terminal's **Trade** sidebar.

## 8. Verification

- Backend: 331 tests (unit, bridge persistence on a throwaway migrated DB, orchestrator wiring, drawdown, funding,
  liquidation, gateway). Run with the live stack stopped — outbox relay tests share the DB/Redis.
- Web: `apps/web/e2e/perpetuals-cockpit.spec.ts` (3 tests, API/WS mocked).
- Live certification (2026-09-24): 10× ISOLATED BTCUSDT-PERP short round trip on the running stack — see HANDOFF.
