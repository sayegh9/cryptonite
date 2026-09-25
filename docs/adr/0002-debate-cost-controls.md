# ADR 0002: Debate cost controls (Phase 2)

- Status: Accepted (2026-09-25)
- Context: [AI-first blueprint](../ai-first-blueprint.md) §6 and Phase 2 (A2.1–A2.6)

## Decisions

1. **Pre-debate gate** (`services/debate_worker/gate.py`). Scanner-triggered debates pass, in order: kill switch / daily-loss latch, position headroom under the 25% cap, candidate freshness (> max(15 min, 2 bars) is stale), a market brief (none: skipped), a 30-minute cooldown per instrument+timeframe (released if triage is unavailable or the debate fails), then **triage**. Any failure costs $0, except triage, which costs about $0.00004. Operator-triggered debates skip the gate, because that spend is an explicit choice. Skip reasons are counted in Redis (`debate_gate:<date>:<reason>`).
2. **Market brief** (`cryptonite_scanner.brief`). The scanner builds one deterministic brief (under 2k tokens) per candidate from the indicator frames it already computes, plus the perp mark, funding and OI. It is cached at `brief:<instrument>:<tf>:<bar>` and passed through the debate queue. Every debate role reads the brief instead of pulling raw rows.
3. **Triage on Flash-Lite, thinking off.** The criteria are explicit thresholds (reward-to-risk < 1.5, stale data, both higher timeframes against the trade, low-volume chop). Staleness is enforced in code by the gate. The model does not do that timestamp arithmetic reliably.
4. **Stakes-sized debates.** Scanner candidates get one `debate.lite` call (bull case, bear case and decision in one structured output). **Deviation from the blueprint:** the blueprint also sent *high-conviction* verdicts to the lite path. Here, any lite verdict with allocation ≥ 5% escalates to the full fleet, whatever its conviction, so big positions always get the full review. A small lite verdict (< 5%) stands only with conviction ≥ 0.35; weaker ones are recorded as `NO_TRADE`. HOLD verdicts are recorded as `NO_TRADE`, because there is nothing to approve.
5. **Merged risk panel** (default `DEBATE_RISK_PANEL_MODE=merged`; `full` reverts). One Pro call replaces the three Flash risk managers plus the Pro CRO.
6. **Thinking and output limits per role.** Thinking is billed as output: side roles get 512 thinking tokens, the lite debate gets 1024, verdicts get 2048, triage and summarize get 0.
7. **Deterministic exits** (`exit_monitor.py`), priced from the live quote stream the orchestrator fills against (a position without a quote fresher than 10 s is not evaluated):
   - hard stop at −5%;
   - trailing stop of 2% once a position is +3% up.
   A triggered stop auto-closes only instruments whose open rows, on both sides, are all AI-owned (CLOSE_POSITION flattens the whole instrument); shared instruments only get an alert. A rejected close is retried after 60 s; alerts are debounced hourly. **Model-proposed exit stops are recommendations only** and never auto-close: the exit debate's output is staged for the operator. Exit debates run only on events: the first −2% crossing per drawdown episode (re-armed after recovering to −1%), or a funding spike against the position. They no longer run just because a position is 24 h old.

## Evidence (live runs stored in `ai_eval_runs`, plus a replay test)

| Control | Result |
|---|---|
| Triage, Flash-Lite, 20 cases ×2 | 100% valid. Good setups missed: 0 of 4. Bad setups avoided: 5 of 6 by the model, 6 of 6 with the code staleness check. $0.0012 per 20 calls |
| Lite debate, Flash, 3 cases ×2 | 6/6 valid and agreeing; $0.019 |
| Merged vs 4-call risk panel, 4 scenarios | both 4/4 within expected sizing; allocations within 2.5 pts of each other; merged **27% cheaper, 22% faster** |
| Exit rules, replay of 200 three-day paths | no missed hard stop. Exit debates: **233 vs 1,213** (−81%) at ~3%/day volatility; 199 vs 272 (−27%) at ~5.7%/day |
| Call count per scanner candidate (tests) | triage rejection: **1 cheap call** (was 12); low-stakes lite: **2 calls** (was 12); escalation: 2 + 8 with the merged panel |

Caveats: the eval sets are small and hand-written; volatility and the scanner's real candidate mix decide the true savings. The cost ledger will show the real daily spend once the stack runs Phase 2 (Gate 2: typical day ≤ $6 over 7 days).

## Review fixes (independent review, 2026-09-25)

The first review failed the change for these reasons, all now fixed:
- low-conviction large allocations skipped the full review;
- stale or rejected model stops could auto-close later positions;
- stops read `paper_positions.current_price`, which only updates on fills, so they would never fire;
- ownership was checked per side while the close flattens both sides;
- the best-price key was reused across positions;
- closes were never retried and alerts spammed;
- the cooldown was burned on failures;
- debates ran without a brief;
- exit runs were stuck in RUNNING.

It also found that `cryptonite_api`, the debate worker's role, could not read `agent_safe_positions`, so every real debate would have failed at its context lookup. Migration 021 now grants it; databases already at 021 need `GRANT SELECT ON agent_safe_positions TO cryptonite_api` once, which has been applied to the dev DB.

## Not done

- **Explicit context caching.** Gemini 2.5 caches identical prompt prefixes implicitly, and the ledger records `cached_input_tokens`. Prompts were not restructured around a shared cached prefix, and `google_cached_content` is not used. Revisit once the ledger shows the cached share.
- **Mandate-budget check in the gate.** Mandates arrive in Phase 5.
