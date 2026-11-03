# Policy comparison A / B / C, 2026-11-03 (H11)

Read-only on production (`SET default_transaction_read_only = on`; the 1m lake read in a 2 GB container). Window:
Phase 2 start, the first `policy_decisions` row at **2026-11-01 23:15:07 UTC**, to 2026-11-03 ~14:50 UTC (about
39.5 hours).

**Verdict: the sample is far too small to conclude anything.** 13 candidates have all three policy records, only 4
of them are labelled (all 15m longs from two days, ETH and BTC), policy B took none of them, and no actual fill came
from any policy. Nothing below says one policy is better than another.

## 1. Candidates and policy records

| Group | Candidates | Notes |
|---|---|---|
| Created since Phase 2 start | 145 | 144 created after the first policy row plus c9740c45, the candidate that produced it |
| Historical rescans (signal more than 1 h before the start) | 124 | lake-repair sweep (H7b), never dispatched: no policy record by design |
| Post-repair rescans, no debate run | 6 | 2026-11-02 19:45–20:00 sweep, not dispatched |
| Live, gate-refused COOLDOWN, **no policy record** | 2 | 713d263e (BTC 1h swing-sniper-pullback-short-15m), 12be2a3e (ETH 15m swing-squeeze-breakout-1h) |
| **Matched: A, B and C all recorded** | **13** | the comparison set |

The two COOLDOWN refusals have no A / B / C rows although other gate-refused candidates do. That gap is outside H11
and noted as a follow-up.

Decisions on the 13 matched candidates:

| Policy | TAKE | SKIP | Size factor on TAKE |
|---|---|---|---|
| A (rules) | 10 | 3 | 0.5 |
| B (current AI path) | 0 | 13 | – |
| C (narrow veto) | 12 | 1 | 1.0 |

## 2. Labels: settled vs pending

| State | Count | Candidates |
|---|---|---|
| Labelled (hold window complete, `candidate_outcomes` row) | 4 | c9740c45, c96b0111, e47069d0, 4b3caba9 (all 15m LONG) |
| Pending (window not elapsed: 15m = 24 h, 1h = 72 h) | 9 | 018f8538 (1324 of 1440 bars), d5b28efb, e5d41c9d, ed956db9, e6d45e6d, a0029a03 (15m); 9d0b07a3, c8f8adc9, bbbbdbd1 (1h) |

## 3. Simulated outcomes on the 4 labelled candidates

Net R, cost model v1. `preset` = the preset's stop and target; `old` = the 5%-stop profile model (stored
`net_r_profile`); `live` = H11 live-parity exits (structural stop, TP1 50% at 2R, global trail, 5% backstop),
computed read-only from the lake, since migration 062 is not on production yet.

| Candidate | Signal (UTC) | A | B | C | preset | old | live |
|---|---|---|---|---|---|---|---|
| c9740c45 ETH 15m LONG | 11-01 23:15 | TAKE ×0.5 | SKIP | TAKE | +0.86 | −2.33 | −0.14 |
| c96b0111 ETH 15m LONG | 11-02 01:15 | TAKE ×0.5 | SKIP | TAKE | +1.01 | −2.59 | +0.01 |
| e47069d0 BTC 15m LONG | 11-02 01:15 | SKIP | SKIP | TAKE | −1.70 | −4.46 | −1.70 |
| 4b3caba9 ETH 15m LONG | 11-02 12:30 | SKIP | SKIP | TAKE | −1.49 | −1.14 | −1.49 |

Per policy (unweighted mean net R of its TAKEs; size factor not applied):

| Policy | TAKEs | preset | old | live | SKIPs' live mean (opportunity) |
|---|---|---|---|---|---|
| A | 2 | +0.94 | −2.46 | −0.07 | −1.60 (2 skips) |
| B | 0 | – | – | – | −0.83 (4 skips) |
| C | 4 | −0.33 | −2.63 | −0.83 | – (0 skips) |

With n = 2 and n = 4 from two days of one market move, these means carry no information about the policies. The
exit model changes the sign of A's result (+0.94 preset vs −0.07 live), which shows how much the exit model matters,
not which policy is better.

Pending candidates are not in any figure above. `scripts/policy_sim.py` gains a `net_r_live` exit set; it reads
the labels once 062 is migrated and backfilled.

## 4. Actual fills (kept apart from the simulation)

Four paper entries were filled in the window. All are Brain order cards approved by the operator; **none came from
policy A, B or C**, and none carries a `candidate_id`. Three match a matched or labelled candidate by identical stop
and target:

| Fill | Candidate (same levels) | A / B / C on it | Result |
|---|---|---|---|
| ETH spot BUY ×3, 11-02 13:06–15:34 | 4b3caba9 (stop 2278.50) | SKIP / SKIP / TAKE | closed by the exit monitor on the 2278.50 structural stop 17:11, −$20.03; label live −1.49R |
| BTC perp SELL 2x, 11-02 22:24 | bbbbdbd1 (stop 77578.61, TP 75581.41) | SKIP / SKIP / TAKE | open; label pending (1h, 72 h) |
| XRP spot BUY, 11-03 11:29 | e6d45e6d (stop 1.2541, TP 1.2686) | TAKE ×0.5 / SKIP / TAKE | open; label pending |

The fourth fill is the 11-02 17:11 exit-monitor close of the ETH position.

## 5. What it would take

At this pace (13 policy-recorded candidates in ~39 hours, labels 24–72 hours behind), a comparison with even 30
labelled TAKEs per policy needs weeks, and B has to take something before a B comparison exists at all. Re-run this
report with `net_r_live` once 062 is deployed and backfilled, and give the totals in overlap-adjusted trades with
day-clustered intervals (`scripts/policy_sim.py`).
