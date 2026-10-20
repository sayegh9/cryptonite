# Leverage replay of the AI spot record (October 2026)

Status: research, read-only. Written 2026-10-20 from the dev DB `cryptonite` (SELECT only), the live
DuckDB lake in `deploy-main/data` (read-only, 1 GB DuckDB memory cap) and Binance public futures
endpoints. No order was placed, no service was touched, no model was called.
Script: [`leverage_replay_2026_10.py`](leverage_replay_2026_10.py), raw output:
[`leverage_replay_2026_10.json`](leverage_replay_2026_10.json).

## Bottom line

**The record does not support any leverage above 1x yet.** Liquidation was not what went wrong. The two
problems are that the trades lose money and that the stops fail too often.

- **Expectancy is negative.** There are 12 closed AI-mandate spot trades: 4 wins, 8 losses, **−$86.57**.
  Per trade that is a mean of **−0.39 R** (SD 1.34 R, t ≈ −1.0). Twelve trades are not enough to prove the
  edge is negative, but nothing in them points to a positive one. The best trade (+$87.63, WLD +8.98%) was
  closed by the operator in the web UI, not by the AI or its stops. The 10 trades the AI or its stops
  exited made **−$177.28**, with 2 wins.
- **Leverage only scales that loss.** Under the current 25% notional cap the total goes from −$88 at 1x to
  **−$293 at 5x and at 10x**. The cap binds from about 2.6x, so 5x and 10x give identical PnL; 10x only
  posts less margin and moves the liquidation price closer. With the same margin and no mandate-level cap
  it is **−$880 at 10x**. Under fixed-risk sizing, leverage does not change PnL at all (−$272 at 0.5% risk,
  −$544 at 1% risk, at every leverage). It only changes how much margin is posted.
- **0 of 12 trades would have been liquidated** at 3x, 5x or 10x isolated, on either the spot 1m path or
  the Binance mark-price 1m path. The tightest 10x headroom was about 3.6 percentage points (ETH 10-05).
  The lowest leverage that any trade's actual path would have liquidated is **16x**.
- **Market slippage on stops is small; stop failures are not.** On the 6 hard and structural stops, fills
  came in 0.02–0.42% of entry below the trigger (the XRP hard stop: −5.27% against −5%, 0.27% past
  the trigger). That is 0.2–4.2% of margin at 10x. But **4 of the 12 trades had a stop that did not do
  its job**. Three of them cost 2.7–3.1% of entry beyond the intended stop (27–31% of margin at 10x). The
  fourth left a BTC position 5.7% under water with its hard stop only sending an alert; it later recovered
  to −1.9%. At 10x isolated, a missed stop plus a 9–10% move means liquidation.
- **"5–10x depending on how good the setup looks" cannot be tested.** Brain order cards record no setup
  quality or conviction score. Only the 4 early debate entries have one (0.4–0.8), and 4 trades cannot
  calibrate anything.

Recommendation: keep the 5x cap and the L3-only policy as they are. The prerequisites for revisiting are in
[Conclusion](#conclusion).

## What was replayed

**Trades.** Every `paper_positions` row with `status = 'CLOSED'`, an instrument not like `%PERP%`, and an
`AI_MANDATE_*` instance: 12 positions across 3 mandates (BTC-ETH spot L1, Brain XRPUSDT, WLDUSDT). Each has
`capital_budget_usd` = $10,000. The 13th closed spot position is the MANUAL_TRADE BTC position (−$60.58).
It is excluded, and it accounts for the gap to the "−$147" figure (−$86.57 − $60.58 = −$147.15). Fills come
from `paper_trades`, each linked to its `paper_orders` row. Two positions (BTC 10-04, ETH 10-05) scaled in
with two buys, and the replay keeps both legs at their real times and sizes. The WLD 10-17 position has a
0.000002 dust sell, which is ignored for timing. Exit reasons come from the `CLOSE_POSITION` rows in
`execution_commands`. Structural stops and targets come from the entry `SUBMIT_ORDER` payload
(`proposed_stop_loss`).

**Perp model.** Each trade is opened as a USD-M long with the same entry and exit times and the same
prices (spot fills stand in for perp fills, so basis is ignored). Margin is ISOLATED. The taker fee is 0.05%
on each side; the spot paper engine charges 0.10%, which is why 1x perp comes out slightly better than spot.
A liquidated trade loses its whole isolated margin plus the entry fee, matching
`GuardedOrderDispatcher._liquidate`, where an isolated loss stops at the margin.

**Liquidation price** comes from `cryptonite_execution.margin.calculate_liquidation_price` (ISOLATED,
`wallet_balance` = isolated margin), with its self-consistent bracket search. BTC and ETH use
`REFERENCE_BRACKETS`. `GET /fapi/v1/leverageBracket` returns `401 API-key format invalid` without a key, so
**XRP and WLD brackets are an assumption**:

- XRP first tier: < $10k notional, MMR 0.5%.
- WLD first tier: < $5k notional, MMR 1.0%, then 1.5% up to $25k.

These are shaped like Binance's published alt-coin tables. A sensitivity run with every assumed MMR doubled
moves the 10x liquidation price up by 0.45–0.95% of entry and still liquidates nothing.

**Path check.** Two sources are checked for each trade:

- Spot 1m bars from the lake (`binance_klines_enriched/binance/spot/1m`; `ts_event_ns` is the bar close).
- Binance `GET /fapi/v1/markPriceKlines` 1m. The mark price is what actually triggers a perp liquidation.

A trade counts as liquidated if either path's low touches the liquidation price between the entry minute
and the exit minute, both included. That is conservative: the window can include wicks after the fill.

The lake has one 784-minute hole in every symbol, from 2026-10-19 21:23 to 2026-10-20 ~10:28 UTC. It
affects trades 5, 11 and 12 (see the bar counts in Table 1). The mark-price path covers that window
completely and agrees.

**Funding** comes from `GET /fapi/v1/fundingRate`: each settlement inside the holding window pays
`−qty × markPrice × rate`. For BTC and ETH, every `BINANCE_REST` row in the `funding_rates` table matches the
API value. Some settlements have only a `STREAM_SETTLEMENT` row, and those differ from the final rate by up
to about 1e-5.

**Equity and sizing.** Equity is $10,000 per mandate (the budget). Totals are also shown as a percentage of
the $30k held by the 3 mandates that traded.

- **(a) Current invariant.** Notional = real spot cost × L, capped at 25% of the $10k mandate ($2,500).
  The dispatcher's own 25% check runs against the paper *account* equity of about $101k, so it would never
  bind at these sizes. That uncapped case is shown as (a′).
- **(b) Fixed risk.** Notional = (0.5% or 1% of $10k) ÷ stop distance. The stop distance comes from the
  structural stop when the entry carried one, otherwise from the 5% hard stop. Margin = notional ÷ L. The
  rule is applied as written, even where it breaks the 25% cap: 5 trades at 0.5% risk and 8 at 1% risk.

## Table 1: The trades and their paths

MAE and MFE are the worst and best 1m excursions from the average entry, including the exit minute. The
last column is the lowest leverage at which this path would have touched the liquidation price (rule (a)
notional).

| # | Trade (UTC entry) | Legs | Entry | Exit | Exit reason | Move | Spot PnL | Stop used for sizing | MAE (worst of spot/mark 1m) | MFE | 1m bars lake / mark | Funding events (Σ rate) | Lowest L liquidated by this path |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 2 | 85269.1 | 83632.8 | AI_SELL_CARD | -1.92% | −$19.88 | 5.00% (hard 5%) | -5.75% | +2.03% | 12743 / 12973 of 12972 | 27 (+0.0407%) | 17x |
| 2 | ETH 10-05 02:47 | 2 | 2727.54 | 2590.54 | HARD_STOP | -5.02% | −$50.42 | 5.00% (hard 5%) | -5.96% | +0.26% | 3318 / 3321 of 3320 | 7 (+0.0211%) | 16x |
| 3 | XRP 10-13 16:06 | 1 | 1.3757 | 1.3032 | HARD_STOP | -5.27% | −$26.82 | 2.60% (structural) | -5.36% | +0.81% | 2017 / 2057 of 2057 | 4 (-0.0071%) | 18x |
| 4 | WLD 10-16 15:28 | 1 | 0.5161 | 0.5131 | TRAILING_STOP | -0.58% | −$6.57 | 4.09% (structural) | -4.55% | +4.59% | 1224 / 1237 of 1237 | 3 (+0.0167%) | 19x |
| 5 | BTC 10-17 12:20 | 1 | 81550 | 81892 | MANUAL_CLOSE | +0.42% | +$3.08 | 1.04% (structural) | -1.09% | +1.97% | 3607 / 4417 of 4417 | 9 (+0.0612%) | 68x |
| 6 | WLD 10-17 18:07 | 1 | 0.4964 | 0.4695 | HARD_STOP | -5.42% | −$53.25 | 2.30% (structural) | -5.54% | +3.26% | 1432 / 1449 of 1449 | 3 (-0.0267%) | 16x |
| 7 | XRP 10-18 11:23 | 1 | 1.3009 | 1.2826 | STRUCTURAL_STOP | -1.41% | −$14.54 | 1.07% (structural) | -1.49% | +0.06% | 125 / 126 of 126 | 0 (+0.0000%) | 51x |
| 8 | WLD 10-18 18:40 | 1 | 0.473355 | 0.4613 | STRUCTURAL_STOP | -2.55% | −$25.67 | 2.48% (structural) | -2.69% | -0.33% | 62 / 62 of 61 | 0 (+0.0000%) | 28x |
| 9 | XRP 10-18 18:42 | 1 | 1.2728 | 1.2943 | TRAILING_STOP | +1.69% | +$15.33 | 1.23% (structural) | -1.35% | +3.87% | 1160 / 1162 of 1162 | 2 (+0.0054%) | 55x |
| 10 | ETH 10-18 18:47 | 1 | 2365.57 | 2338.22 | STRUCTURAL_STOP | -1.16% | −$12.12 | 1.00% (structural) | -1.22% | +0.18% | 53 / 53 of 53 | 0 (+0.0000%) | 59x |
| 11 | XRP 10-19 16:51 | 1 | 1.3028 | 1.3266 | TRAILING_STOP | +1.83% | +$16.66 | 1.62% (structural) | -1.30% | +3.35% | 557 / 1343 of 1342 | 2 (+0.0124%) | 56x |
| 12 | WLD 10-19 18:25 | 1 | 0.5067 | 0.5522 | MANUAL_CLOSE | +8.98% | +$87.63 | 5.00% (hard 5%) | -1.07% | +10.26% | 387 / 1172 of 1172 | 2 (+0.0200%) | 49x |

Note on trade 1 (BTC 10-04): the path spent 99 one-minute bars below the −5% hard stop on 2026-10-08
between 15:38 and 18:20 UTC, and no `CLOSE_POSITION` was raised. The reason is that the instrument was
shared with the MANUAL_TRADE BTC position (open 10-07 → 10-17). On a shared instrument the exit monitor only
sends an alert (`exit_monitor.py` docstring: "a shared instrument only gets an alert"). The AI closed the
position on 10-13 with an order-card SELL at −1.92%.

## Table 2: Liquidation prices (ISOLATED, rule (a) notional)

At 1x a long has no positive liquidation price. Distance is measured from the average entry.

| # | Trade | 3x liq (dist) | 5x liq (dist) | 10x liq (dist) | Touched before exit? (1/3/5/10x) |
|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 57074.4 (33.07%) | 68489.2 (19.68%) | 77050.4 (9.64%) | no/no/no/no |
| 2 | ETH 10-05 02:47 | 1827.5 (33.00%) | 2193 (19.60%) | 2467.12 (9.55%) | no/no/no/no |
| 3 | XRP 10-13 16:06 | 0.921742 (33.00%) | 1.10609 (19.60%) | 1.24435 (9.55%) | no/no/no/no |
| 4 | WLD 10-16 15:28 | 0.347542 (32.66%) | 0.417051 (19.19%) | 0.469182 (9.09%) | no/no/no/no |
| 5 | BTC 10-17 12:20 | 54585 (33.07%) | 65502 (19.68%) | 73689.8 (9.64%) | no/no/no/no |
| 6 | WLD 10-17 18:07 | 0.334276 (32.66%) | 0.401131 (19.19%) | 0.451273 (9.09%) | no/no/no/no |
| 7 | XRP 10-18 11:23 | 0.871625 (33.00%) | 1.04595 (19.60%) | 1.17669 (9.55%) | no/no/no/no |
| 8 | WLD 10-18 18:40 | 0.318758 (32.66%) | 0.382509 (19.19%) | 0.430323 (9.09%) | no/no/no/no |
| 9 | XRP 10-18 18:42 | 0.852797 (33.00%) | 1.02336 (19.60%) | 1.15128 (9.55%) | no/no/no/no |
| 10 | ETH 10-18 18:47 | 1584.97 (33.00%) | 1901.97 (19.60%) | 2139.71 (9.55%) | no/no/no/no |
| 11 | XRP 10-19 16:51 | 0.872898 (33.00%) | 1.04748 (19.60%) | 1.17841 (9.55%) | no/no/no/no |
| 12 | WLD 10-19 18:25 | 0.341212 (32.66%) | 0.409455 (19.19%) | 0.460636 (9.09%) | no/no/no/no |

**Liquidations: 0 of 12 at 1x, 3x, 5x and 10x, under every sizing rule and on both price paths.** The
notional tier never leaves the first one or two brackets, so the sizing rule moves the liquidation distance by
at most 0.2 percentage points.

## Table 3: PnL per trade

PnL is after perp fees and funding. The value in parentheses is PnL on posted margin; for PnL on account
equity, divide the dollar figure by $10,000.

### (a) Notional = spot cost × L, capped at 25% of the $10k mandate

| # | Trade | 1x notional / margin | 3x notional / margin | 5x notional / margin | 10x notional / margin | 1x PnL $ (on margin) | 3x PnL $ (on margin) | 5x PnL $ (on margin) | 10x PnL $ (on margin) | funding $ @10x |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 985 / 985 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -20.25 (-2.1%) | -51.38 (-6.2%) | -51.38 (-10.3%) | -51.38 (-20.6%) | -0.93 |
| 2 | ETH 10-05 02:47 | 985 / 985 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -50.61 (-5.1%) | -128.42 (-15.4%) | -128.42 (-25.7%) | -128.42 (-51.4%) | -0.41 |
| 3 | XRP 10-13 16:06 | 500 / 500 | 1,500 / 500 | 2,500 / 500 | 2,500 / 250 | -26.80 (-5.4%) | -80.41 (-16.1%) | -134.01 (-26.8%) | -134.01 (-53.6%) | +0.17 |
| 4 | WLD 10-16 15:28 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -6.74 (-0.7%) | -17.44 (-2.1%) | -17.44 (-3.5%) | -17.44 (-7.0%) | -0.42 |
| 5 | BTC 10-17 12:20 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | +2.49 (+0.3%) | +6.44 (+0.8%) | +6.44 (+1.3%) | +6.44 (+2.6%) | -1.54 |
| 6 | WLD 10-17 18:07 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -53.03 (-5.5%) | -137.25 (-16.5%) | -137.25 (-27.5%) | -137.25 (-54.9%) | +0.66 |
| 7 | XRP 10-18 11:23 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -14.55 (-1.5%) | -37.65 (-4.5%) | -37.65 (-7.5%) | -37.65 (-15.1%) | +0.00 |
| 8 | WLD 10-18 18:40 | 971 / 971 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -25.68 (-2.6%) | -66.14 (-7.9%) | -66.14 (-13.2%) | -66.14 (-26.5%) | +0.00 |
| 9 | XRP 10-18 18:42 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | +15.29 (+1.6%) | +39.57 (+4.7%) | +39.57 (+7.9%) | +39.57 (+15.8%) | -0.14 |
| 10 | ETH 10-18 18:47 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | -12.13 (-1.3%) | -31.39 (-3.8%) | -31.39 (-6.3%) | -31.39 (-12.6%) | +0.00 |
| 11 | XRP 10-19 16:51 | 966 / 966 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | +16.55 (+1.7%) | +42.84 (+5.1%) | +42.84 (+8.6%) | +42.84 (+17.1%) | -0.31 |
| 12 | WLD 10-19 18:25 | 988 / 988 | 2,500 / 833 | 2,500 / 500 | 2,500 / 250 | +87.47 (+8.9%) | +221.35 (+26.6%) | +221.35 (+44.3%) | +221.35 (+88.5%) | -0.53 |

### (a′) Same margin × L, only the account-level 25% cap (about $25k, never binds)

| # | Trade | 1x notional / margin | 3x notional / margin | 5x notional / margin | 10x notional / margin | 1x PnL $ (on margin) | 3x PnL $ (on margin) | 5x PnL $ (on margin) | 10x PnL $ (on margin) | funding $ @10x |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 985 / 985 | 2,956 / 985 | 4,927 / 985 | 9,853 / 985 | -20.25 (-2.1%) | -60.75 (-6.2%) | -101.26 (-10.3%) | -202.51 (-20.6%) | -3.67 |
| 2 | ETH 10-05 02:47 | 985 / 985 | 2,956 / 985 | 4,926 / 985 | 9,852 / 985 | -50.61 (-5.1%) | -151.83 (-15.4%) | -253.05 (-25.7%) | -506.10 (-51.4%) | -1.61 |
| 3 | XRP 10-13 16:06 | 500 / 500 | 1,500 / 500 | 2,500 / 500 | 5,000 / 500 | -26.80 (-5.4%) | -80.41 (-16.1%) | -134.01 (-26.8%) | -268.02 (-53.6%) | +0.35 |
| 4 | WLD 10-16 15:28 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | -6.74 (-0.7%) | -20.22 (-2.1%) | -33.70 (-3.5%) | -67.39 (-7.0%) | -1.62 |
| 5 | BTC 10-17 12:20 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | +2.49 (+0.3%) | +7.47 (+0.8%) | +12.45 (+1.3%) | +24.89 (+2.6%) | -5.93 |
| 6 | WLD 10-17 18:07 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | -53.03 (-5.5%) | -159.08 (-16.5%) | -265.13 (-27.5%) | -530.26 (-54.9%) | +2.53 |
| 7 | XRP 10-18 11:23 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | -14.55 (-1.5%) | -43.64 (-4.5%) | -72.73 (-7.5%) | -145.46 (-15.1%) | +0.00 |
| 8 | WLD 10-18 18:40 | 971 / 971 | 2,912 / 971 | 4,853 / 971 | 9,707 / 971 | -25.68 (-2.6%) | -77.04 (-7.9%) | -128.39 (-13.2%) | -256.79 (-26.5%) | +0.00 |
| 9 | XRP 10-18 18:42 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | +15.29 (+1.6%) | +45.86 (+4.7%) | +76.44 (+7.9%) | +152.88 (+15.8%) | -0.53 |
| 10 | ETH 10-18 18:47 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | -12.13 (-1.3%) | -36.38 (-3.8%) | -60.64 (-6.3%) | -121.27 (-12.6%) | +0.00 |
| 11 | XRP 10-19 16:51 | 966 / 966 | 2,898 / 966 | 4,829 / 966 | 9,659 / 966 | +16.55 (+1.7%) | +49.65 (+5.1%) | +82.75 (+8.6%) | +165.51 (+17.1%) | -1.19 |
| 12 | WLD 10-19 18:25 | 988 / 988 | 2,964 / 988 | 4,940 / 988 | 9,879 / 988 | +87.47 (+8.9%) | +262.41 (+26.6%) | +437.35 (+44.3%) | +874.70 (+88.5%) | -2.09 |

### (b) Fixed risk 0.5% of equity ($50) ÷ stop distance

| # | Trade | 1x notional / margin | 3x notional / margin | 5x notional / margin | 10x notional / margin | 1x PnL $ (on margin) | 3x PnL $ (on margin) | 5x PnL $ (on margin) | 10x PnL $ (on margin) | funding $ @10x |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 1,000 / 1,000 | 1,000 / 333 | 1,000 / 200 | 1,000 / 100 | -20.55 (-2.1%) | -20.55 (-6.2%) | -20.55 (-10.3%) | -20.55 (-20.6%) | -0.37 |
| 2 | ETH 10-05 02:47 | 1,000 / 1,000 | 1,000 / 333 | 1,000 / 200 | 1,000 / 100 | -51.37 (-5.1%) | -51.37 (-15.4%) | -51.37 (-25.7%) | -51.37 (-51.4%) | -0.16 |
| 3 | XRP 10-13 16:06 | 1,927 / 1,927 | 1,927 / 642 | 1,927 / 385 | 1,927 / 193 | -103.28 (-5.4%) | -103.28 (-16.1%) | -103.28 (-26.8%) | -103.28 (-53.6%) | +0.13 |
| 4 | WLD 10-16 15:28 | 1,223 / 1,223 | 1,223 / 408 | 1,223 / 245 | 1,223 / 122 | -8.53 (-0.7%) | -8.53 (-2.1%) | -8.53 (-3.5%) | -8.53 (-7.0%) | -0.20 |
| 5 | BTC 10-17 12:20 | 4,797 / 4,797 | 4,797 / 1,599 | 4,797 / 959 | 4,797 / 480 | +12.36 (+0.3%) | +12.36 (+0.8%) | +12.36 (+1.3%) | +12.36 (+2.6%) | -2.95 |
| 6 | WLD 10-17 18:07 | 2,177 / 2,177 | 2,177 / 726 | 2,177 / 435 | 2,177 / 218 | -119.53 (-5.5%) | -119.53 (-16.5%) | -119.53 (-27.5%) | -119.53 (-54.9%) | +0.57 |
| 7 | XRP 10-18 11:23 | 4,679 / 4,679 | 4,679 / 1,560 | 4,679 / 936 | 4,679 / 468 | -70.47 (-1.5%) | -70.47 (-4.5%) | -70.47 (-7.5%) | -70.47 (-15.1%) | +0.00 |
| 8 | WLD 10-18 18:40 | 2,013 / 2,013 | 2,013 / 671 | 2,013 / 403 | 2,013 / 201 | -53.26 (-2.6%) | -53.26 (-7.9%) | -53.26 (-13.2%) | -53.26 (-26.5%) | +0.00 |
| 9 | XRP 10-18 18:42 | 4,054 / 4,054 | 4,054 / 1,351 | 4,054 / 811 | 4,054 / 405 | +64.16 (+1.6%) | +64.16 (+4.7%) | +64.16 (+7.9%) | +64.16 (+15.8%) | -0.22 |
| 10 | ETH 10-18 18:47 | 5,014 / 5,014 | 5,014 / 1,671 | 5,014 / 1,003 | 5,014 / 501 | -62.95 (-1.3%) | -62.95 (-3.8%) | -62.95 (-6.3%) | -62.95 (-12.6%) | +0.00 |
| 11 | XRP 10-19 16:51 | 3,087 / 3,087 | 3,087 / 1,029 | 3,087 / 617 | 3,087 / 309 | +52.90 (+1.7%) | +52.90 (+5.1%) | +52.90 (+8.6%) | +52.90 (+17.1%) | -0.38 |
| 12 | WLD 10-19 18:25 | 1,000 / 1,000 | 1,000 / 333 | 1,000 / 200 | 1,000 / 100 | +88.54 (+8.9%) | +88.54 (+26.6%) | +88.54 (+44.3%) | +88.54 (+88.5%) | -0.21 |

### (b) Fixed risk 1% of equity ($100) ÷ stop distance

| # | Trade | 1x notional / margin | 3x notional / margin | 5x notional / margin | 10x notional / margin | 1x PnL $ (on margin) | 3x PnL $ (on margin) | 5x PnL $ (on margin) | 10x PnL $ (on margin) | funding $ @10x |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC 10-04 08:46 | 2,000 / 2,000 | 2,000 / 667 | 2,000 / 400 | 2,000 / 200 | -41.11 (-2.1%) | -41.11 (-6.2%) | -41.11 (-10.3%) | -41.11 (-20.6%) | -0.74 |
| 2 | ETH 10-05 02:47 | 2,000 / 2,000 | 2,000 / 667 | 2,000 / 400 | 2,000 / 200 | -102.74 (-5.1%) | -102.74 (-15.4%) | -102.74 (-25.7%) | -102.74 (-51.4%) | -0.33 |
| 3 | XRP 10-13 16:06 | 3,854 / 3,854 | 3,854 / 1,285 | 3,854 / 771 | 3,854 / 385 | -206.56 (-5.4%) | -206.56 (-16.1%) | -206.56 (-26.8%) | -206.56 (-53.6%) | +0.27 |
| 4 | WLD 10-16 15:28 | 2,446 / 2,446 | 2,446 / 815 | 2,446 / 489 | 2,446 / 245 | -17.07 (-0.7%) | -17.07 (-2.1%) | -17.07 (-3.5%) | -17.07 (-7.0%) | -0.41 |
| 5 | BTC 10-17 12:20 | 9,594 / 9,594 | 9,594 / 3,198 | 9,594 / 1,919 | 9,594 / 959 | +24.72 (+0.3%) | +24.72 (+0.8%) | +24.72 (+1.3%) | +24.72 (+2.6%) | -5.90 |
| 6 | WLD 10-17 18:07 | 4,354 / 4,354 | 4,354 / 1,451 | 4,354 / 871 | 4,354 / 435 | -239.06 (-5.5%) | -239.06 (-16.5%) | -239.06 (-27.5%) | -239.06 (-54.9%) | +1.14 |
| 7 | XRP 10-18 11:23 | 9,359 / 9,359 | 9,359 / 3,120 | 9,359 / 1,872 | 9,359 / 936 | -140.95 (-1.5%) | -140.95 (-4.5%) | -140.95 (-7.5%) | -140.95 (-15.1%) | +0.00 |
| 8 | WLD 10-18 18:40 | 4,027 / 4,027 | 4,027 / 1,342 | 4,027 / 805 | 4,027 / 403 | -106.53 (-2.6%) | -106.53 (-7.9%) | -106.53 (-13.2%) | -106.53 (-26.5%) | +0.00 |
| 9 | XRP 10-18 18:42 | 8,107 / 8,107 | 8,107 / 2,702 | 8,107 / 1,621 | 8,107 / 811 | +128.32 (+1.6%) | +128.32 (+4.7%) | +128.32 (+7.9%) | +128.32 (+15.8%) | -0.45 |
| 10 | ETH 10-18 18:47 | 10,028 / 10,028 | 10,028 / 3,343 | 10,028 / 2,006 | 10,028 / 1,003 | -125.91 (-1.3%) | -125.91 (-3.8%) | -125.91 (-6.3%) | -125.91 (-12.6%) | +0.00 |
| 11 | XRP 10-19 16:51 | 6,174 / 6,174 | 6,174 / 2,058 | 6,174 / 1,235 | 6,174 / 617 | +105.80 (+1.7%) | +105.80 (+5.1%) | +105.80 (+8.6%) | +105.80 (+17.1%) | -0.76 |
| 12 | WLD 10-19 18:25 | 2,000 / 2,000 | 2,000 / 667 | 2,000 / 400 | 2,000 / 200 | +177.08 (+8.9%) | +177.08 (+26.6%) | +177.08 (+44.3%) | +177.08 (+88.5%) | -0.42 |

## Table 4: Totals per leverage level and sizing rule

The fees column is the round-trip perp taker fee at 0.05% per side; the real-record row shows the actual
0.10% spot fees.

| Sizing rule | Lev | Total PnL $ | % of $30k (3 mandates) | Worst trade $ (% of $10k) | Liquidated | Funding $ | Fees $ | Wins/Losses | Max notional (% of $10k) |
|---|---|---|---|---|---|---|---|---|---|
| Real spot record | 1x | -86.57 | -0.29% | -53.25 | 0 | 0 | 22.30 | 4/8 | 9.9% |
| (a) 25% cap on the $10k mandate | 1x | -87.98 | -0.29% | -53.03 (-0.53%) | 0 | -1.38 | 11.19 | 4/8 | 10% |
| (a) 25% cap on the $10k mandate | 3x | -239.88 | -0.80% | -137.25 (-1.37%) | 0 | -3.51 | 29.00 | 4/8 | 25% |
| (a) 25% cap on the $10k mandate | 5x | -293.48 | -0.98% | -137.25 (-1.37%) | 0 | -3.44 | 30.00 | 4/8 | 25% |
| (a) 25% cap on the $10k mandate | 10x | -293.48 | -0.98% | -137.25 (-1.37%) | 0 | -3.44 | 30.00 | 4/8 | 25% |
| (a′) same margin × L, account-level cap only | 1x | -87.98 | -0.29% | -53.03 (-0.53%) | 0 | -1.38 | 11.19 | 4/8 | 10% |
| (a′) same margin × L, account-level cap only | 3x | -263.95 | -0.88% | -159.08 (-1.59%) | 0 | -4.13 | 33.57 | 4/8 | 30% |
| (a′) same margin × L, account-level cap only | 5x | -439.92 | -1.47% | -265.13 (-2.65%) | 0 | -6.88 | 55.95 | 4/8 | 49% |
| (a′) same margin × L, account-level cap only | 10x | -879.83 | -2.93% | -530.26 (-5.30%) | 0 | -13.77 | 111.90 | 4/8 | 99% |
| (b) 0.5% risk | 1x | -271.99 | -0.91% | -119.53 (-1.20%) | 0 | -3.80 | 31.97 | 4/8 | 50% |
| (b) 0.5% risk | 3x | -271.99 | -0.91% | -119.53 (-1.20%) | 0 | -3.80 | 31.97 | 4/8 | 50% |
| (b) 0.5% risk | 5x | -271.99 | -0.91% | -119.53 (-1.20%) | 0 | -3.80 | 31.97 | 4/8 | 50% |
| (b) 0.5% risk | 10x | -271.99 | -0.91% | -119.53 (-1.20%) | 0 | -3.80 | 31.97 | 4/8 | 50% |
| (b) 1% risk | 1x | -543.99 | -1.81% | -239.06 (-2.39%) | 0 | -7.60 | 63.94 | 4/8 | 100% |
| (b) 1% risk | 3x | -543.99 | -1.81% | -239.06 (-2.39%) | 0 | -7.60 | 63.94 | 4/8 | 100% |
| (b) 1% risk | 5x | -543.99 | -1.81% | -239.06 (-2.39%) | 0 | -7.60 | 63.94 | 4/8 | 100% |
| (b) 1% risk | 10x | -543.99 | -1.81% | -239.06 (-2.39%) | 0 | -7.60 | 63.94 | 4/8 | 100% |

How to read it:

- **Funding is noise here.** It never exceeds $14 over all 12 trades, even at 10x (a′). Rates were mostly
  small and positive, so longs paid.
- **Under (a), the 25% cap absorbs most of the leverage.** 5x and 10x are the same trade with half the
  margin. The extra leverage buys no return and moves the liquidation price from about 19.6% away to about
  9.5% away.
- **Under (b), leverage is a capital-efficiency setting, not a return lever.** PnL is fixed by the risk
  budget. At 1% risk the tight structural stops (1.0–2.6%) call for $3.9k–$10k notional, which is 39–100%
  of the mandate. That breaks the 25% invariant on 8 of 12 trades. At 1x it would tie up as much as the whole
  mandate in one position; at 5x the margin is at most $2k. The result is still −$544.
- **The AI's own exits are worse than the totals suggest.** Without the two operator closes (trades 5 and
  12), the 10 AI-exited trades make −$178 at 1x and −$521 under (a) at 5x/10x. Under (b) they make −$373
  at 0.5% risk and −$746 at 1% risk.

## Stop slippage and what it costs at leverage

The trigger is known for hard stops (entry × 0.95, on the bid) and for structural stops (the card's
`proposed_stop_loss`). For trailing stops the monitor's best price is not persisted, so the reference is
the spot price when the close command was raised. Slippage is expressed as a percentage of entry. The cost
on margin is slippage × L. The rule (a) dollar cost is slippage × that rule's notional. The rule (b) cost
is in R, which does not depend on leverage.

| # | Trade | Exit | Trigger | Price when close raised | Fill | Delay | Slip vs trigger | Slip vs price at command | Cost on margin at 1x / 3x / 5x / 10x | Rule A cap $ at 1x / 3x / 5x / 10x | Rule B (R) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2 | ETH 10-05 02:47 | HARD_STOP | 2591.17 | 2595.23 | 2590.54 | 0.1s | -0.02% | -0.17% | -0.02% / -0.07% / -0.11% / -0.23% | -0.23 / -0.57 / -0.57 / -0.57 | -0.00R |
| 3 | XRP 10-13 16:06 | HARD_STOP | 1.30691 | 1.3165 | 1.3032 | 0.1s | -0.27% | -0.97% | -0.27% / -0.81% / -1.35% / -2.70% | -1.35 / -4.05 / -6.75 / -6.75 | -0.10R |
|   | ↳ intended stop 1.34 | | | | | | -2.68% vs intended | | -2.7% / -8.0% / -13.4% / -26.8% | | |
| 4 | WLD 10-16 15:28 | TRAILING_STOP | — | 0.528 | 0.5131 | 18592.1s | — | -2.89% | -2.89% / -8.66% / -14.44% / -28.87% | -27.88 / -72.18 / -72.18 / -72.18 | -0.71R |
| 6 | WLD 10-17 18:07 | HARD_STOP | 0.47158 | 0.4724 | 0.4695 | 0.1s | -0.42% | -0.58% | -0.42% / -1.26% / -2.10% / -4.19% | -4.05 / -10.48 / -10.48 / -10.48 | -0.18R |
|   | ↳ intended stop 0.485 | | | | | | -3.12% vs intended | | -3.1% / -9.4% / -15.6% / -31.2% | | |
| 7 | XRP 10-18 11:23 | STRUCTURAL_STOP | 1.287 | 1.2888 | 1.2826 | 0.1s | -0.34% | -0.48% | -0.34% / -1.01% / -1.69% / -3.38% | -3.27 / -8.46 / -8.46 / -8.46 | -0.32R |
| 8 | WLD 10-18 18:40 | STRUCTURAL_STOP | 0.4616 | 0.4628 | 0.4613 | 0.1s | -0.06% | -0.32% | -0.06% / -0.19% / -0.32% / -0.63% | -0.62 / -1.58 / -1.58 / -1.58 | -0.03R |
| 9 | XRP 10-18 18:42 | TRAILING_STOP | — | 1.297 | 1.2943 | 0.1s | — | -0.21% | -0.21% / -0.64% / -1.06% / -2.12% | -2.05 / -5.30 / -5.30 / -5.30 | -0.17R |
| 10 | ETH 10-18 18:47 | STRUCTURAL_STOP | 2341.98 | 2344.88 | 2338.22 | 0.1s | -0.16% | -0.28% | -0.16% / -0.48% / -0.79% / -1.59% | -1.54 / -3.97 / -3.97 / -3.97 | -0.16R |
| 11 | XRP 10-19 16:51 | TRAILING_STOP | — | 1.3193 | 1.3266 | 0.1s | — | +0.56% | +0.56% / +1.68% / +2.80% / +5.60% | +5.41 / +14.01 / +14.01 / +14.01 | +0.35R |

Rows marked ↳ are **stop failures, not market slippage**. They compare the final fill with the stop the
system intended:

1. **XRP 10-13.** The card carried a structural stop at 1.34 (−2.6%), but `STRUCTURAL_STOP` only shipped on
   2026-10-17 (commit 906c550). The trade ran to the 5% hard stop. The extra loss was 2.68% of entry, or
   −26.8% of margin at 10x.
2. **WLD 10-17.** `STRUCTURAL_STOP` fired at 21:37 but sold 0.000002 WLD. The rest of the position rode to
   the hard stop about 20.6 h later. The extra loss was 3.12% of entry, or −31.2% of margin at 10x.
3. **WLD 10-16, trailing.** The `CLOSE_POSITION` was created at 06:55:06 with price about 0.528 (+2.3%), but
   `processed_at` is 12:04:58, a 5 h 10 min wait. It filled at 0.5131 (−0.58%). The delay cost 2.89% of
   entry, which turned a winner into a loser, or −28.9% of margin at 10x. The cause of the wait was not
   investigated here.
4. **BTC 10-04, hard stop alert-only.** This one is not in the table because no stop fired. The position
   reached −5.72% (mark −5.75%) and the hard stop did nothing (see the Table 1 note). At 17x this path
   liquidates.

Market slippage proper (hard and structural stops, trigger → fill) ranged from 0.02% to 0.42% of entry,
with a median of about 0.2%. At 10x that is 0.2–4.2% of margin, tolerable on its own. Measured from the
price when the close was raised, the slippage is larger (0.17–0.97%), because the monitor polls every 30 s
and the quote moves before the fill. The one positive trailing slip (+0.56%, XRP 10-19) is an artefact of
the reference price.

## Conclusion

The record so far does not support any leverage.

1. **No edge has been shown.** The mean is −0.39 R over 12 trades and −$177 on the trades the AI exited
   itself. Leverage multiplies expectancy, and here it multiplies a negative one: every leveraged variant
   loses more than 1x spot.
2. **Liquidation risk at 10x was real but was not hit.** Worst-trade headroom was about 3.6 points. With
   stops working as designed (5% hard stop, 0.5% slippage), 10x isolated keeps roughly 4 points of buffer
   on these coins. With the stop failures seen in 4 of 12 trades, that buffer is gone on the next 10% move,
   and WLD moved more than 10% within 19 hours during trade 12.
3. **Under the 25% cap, leverage above about 3x does nothing for returns.** Under risk-based sizing it
   changes only the margin posted. "5–10x on good setups" only means something if the notional cap is
   raised or replaced. Doing that would need a recorded setup score with demonstrated predictive value,
   which does not exist yet.

**Before reopening the 5x/L3 policy**, these thresholds are suggestions for the operator to set:

- **Stops.** Fix the three failure modes found here: the shared-instrument alert-only stop, the dust-sized
  structural close, and the 5 h close delay. Then show zero stop failures over the next 30+ AI exits.
- **Edge.** A positive mean R at 1x over at least 30–50 AI-exited trades, excluding operator closes.
- **Setup score.** Record a setup or conviction score on every Brain card at entry, and show that higher
  scores give higher R before letting the score choose leverage.
- **Brackets.** Fetch the live leverage bracket table for every alt the mandates trade (this needs a key,
  roadmap C11) before any perp alt goes live.

## Caveats

- The sample is 12 trades from a single 16-day window (2026-10-04 → 2026-10-20).
- Spot prices stand in for perp prices. Perp basis was typically within ±0.05% and is ignored.
- The replay holds the exit fixed. The exit rules are price-based, so they would trigger at the same prices at
  any leverage, but an operator watching margin PnL at 10x might act differently.
- In isolated mode, funding comes out of the isolated margin and moves the liquidation price slightly. The
  replay ignores this because funding was at most 0.06% cumulative per trade.
- XRP and WLD brackets are assumed (see above). The MMR ×2 sensitivity changes no outcome.
- MAE includes the exit minute, so it can include wicks after the fill (for example, ETH 10-05's low of
  −5.96% was in the exit minute).

## Reproduce

```bash
~/Documents/projects/cryptonite/repo/.venv/bin/python -I docs/research/leverage_replay_2026_10.py --cache <scratch dir>
```

The script reads Postgres through `docker exec infra-postgres-1 psql` with SELECT-only `COPY (…) TO STDOUT`,
reads the lake with an in-memory DuckDB (1 GB cap, 2 threads), and caches the Binance public responses under
`--cache`. One run took about 55 s.
