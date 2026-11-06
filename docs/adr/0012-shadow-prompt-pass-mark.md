# ADR 0012: Shadow prompt pass mark

- Status: Accepted (2026-11-04), fixed before any shadow data exists. Do not change a threshold based on results; a changed prompt or pass mark needs a new `prompt_version` and starts from zero.
- Code: `services/debate_worker/shadow_debate.py`, `services/debate_worker/shadow_eval.py`, `scripts/shadow_prompt_report.py`, migration 065

## Context

All 21 scanner debates since 2026-11-02 said HOLD, 20 citing mixed or conflicting signals, so the lite debate filters nothing: right on the earlier labelled setups (-1.04R), wrong on the 2026-11-04 rally. Prompt `shadow-v1` removes "Prefer HOLD when the data is mixed, stale or thin" and asks whether this setup, with its stop and target, has positive expected value. It answers the same setups beside the live prompt, and it must earn trust on labels it has not seen.

## Isolation

Shadow output reaches nothing live: not `route_entry`, `setup_proposals`, `policy_decisions`, `portfolio_decisions`, `debate_runs`, `debate_turns`, the websocket channels, alerts, the funnel stages or the Brain. `cryptonite_agent_ro`, the role of the models' SQL tool, has no grant on `shadow_debate_verdicts`. A failure, timeout, budget stop or missing model never affects the debate. The evaluation only prints a verdict: **going live is a later operator decision and a separate change.**

## The pass mark

"TAKE" is a shadow BUY or SELL in the setup's direction; anything else is SKIP. The label is `candidate_outcomes.net_r_live`. Rows count when the state is OK, `prompt_version` matches, the label is present and the scan was not `HISTORICAL_PRIMING`. A regime is the sign of that coin's 24h close change at the signal, from lake 1h bars at report time.

All must hold for PASS:

| Condition | Value |
|---|---|
| Labelled rows, on distinct UTC days | at least 100, at least 21 days |
| Labelled rows in each regime | at least 30 |
| TAKEs in each regime | at least 10 |
| TAKE rate | 10% to 70% |
| Mean net R of TAKE | above 0 |
| Mean TAKE minus mean SKIP, overall and in each regime | at least 0.20R |
| 90% lower bound of that difference | above 0 |

The bound is a seeded bootstrap (5000 resamples) of whole UTC days, because setups overlap and rows are not independent. The live policy's figure is reported beside the verdict for reference and is no part of it.

Any failed condition gives FAIL only when the size conditions are met; before that the result is NOT_ENOUGH_DATA, listing what is missing.

## Readings where the wording left a choice

Fixed here so that they cannot be chosen after seeing results:

- A TAKE rate outside the band fails once the row counts are met, without waiting for the 10 TAKEs per regime. Otherwise a filter that never says yes could never fail. In-band with too few TAKEs in a regime stays NOT_ENOUGH_DATA.
- "21 calendar days" means 21 distinct UTC signal days with a usable row, not a span.
- "90% lower bound" is one-sided: the 10th percentile of the resampled differences.
- A flat or unmeasurable 24h change has no regime. Such a row counts overall and in no regime.
- A BUY or SELL against the setup's direction is a SKIP.

The report can be run at any time, but repeated looks raise the chance of a lucky PASS. Treat a PASS as the start of the operator's decision, not as its end.

## Amendment, 2026-11-06: prompt `shadow-v2`

`shadow-v2` answers the same setups beside `shadow-v1`, in the same isolation and under the same daily spend cap. It asks for the strongest supported case on each side, says mixed signals alone do not decide, returns HOLD when essential evidence is missing, and treats `conviction_score` as confidence rather than a win probability. Its text is frozen and it counts from zero under the unchanged pass mark above. `shadow-v1` keeps its own count. The table's key is now `(candidate_id, prompt_version)`. The report prints one verdict per version, so reading two versions adds another chance of a lucky PASS. The live lite prompt does not change.
