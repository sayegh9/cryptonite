# Leverage Stage 6: how perps reach the Binance futures testnet (decision needed)

Status: open, for the operator. Stage 6 starts only after the Stage 5 paper gate passes
(`scripts/perp_gate_status.py`). The venue steps it needs are built and mock-tested in
`cryptonite_execution.perp_venue` (#106): `prepare_entry`, `protect_position`, `reconcile`, `flatten_all`.

## Why it is a decision, not a wiring task

`DemoExecutionPipeline` (`packages/execution/cryptonite_execution/demo_pipeline.py`, the DEMO executor) is built for
spot:

- **Reservations are spot balances.** `process_submit_order` reserves `USDT` for a BUY and the base coin for a SELL
  (`reserve_asset = "USDT" if side == "BUY" else symbol.replace("USDT", "")`). A perp short would reserve BTC that does
  not exist, and a perp entry needs its *initial margin* reserved, not its notional.
- **Filters and OCO are spot.** `DynamicExchangeFilterValidator` reads spot filters; `SpotOcoManager` handles spot
  OCO lists. USD-M has its own `exchangeInfo` filters and no OCO (the protective order is a `closePosition` stop).
- **Execution reports are spot.** Fills arrive on the spot user-data stream shape; USD-M sends `ORDER_TRADE_UPDATE`
  and `ACCOUNT_UPDATE` (position and margin changes, liquidation notices) on its own listen key.

Hooking the venue steps into this pipeline without changing those three would look complete while the money handling
stays wrong for perps.

## Options

| | A. Extend `DemoExecutionPipeline` | B. A separate futures executor |
|---|---|---|
| Shape | Perp branches in reservation, filters, OCO and report handling | `services/futures_executor`: claims `DEMO` perp commands only, uses `perp_venue`, USD-M user-data stream |
| Reuse | Fencing tokens, advisory lock, ambiguous-outcome protocol, outbox | Copies the fencing / ambiguous-outcome protocol (or extracts it to a shared module first) |
| Risk | Touches the spot path that already runs; every spot test must keep passing | Spot path untouched; two executors must never claim the same command (claim by instrument suffix) |
| Margin | New reservation kind for initial margin | Margin read from `ACCOUNT_UPDATE`; reservation = initial margin + fee |
| Kill switch | One latch, one flatten for both | Both executors honour the same latch; `flatten_all` for perps |

**Recommendation: B**, with the fencing / ambiguous-outcome protocol extracted into a shared module first, so both
executors use one implementation. It leaves the spot executor unchanged, and the perp path is small enough to review
on its own.

## What Stage 6 then builds (either option)

1. Claim only `DEMO` commands for `-PERP` instruments (B) or branch on them (A).
2. Before each entry: `prepare_entry` (margin type and leverage set only where they differ, read back, refusals as
   command rejections).
3. Reserve the initial margin plus fee, not the notional.
4. On the entry fill: `protect_position` (the `closePosition` stop on the mark; if refused, close at once).
5. USD-M user-data stream (`ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`) into the same journal and outbox as spot.
6. `reconcile` every few seconds against `paper_positions`-style DEMO rows; a mismatch latches the DEMO kill switch.
7. Kill switch: `flatten_all` on the exchange.
8. Prerequisites (HANDOFF operator items): DEMO kill switch reset, credentials rotated, CI restored, wired network.
