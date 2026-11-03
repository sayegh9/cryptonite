# Live-parity preset replay, 2026-11-03 (H11)

**Not installed.** The packaged `packages/scanner/cryptonite_scanner/data/preset_replay.json` is unchanged; the lead
decides whether to replace it with `results` from `preset_replay_live.json`. Until then the scanner brief shows the
installed figure labelled "5%-stop model, not live exits" with this live-parity figure next to it
(`data/preset_replay_live.json`, display only: gates, profile warnings and the API still read the installed file).

## How it was made

```
docker run --rm --memory 2g -v deploy-main/data/analytics_lake:/lake:ro  (+ the H11 script/module mounts)  <scanner image> \
  python /app/preset_walkforward.py --lake /lake --coins BTCUSDT,ETHUSDT,XRPUSDT,WLDUSDT --since 2026-04-15 \
  --mode live --live-exits '{"*": {"tp_r": 2, "tp1_scale_pct": 50}}' --out /out/preset_replay_live.json
```

- Lake read-only (production `deploy-main/data`), 22 GB free at start, container capped at 2 GB.
- Signals: every registered preset on its own timeframes, same stream as the 2026-11-01 walk-forward.
- Exits per signal, all on the same fill (open of the bar after the signal), net of cost model v1 (spot for longs,
  perp + default funding for shorts):
  - **old**: `net_r_profile`, the 5% hard stop and +3% / 2% trail only (what the installed file holds);
  - **live**: `net_r_live`, the exit monitor's own rules (`cryptonite_schemas.exit_rules.simulate_live_exit`):
    structural stop = the preset stop (R = fill to that stop), TP1 at 2R for 50%, the remainder on the global
    +3% / 2% trail, 5% hard stop as backstop, no profile max hold (all four active profiles on 2026-11-03:
    `exit_tp_r` 2.00, `exit_tp1_scale_pct` 50, trail and max hold NULL; no exit thresholds stored), so the trade
    expires at the label horizon (15m 24 h, 1h 72 h, 4h 168 h);
  - **preset**: `net_r_preset`, the preset's own stop and target.
- **Live spot TP1 has never happened.** Until this PR the exit monitor sent a spot TP1 as a `SUBMIT_ORDER` SELL
  with no mandate, and the orchestrator refused it `MANDATE_REQUIRED` on every attempt since 2026-10-18. The
  `exit:tp1` flag was set anyway, so TP1 was never retried. Live spot TP1 starts only with this fix (an owner-scoped
  `CLOSE_POSITION`, as perps already used). The replay assumes the fixed behaviour, so spot longs replayed here are
  not what past live spot trades did: those kept the whole position until a stop, the trail or the operator closed it.
- Bars are the signal timeframe (15m / 1h). Inside a bar the path is open → adverse extreme → favourable extreme →
  close, so a bar that touches a stop and TP1 counts as the stop.
- Train / test is chronological: data 2026-04-15 → 2026-11-03 12:45 UTC, cutoff 2026-08-28 00:30 UTC (2/3). Train
  = trades fully closed before the cutoff (491), test = signals at or after it (940); the 10 trades that straddle
  the cutoff are in neither. 55 signals were still open at the data end (censored).
- **No variant selection**: every signal of every preset counts. **Overlap is not removed**: "overlap-adj." is how
  many trades are left if each coin holds one position at a time under the live exits.
- The swing presets have no train-window trades in this run, so they have test numbers only.

## Old vs new
### Per preset (all coins pooled; same signals in every column)

| Preset | Train n (overlap-adj.) | Train old | Train live | Train preset | Test n (overlap-adj.) | Test old | Test live | Test preset | Test live − old |
|---|---|---|---|---|---|---|---|---|---|
| bollinger-breakdown-short | 118 (71) | -0.38 | -0.21 | -0.16 | 118 (55) | -0.06 | -0.27 | -0.15 | -0.21 |
| bollinger-breakout | 128 (68) | +0.41 | +0.12 | -0.06 | 123 (58) | -1.56 | -0.77 | -0.48 | +0.79 |
| macd-momentum-shift | 144 (80) | +0.15 | -0.17 | -0.18 | 154 (57) | -1.41 | -0.58 | -0.44 | +0.83 |
| rsi-oversold-reversal | 30 (29) | -0.60 | -0.71 | -0.81 | 44 (41) | -0.56 | -0.77 | -0.63 | -0.22 |
| scalp-multi-tf-short | 0 (0) | – | – | – | 2 (2) | +2.68 | +0.96 | -0.25 | -1.72 |
| swing-sniper-pullback-15m | 0 (0) | – | – | – | 21 (20) | -3.52 | -0.93 | -1.25 | +2.59 |
| swing-sniper-pullback-short-15m | 0 (0) | – | – | – | 60 (47) | +1.52 | -0.59 | -0.59 | -2.10 |
| swing-squeeze-breakdown-short-1h | 0 (0) | – | – | – | 239 (83) | -0.02 | -0.09 | -0.12 | -0.07 |
| swing-squeeze-breakout-1h | 0 (0) | – | – | – | 104 (43) | -2.00 | -0.93 | -0.58 | +1.08 |
| swing-trend-pullback-1h | 0 (0) | – | – | – | 3 (3) | -4.74 | -0.04 | -0.65 | +4.70 |
| swing-trend-pullback-short-1h | 0 (0) | – | – | – | 10 (10) | +0.74 | +0.29 | +0.03 | -0.45 |
| trend-pullback | 37 (36) | -0.65 | -0.50 | -0.59 | 28 (19) | +0.73 | -0.10 | -0.52 | -0.83 |
| trend-pullback-short | 34 (28) | +0.35 | -0.70 | -0.59 | 34 (30) | +0.67 | -0.94 | -0.96 | -1.61 |
| **all** | 491 (312) | -0.00 | -0.20 | -0.24 | 940 (468) | -0.63 | -0.48 | -0.40 | +0.15 |

### Per coin × preset, test window (installable rows = `results`)

| Coin | Preset | Test n | Overlap-adj. | Days | Old (5%-stop) | Live | Preset exits | Live − old | Live outcomes | Installed file now (n, avg) |
|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | bollinger-breakdown-short | 57 | 25 | 31 | -0.13 | -0.45 | -0.21 | -0.31 | expired 16, structural 38, trailing 3 | 55, -0.14 |
| BTCUSDT | bollinger-breakout | 53 | 28 | 31 | -1.82 | -0.92 | -0.58 | +0.90 | expired 6, structural 44, trailing 3 | 49, -1.74 |
| BTCUSDT | macd-momentum-shift | 80 | 26 | 30 | -1.47 | -0.71 | -0.53 | +0.76 | expired 17, structural 58, trailing 5 | 72, -1.45 |
| BTCUSDT | rsi-oversold-reversal | 17 | 15 | 16 | -0.41 | -1.01 | -0.90 | -0.60 | expired 3, structural 14 | 15, -0.39 |
| BTCUSDT | scalp-multi-tf-short | 1 | 1 | 1 | -0.24 | -1.76 | -1.76 | -1.51 | structural 1 | 1, -0.24 |
| BTCUSDT | swing-sniper-pullback-15m | 5 | 5 | 4 | -5.67 | -1.90 | -1.51 | +3.76 | structural 5 | 4, -5.47 |
| BTCUSDT | swing-sniper-pullback-short-15m | 11 | 9 | 7 | -1.51 | -1.51 | -1.72 | -0.00 | expired 2, structural 9 | 10, -0.15 |
| BTCUSDT | swing-squeeze-breakdown-short-1h | 54 | 20 | 18 | -0.90 | -0.84 | -0.61 | +0.06 | expired 11, structural 41, trailing 2 | 51, -0.87 |
| BTCUSDT | swing-squeeze-breakout-1h | 38 | 17 | 14 | -2.52 | -1.28 | -0.90 | +1.24 | expired 4, structural 34 | 33, -2.60 |
| BTCUSDT | swing-trend-pullback-1h | 1 | 1 | 1 | -15.06 | -2.16 | -2.16 | +12.90 | structural 1 | 1, -15.06 |
| BTCUSDT | swing-trend-pullback-short-1h | 3 | 3 | 3 | -0.13 | +0.01 | +0.38 | +0.15 | expired 1, structural 2 | 3, -0.13 |
| BTCUSDT | trend-pullback | 17 | 11 | 12 | +1.15 | +0.22 | -0.59 | -0.93 | expired 2, structural 12, trailing 3 | 16, +1.73 |
| BTCUSDT | trend-pullback-short | 18 | 16 | 15 | -0.24 | -1.14 | -1.23 | -0.90 | expired 2, structural 15, trailing 1 | 18, -0.24 |
| ETHUSDT | bollinger-breakdown-short | 61 | 30 | 34 | +0.00 | -0.11 | -0.11 | -0.11 | expired 5, structural 39, trailing 17 | 57, +0.06 |
| ETHUSDT | bollinger-breakout | 70 | 30 | 36 | -1.37 | -0.65 | -0.41 | +0.71 | expired 8, structural 54, trailing 8 | 64, -1.48 |
| ETHUSDT | macd-momentum-shift | 74 | 31 | 38 | -1.35 | -0.44 | -0.34 | +0.91 | expired 16, structural 50, trailing 8 | 67, -1.45 |
| ETHUSDT | rsi-oversold-reversal | 27 | 26 | 18 | -0.65 | -0.63 | -0.45 | +0.03 | expired 2, structural 20, trailing 5 | 27, -0.65 |
| ETHUSDT | scalp-multi-tf-short | 1 | 1 | 1 | +5.61 | +3.68 | +1.25 | -1.93 | trailing 1 | 1, +5.61 |
| ETHUSDT | swing-sniper-pullback-15m | 8 | 8 | 6 | -1.32 | -1.02 | -1.03 | +0.29 | structural 7, trailing 1 | 6, -1.90 |
| ETHUSDT | swing-sniper-pullback-short-15m | 17 | 13 | 10 | +0.65 | -1.00 | -1.32 | -1.65 | expired 2, structural 14, trailing 1 | 15, +0.86 |
| ETHUSDT | swing-squeeze-breakdown-short-1h | 56 | 19 | 17 | +0.24 | -0.00 | -0.13 | -0.25 | expired 6, structural 33, trailing 17 | 54, +0.23 |
| ETHUSDT | swing-squeeze-breakout-1h | 29 | 9 | 11 | -2.44 | -1.06 | -0.33 | +1.37 | structural 28, trailing 1 | 24, -3.10 |
| ETHUSDT | swing-trend-pullback-short-1h | 3 | 3 | 3 | +1.74 | +0.55 | -0.26 | -1.19 | expired 1, structural 1, trailing 1 | 3, +1.74 |
| ETHUSDT | trend-pullback | 11 | 8 | 8 | +0.07 | -0.60 | -0.41 | -0.67 | structural 7, trailing 4 | 8, +0.78 |
| ETHUSDT | trend-pullback-short | 16 | 14 | 11 | +1.68 | -0.72 | -0.67 | -2.40 | expired 1, structural 13, trailing 2 | 15, +1.62 |
| WLDUSDT | swing-sniper-pullback-15m | 5 | 4 | 4 | -1.95 | +0.88 | -0.85 | +2.83 | structural 2, trailing 3 | 5, -1.95 |
| WLDUSDT | swing-sniper-pullback-short-15m | 22 | 17 | 13 | +2.35 | +0.33 | +0.54 | -2.01 | expired 2, structural 13, trailing 7 | 19, +2.22 |
| WLDUSDT | swing-squeeze-breakdown-short-1h | 60 | 23 | 18 | +0.70 | +0.51 | +0.17 | -0.19 | expired 3, hard 1, structural 23, trailing 33 | 54, +0.58 |
| WLDUSDT | swing-squeeze-breakout-1h | 17 | 9 | 7 | -0.81 | -0.26 | -0.60 | +0.56 | structural 10, trailing 7 | 17, -0.81 |
| WLDUSDT | swing-trend-pullback-1h | 2 | 2 | 2 | +0.42 | +1.02 | +0.11 | +0.60 | structural 1, trailing 1 | 2, +0.42 |
| WLDUSDT | swing-trend-pullback-short-1h | 2 | 2 | 2 | -0.43 | +0.24 | +0.24 | +0.67 | expired 1, structural 1 | 1, -2.43 |
| XRPUSDT | swing-sniper-pullback-15m | 3 | 3 | 3 | -8.45 | -2.08 | -2.08 | +6.36 | structural 3 | 3, -8.45 |
| XRPUSDT | swing-sniper-pullback-short-15m | 10 | 8 | 7 | +4.48 | -0.90 | -0.58 | -5.38 | structural 9, trailing 1 | 10, +4.48 |
| XRPUSDT | swing-squeeze-breakdown-short-1h | 69 | 21 | 17 | -0.16 | -0.08 | +0.01 | +0.07 | expired 12, structural 41, trailing 16 | 61, -0.11 |
| XRPUSDT | swing-squeeze-breakout-1h | 20 | 8 | 9 | -1.41 | -0.63 | -0.32 | +0.78 | expired 2, structural 15, trailing 3 | 20, -1.41 |
| XRPUSDT | swing-trend-pullback-short-1h | 2 | 2 | 2 | +1.72 | +0.35 | -0.29 | -1.37 | structural 1, trailing 1 | 2, +1.72 |

## Reading it

- Over all 940 test signals: old −0.63R, live −0.48R, preset exits −0.40R. The live exits do not rescue the
  presets: the pooled test mean stays negative, and so does every preset with 20 or more test signals (best: swing-squeeze-breakdown-short-1h −0.09R).
- The old model was biased both ways, not only against tight stops. It inflated losses where the structural stop is
  tight (bollinger-breakout +0.79R, macd-momentum-shift +0.83R, swing-squeeze-breakout-1h +1.08R better under live
  exits) and inflated gains where the price first ran through the structural stop and recovered (trend-pullback-short
  −1.61R, swing-sniper-pullback-short-15m −2.10R, XRP swing-sniper-pullback-short-15m +4.48 → −0.90).
- The "−1.41R replay" an XRP debate cited is XRP / swing-squeeze-breakout-1h: −0.63R under live exits on the same
  20 signals (8 after overlap), still negative.
- Most coin × preset cells have under 30 test signals and under 20 overlap-adjusted trades: treat them as thin.

## The three labelled production candidates (real 1m bars, read-only)

| Candidate | Stored net_r_preset | Stored net_r_profile | net_r_live | Live exit |
|---|---|---|---|---|
| c9740c45 ETH 15m LONG | +0.86 | −2.33 | −0.14 | TP1 50% at 2R, remainder on the structural stop (bar 1089) |
| c96b0111 ETH 15m LONG | +1.01 | −2.59 | +0.01 | TP1 50% at 2R, remainder on the structural stop (bar 884) |
| e47069d0 BTC 15m LONG | −1.70 | −4.46 | −1.70 | structural stop (bar 331), same as the preset stop |

The live label is 2.2–2.8R above the old profile label on all three. On the stop case it equals the preset label. On
the two target cases it is about 1R below the preset label: the preset banks the whole position at its 1.5R target,
while the live monitor scales out half at 2R and lets the other half ride back to the structural stop.
