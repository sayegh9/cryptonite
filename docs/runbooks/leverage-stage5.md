# Leverage Stage 5: paper validation of AI perps

Stage 5 of [leverage-trading-plan.md](../leverage-trading-plan.md) (ADR 0009). It starts after the leverage release
(#105, migration 048) is deployed and ends when the perp gate passes, or when the operator stops it. Nothing in it
touches Binance with keys: it is paper only. Stage 6 (the futures testnet) is a separate go/no-go after it.

## 1. Before the first trade

1. Deploy check: `alembic current` is `048`; Settings shows the leverage guardrails (10x ceiling, perps at L1 only,
   1–2% risk per trade, grade ceilings A 10 / B 5 / C 2, slippage allowance 0.3%).
2. Optional, for perps beyond BTC and ETH: a **read-only** Binance USD-M key for `derivatives_feed`
   (`BINANCE_FUTURES_READONLY_API_KEY`, `BINANCE_FUTURES_READONLY_PRIVATE_KEY_PATH`, the Ed25519 PEM mounted
   read-only). Check: `redis-cli KEYS 'perp:brackets:*'` lists the symbols, and new entries on other symbols stop
   being refused with `NO_LEVERAGE_BRACKETS`.
3. Create the Stage 5 mandate in Mission Control (TOTP), separate from the spot mandates so the gate reads only it:
   - name `Perps BTC-ETH`, instruments `BTCUSDT-PERP`, `ETHUSDT-PERP`, budget $10,000, position limit 25%,
     hard stop 20%, level L1;
   - `allow_perps` on, `max_leverage` **5** for the first two weeks, `max_risk_per_trade_pct` 1.
4. Keep the exit monitor's Mission Control tile in view: blind time on the mark must stay near zero.

## 2. Running it

- Entries come only from Brain perp cards that you approve with TOTP (L1). Every card must show a stop, the
  grade with its reasons, the binding ceiling, and the loss at the stop. Refuse a card whose reasoning does not match
  its grade.
- After two weeks with no liquidation, no stop failure and no proximity alert, you may raise `max_leverage` to 10
  (TOTP). The grade ceilings still cap each trade.
- Do not close Stage 5 trades by hand unless something is wrong: manual closes hide whether the AI's exits work.
  If you do, note why in the conversation.
- Funding is booked against the position; long holds in crowded funding show up as worse R.

## 3. Reading the gate

```bash
docker compose -f infra/docker-compose.yml run --rm --no-deps test \
  uv run python scripts/perp_gate_status.py --mandate "Perps BTC-ETH"
```

Required (all must pass): at least 30 closed AI perp trades over at least 30 days, 0 liquidations, 0 stop failures
(a loss beyond 1.25 R, or a `CLOSE_SHORT` alert), every trade carrying its plan, and a mean R above zero after fees
and funding. Informational: the mean R's 90% interval, the spot record over the same window, and whether grade A
beats B beats C. If the grades do not order after 30 graded trades, lower every grade ceiling to B's (Settings).

## 4. Stop conditions (pause the mandate, then look)

- Any liquidation, or any `LIQUIDATION_PROXIMITY` close.
- A stop failure: the gate lists the position id.
- Mean R below −0.5 after 15 trades.
- A resting stop logged CRITICAL as refused ("the position has no resting stop") in the orchestrator log.
