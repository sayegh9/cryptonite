# Remediation Plan (2026-10-04, reviewed)

Source: the critique of 2026-10-04 (data honesty, backtest accuracy, AI decision accuracy, gates,
indicators, UI/UX, operations, features). Each item lists the problem, the change, the files, the tests
and a done-when line. A second pass checked every item against the code; its corrections are in §0 and
are already applied to the phases below.

The three rules stay: **AI and UI propose, deterministic rules and risk controls decide, NautilusTrader
executes.** No item touches `GuardedOrderDispatcher`, the 1–25% clamp, the mandate check, the kill
switch, TOTP approvals or the outbox.

## 0. Review of the critique and the first plan

### Corrections

| # | First claim | What the code shows | Effect on the plan |
|---|---|---|---|
| C1 | The approval screen shows a random price | `ExecutionStagingCanvas` is only used by `DebateDashboard`, which nothing imports: **dead code**. The live approval surfaces are `ActionInbox` (debates outside a mandate), Mission Control `ApprovalQueue` (mandates), Brain order cards and Telegram | R1.2 becomes "delete the dead components"; new R1.2b fixes `ActionInbox`, which sizes `targetAllocationUsd` from equity that defaults to 100,000 when its fetch fails (the dispatcher still clamps to 25% of real equity, so this is a display/sizing error, not a bypass) |
| C2 | Gate 5 and L2 are judged on HOLD-as-SELL labels and understated backtest drawdowns | The L1→L2 gate reads `ai_mandate_equity` (live paper equity), closed trades, breaches and Sortino vs BTC hold. It uses **neither** outcome labels **nor** backtest metrics. Labels feed only L3 calibration and reflections | Phases 2 and 3 do not change the L2 picture; Phase 4 does (it adds checks) |
| C3 | Past decisions need relabelling in the DB | `ai_decision_outcomes` stores the unsigned `return_pct`; the sign is applied at read time | No DB relabel, and (after N16) no reflection change either |
| C4 | Triage asks an LLM for 4 rules | The stale rule is already deterministic in `gate.py` (`STALE_CANDIDATE`). Exact thresholds for the other three already exist in `scripts/build_triage_eval_set.py::label()`. The prompt says chop is "volume below 1.0x" while the eval labels use `<= 0.8x` inside the bands | R3.1 reuses `label()`'s thresholds; prompt and rules are made consistent |
| C5 | — | Changing the `TRIAGE` prompt invalidates the certified A7.4 student (trained on the current prompt) and the `triage_v3` baselines | R3.1 keeps the prompt unchanged; rules only short-circuit before the call |
| C6 | Remove the On-Chain Analyst | It runs only in the full debate (the lite path has no analysts). The same false premise is in the `ONCHAIN_CHAT` copilot persona, which has an endpoint, an auth test and a UI tab | R3.2 removes the debate analyst but keeps the chat endpoint, rewriting its prompt to state what data it really has |

### Missed in the critique

| # | Gap | Added as |
|---|---|---|
| M1 | Three separate indicator implementations: scanner (Polars), strategies (Nautilus) and the chart (`technicalindicators` npm). Values can disagree on screen | R7.2 parity test |
| M2 | No database backups. The dev DB holds operator TOTP enrollment, the AI ledger, mandates, paper history | R7.9 (do early: it is cheap and protects everything else) |
| M3 | After R2.2, runs with old and new metrics would be compared side by side (`MultiRunComparator`, Brain tools `read_nautilus_tearsheet`, `compare_live_vs_backtest`) | R2.4 extended |
| M4 | `Math.random()` is also used legitimately for ids; a blanket ESLint ban would break the build | R1.5: switch ids to `crypto.randomUUID()` first |
| M5 | Unused components besides the staging canvas: `DebateDashboard`, `ConvictionBadge` (0 references) | R1.2 |

### Second review (same day): found in the code

| # | Finding | Severity | Added as |
|---|---|---|---|
| N1 | **The ActionInbox sizing slider has no effect.** `ActionInbox` sends `{targetAllocationUsd}`; the server's `ApproveRequestOverrides` only knows `allocation_pct` and Pydantic drops unknown fields. The order is sized from the AI's `allocation_pct` whatever the operator chose. Still clamped (1–25%, mandate headroom, dispatcher), so not a bypass, but the operator approves a size that is not used | High (correctness) | R1.2c |
| N2 | **Fake success on approve/reject.** `useDebateStore.approveDebate` / `rejectDebate` mark a run COMPLETED locally, with no server call, when the run id starts with `demo-`/`sim-` **or when no CSRF token is stored** (e.g. an expired session) | High | R1.2d |
| N3 | **Fake debate injected on every trigger.** `AIBrainWorkspace.handleRunDemoDebate` adds a `demo-` PENDING_APPROVAL debate ("OVERWEIGHT 84% conviction, $25,000 max") to the inbox, then also triggers a real one | High | R1.2d |
| N4 | `e2e/ai_brain.spec.ts` (step 8) depends on N2/N3: it approves the injected demo debate and expects COMPLETED. Phase 1 breaks it unless the spec is rewritten to stub the debates API | Conflict | R1.7 |
| N5 | The gate check `honest_paper` is hardcoded `True` (`autonomy_metrics.py:66`, default in `autonomy.py:101`), so it always passes | Medium | R4.6 |
| N6 | If the TOTP AES key is missing, `get_totp_aes_key()` logs a warning and encrypts with 32 zero bytes (`auth.py:32,60`). A restore or deploy without `.env` silently weakens TOTP storage | Medium (security) | R7.10 |
| N7 | Backups of the DB are useless for TOTP without the AES key, and the Binance demo PEM and `.env.notifier` are needed to rebuild | Medium | R7.9 extended |
| N8 | Brief timeframes come from the scanner universe (default `1m, 5m, 15m, 1h`); the prompt says "the two highest timeframes shown" while the eval labels use 15m and 1h. Same thing for the default universe, different for a universe with 4h | Low | R3.1 wording |

### Third review (same day): found in the code

| # | Finding | Severity | Added as |
|---|---|---|---|
| N9 | **Risk-limit edits do nothing.** `POST /control/risk-limits` (`routes/control.py:230`) verifies the step-up password, writes an audit row and returns success, but stores and enforces nothing. The Operator panel `RiskLimitsEditor` starts from hardcoded values (5% / $10,000 / $50,000), never loads the real limits, targets `DEMO-ACCOUNT-001`, and shows "Risk limits updated successfully". The real limits (5% daily drawdown, 1–25% clamp, mandate limits) are unaffected, so nothing is weakened; the operator is misinformed | High | R1.8 now; F7 later |
| N10 | The AI Brain tour's "Try a Demo Debate" button (`AIBrainTourModal` → `onRunDemo`) uses the same `handleRunDemoDebate` that R1.2d deletes | Conflict | R1.2d extended |
| N11 | The plan said the backtest runner already holds the evaluation bars. It does not: it reads parquet timestamps, not bar objects (`runner.py:196-215`). R2.2 needs one extra read of the bar closes from the catalog for the evaluation window | Plan correction | R2.2 wording |

### Fourth and fifth reviews (same day): backend, migrations, tests, API paths, data

| # | Finding | Severity | Added as |
|---|---|---|---|
| N12 | **`ActionInbox` always uses $100,000.** It fetches `/api/v1/portfolio/summary`, which does not exist (there is no portfolio router), so `totalEquity` never leaves its 100,000 default. A cross-check of every `/api/v1` and `/agent-api` path the web calls against the API routes found no other missing route | High | R1.2b/R1.2c: equity from `usePortfolioStore` (`/api/v1/paper/accounts`, the same `paper_accounts.total_equity` the server sizes from) |
| N13 | Research windows already need ~130 days (10 warm-up + 90 IS + 30 OOS) against ~120 days of backfilled history; a 30-day holdout (R5.3) makes it ~160 | Medium (blocks Phase 5) | R5.0 |
| N14 | Rule triage (R3.1) has no structured input: the gate only receives the brief **text** from Redis and a candidate payload that may lack entry/stop/target (as in `test_debate_gate.py`) | Design gap | R3.1 data source |
| N15 | R4.3's 100-decision minimum would fail `test_ai_calibration.py` (built on 20) and tighten the L3 gate | Policy | R4.3 under D6 |
| N16 | Changing `signed_outcome` (R3.3 as first written) would change reflection texts and `test_ai_outcomes.py` for no benefit: the continuous "outcome" of a HOLD (−return = gain missed or loss avoided) is meaningful. The defect is only the discrete right/wrong scoring in calibration | Plan simplification | R3.3 narrowed; R3.4 and D2 dropped |
| N17 | `cryptonite_api` has INSERT but not UPDATE on `execution_commands` | Constraint | R7.4: trace id written at insert only |

| N18 | The enforced limits are constructor defaults inside the orchestrator's `GuardedOrderDispatcher` (`risk.py:88-115`, built in `engine.py:72`) and `ACCOUNT_BACKSTOP_DRAWDOWN_PCT` (`schemas/mandate.py:20`). The API process cannot see what the orchestrator actually runs with, so a card that hardcodes "5% / 25% / 20%" would repeat N9's mistake | Design gap | R1.8 data source |
| N20 | The advertised hold-to-approve control is not in the live app (only in the dead staging canvas) | Doc/plan correction | R6.6, R7.7 |
| N19 | The `sentiment` service has no Redis credentials (internal network, no secrets) and publishes no heartbeat; R6.3/R7.5 listed it as a heartbeat | Design gap | R6.3, R7.5 |

Verified safe in these rounds: every write endpoint really writes or delegates (kill switch through
a SECURITY DEFINER function, mandates through `_transition`, onboarding/backfill as background tasks);
mandate equity is marked at the live bid with commissions (so the L2 drawdown is sound); the paper
engine's 10 bps taker fee equals the `conservative` profile (R2.1 aligns backtests with paper); grants
on `execution_commands`, `reflections`, `research_reports` are table-level, so new nullable columns need
no new grants; the only `SELECT *` on an altered table (`paper.py:180`) returns dicts and tolerates new
columns; all backtest tests pass the profile explicitly; the e2e watchlist, profile-switch and
AI Brain banner steps survive Phase 1–2; `test_strategy_reproducibility`'s "supertrend" is the
strategy, not `calc_supertrend`.

Also checked, fine: Settings → agent configs are stored and read by the agent worker; the kill switches
in Terminal, Mission Control and Operator target PAPER (plus a separate DEMO one on Operator), matching
where mandates trade; Brain order cards are approved server-side; Market overview, Alerts and Coin
profile read the API.

Checked and fine: no other file references the `apps/web` junk scripts; the agent tools never start
backtests with the default profile; the exit monitor does not use the scanner indicators (R7.2 does
not change stops); demotion is driven by `mandate_monitor`, not the gate; `test_identical_run_golden`
passes the profile explicitly.

### Safety rules for carrying out this plan

| # | Rule | Why |
|---|---|---|
| S1 | **Do not change the live decision path during an evaluation window without an operator decision (D5).** Live-path items: R3.1, R3.2, R3.5, R7.2 (scanner values change which candidates fire), R7.3 (paper fills). | Gate 5 (to ~10-15) and the L2 window (to ~10-31) evaluate one fixed process. Changing triage, the debate or the fill model mid-window mixes two processes in one score and makes `honest_paper` (A0.4) debatable |
| S2 | Safe to deploy at any time: Phase 1 (UI only), Phase 2 (backtester only), Phase 4 **as reporting** (intervals shown, not yet pass/fail), Phase 6, R7.4–R7.9 | They do not change what is traded or how fills are simulated |
| S3 | Gate thresholds are policy: changes go into ADR 0004 and take effect only after the operator approves them (D6). Demotion is unaffected (driven by `mandate_monitor` drawdowns, not by the gate) | Prevents silently moving the goalposts |
| S4 | Alembic is linear and head is `031`. Phases that add migrations (R5.3, R7.4) take the next number **when merged**, not when branched; rebase and renumber if two are open | Avoids two `032` heads |
| S5 | Every PR: pytest on the throwaway DB (never the dev DB), Playwright with the API container stopped, `pnpm typecheck && pnpm lint`, `ruff check`; tests never call paid models | Existing project rules |
| S6 | After any change to the debate or triage, re-run evals `triage_v3`, `triage_lake_v1`, `summarize_v1` before deploying | Catches regressions in the routed models |

### Accepted, not planned

| Item from the critique | Why it is not a work item |
|---|---|
| Single host | Paper trading only; the stack has restart policies; backups (R7.9) cover loss. Revisit before any live venue |
| Protected roles (`debate.verdict`, `debate.cro`, `exit.verdict`) depend on Gemini | They fail closed by design (no trade without a verdict); exits keep running through stops and the exit monitor. Correct behaviour, not a defect |
| `tools.py` low cohesion | Refactor with no user-visible gain; do it only together with a feature that touches it |

### Review status

Eight review passes on 2026-10-04 covered: every web view and store; every `/api/v1` and `/agent-api` path the web calls; every API write endpoint; the approval, gate, mandate-equity, calibration and outcome code; the migrations' grants on altered tables; and every test that a plan item touches. Passes 7 and 8 found no new defect, only plan wording (N20). What a code review cannot prove is caught by S5/S6: each PR runs the full pytest suite on a throwaway DB, Playwright, and the model evals before deploy.

## Calendar

| Date | Event |
|---|---|
| ~2026-10-15 | Gate 5 earliest (14 d L1 paper, 0 breaches, working approvals) |
| ~2026-10-31 | L2 candidacy of `BTC-ETH spot L1` |

## Decisions needed from the operator

| # | Decision | Recommendation |
|---|---|---|
| D1 | On-Chain Analyst in the debate | Remove now; re-add only with a real data source (F6) |
| D2 | — | Dropped after review (N16) |
| D3 | Old backtest rows | Badge as legacy; re-run only the runs still in use |
| D4 | The 3 BREACH events of 2026-10-04 | Operator's call (open in HANDOFF) |
| D5 | Live-path items (S1) | Hold them until after the L2 review (~10-31), or deploy now and restart the evaluation clocks. Recommended: hold, except R3.2, whose only effect is removing invented input |
| D6 | New gate checks (Phase 4) | Show now as information; make them required from the L2→L3 gate onward, and for L1→L2 only for mandates whose window starts after approval |

## Branches

`fix/r1-data-honesty`, `fix/r2-backtest-accuracy`, `fix/r3-decision-accuracy`,
`feat/r4-gate-statistics`, `feat/r5-research-validation`, `feat/r6-ux`, `chore/r7-ops`, then one
branch per Phase 8 feature. All into `main`; merges stay with the operator.

---

## Phase 1: data honesty in the UI (~1 day) — safe now (S2)

Rule: **unknown is shown as unknown.** No component renders a made-up value as data.

| ID | Problem | Change | Files |
|---|---|---|---|
| R1.1 | The AI Brain → Debates canvas invents a full analysis (fake RSI, OI, "CRO approved") when an agent call fails | Delete `generateDynamicSynthesis`; on failure append a system-styled error ("agent unavailable: <status>") with Retry; never give non-model text the `assistant` role | `components/brain/OrchestratorCanvas.tsx` (~237–330, 518–526) |
| R1.2 | Dead components with fake data | Delete `debate/DebateDashboard.tsx`, `debate/ExecutionStagingCanvas.tsx`, `debate/ConvictionBadge.tsx`; then delete `SegmentedConvictionIndicator` / `DebateTranscriptViewer` only if their remaining references were the deleted files | `components/debate/` |
| R1.2b | `ActionInbox` always sizes from 100,000: its equity endpoint does not exist (N12) | Read `total_equity` of `PAPER-ACCOUNT-001` from `usePortfolioStore` (`/api/v1/paper/accounts`); `null` while unknown → slider and Approve disabled, "equity unavailable" shown | `components/brain/ActionInbox.tsx:44,97,184,467,580` |
| R1.2c | The ActionInbox slider is ignored by the server (N1) | Send `allocation_pct = size / equity × 100` (known equity only, rounded, 1–25) instead of `targetAllocationUsd`; show "final size may be reduced to the mandate's room". Server unchanged (already validates 1–25). Test: approve with the slider at 2% → command payload carries 2% | `ActionInbox.tsx:184-194` |
| R1.2d | Local fake success and injected demo debate (N2, N3) | `approveDebate`/`rejectDebate`: no CSRF → error "session expired, sign in again", never a local COMPLETED; remove the `demo-`/`sim-` branches. Delete `handleRunDemoDebate`; the button only calls `/agent-api/debates/trigger` and shows "debate queued". Keep the `demo-` cleanup in `ActionInbox` (removes leftovers in browsers that persisted them). The tour's "Try a Demo Debate" (N10) becomes "Run a real debate on <symbol>" (same trigger call; the tour already says it is paper) or is removed; drop the `onRunDemo` prop accordingly | `lib/stores/useDebateStore.ts:97-110,166`, `AIBrainWorkspace.tsx:75-135,280`, `AIBrainTourModal.tsx:11-42,208` |
| R1.3 | Strategy manifest card sizes from `totalEquity = 100000` | Read equity from `usePortfolioStore`; disable the slider while unknown | `OrchestratorCanvas.tsx:130` |
| R1.4 | Watchlist shows hardcoded prices until a fetch succeeds | Seed symbols only (`price: null`), render "—" + stale badge, show fetch errors | `components/WatchlistPanel.tsx:22-60` |
| R1.5 | Remaining fakes | Sweep `Math.random\|mock\|Mock\|fallback\|100000` in `src`; fix each hit that renders as data; move ids to `crypto.randomUUID()` (`TradingViewChart.tsx:795`, `ScannerStudio.tsx:87`); then add an ESLint `no-restricted-properties` rule for `Math.random` | `src/**`, `eslint.config.mjs` |
| R1.6 | No shared freshness concept | `lib/freshness.ts` + `<Fresh value asOf maxAgeMs>` (live / delayed / stale / unknown), used first by R1.2b–R1.4 | new files |

| R1.8 | Risk-limit form reports success but changes nothing (N9) | Replace the form with a read-only "Enforced limits" card. The values must come from the process that enforces them, not from UI constants (N18): the orchestrator publishes its dispatcher's effective settings (`max_daily_loss_pct`, the 1–25% clamp, `taker_fee`) and `ACCOUNT_BACKSTOP_DRAWDOWN_PCT` to Redis `risk:effective_limits` at start-up and every minute; a new read-only `GET /api/v1/control/enforced-limits` returns them (or `unknown` when absent); per-mandate limits come from `/api/v1/ai/mandates`. The endpoint returns `501 Not Implemented` with "limits are not configurable here; edit the mandate" instead of a fake success, keeping its auth and CSRF dependencies so `test_route_auth_guards` still sees 401/403 for anonymous calls. Update step 6 of `test_operator_auth_and_session.py` (lines ~268-279), which today asserts 200, to expect 501. No limit changes, so safe now. Configurable limits become F7 | `operator/RiskLimitsEditor.tsx`, `routes/control.py:230-270`, `tests/test_operator_auth_and_session.py` |
| R1.7 | `e2e/ai_brain.spec.ts` step 8 relies on the injected demo debate (N4) | Rewrite the step: stub `GET /api/v1/agents/debates` to return one PENDING_APPROVAL run, stub approve, assert the approve request body has `allocation_pct` and the UI shows the server's status. Step 7 already stubs `/agent-api/chat` and is unaffected | `e2e/ai_brain.spec.ts:126-139` |

**Tests:** Playwright with the agent and portfolio routes stubbed to 500: no "RSI", "approved" or price
text, Approve disabled. Approve without CSRF shows an error, not COMPLETED. Unit tests for `<Fresh>`.
All 26 existing specs pass after R1.7.
**Done when:** with the API stopped, every workspace shows errors or "—", never numbers.

---

## Phase 2: backtest accuracy (~1.5 days) — safe now (S2)

| ID | Problem | Change | Files |
|---|---|---|---|
| R2.1 | Default profile `optimistic` (0 fees, 0 slippage) | Default `conservative` in `BacktestRunRequest`, the API model and Research Lab; label `optimistic` "no costs – not for decisions". Golden tests pass the profile explicitly, so they are unaffected; the research loop already uses `conservative` | `runner.py:96`, `services/api/.../routes/backtests.py:35`, `ResearchLab.tsx:35` |
| R2.2 | Equity changes only at fills (no mark-to-market; max DD understated, Sharpe overstated; days without fills skipped) | `marked_equity_series(...)`: holdings from balance rows (constant between fills), valued at every bar close of the evaluation window. The runner reads only timestamps today (N11), so add one read of `(ts, close)` for the bar type and window through `NautilusCatalogManager`; max DD from that series; Sharpe/Sortino from a full UTC daily grid. Write `metrics_version: 2` into metrics and artifacts, **not** into `run_config_hash` (the hash describes inputs) | `packages/backtester/.../metrics.py`, `runner.py:384-415` |
| R2.3 | No benchmark in backtests | `benchmark` block: buy-and-hold return, Sharpe, max DD, `alpha_vs_hold`; plus `fees_paid`, `turnover` | `metrics.py`, `TearsheetCards.tsx`, `MultiRunComparator.tsx` |
| R2.4 | Mixed metric versions | "legacy metrics" badge when `metrics_version` is missing; the comparator shows a warning (does not block) when runs of different versions are compared; the e2e fixtures in `research_lab.spec.ts` gain `metrics_version: 2`; Brain tools include `metrics_version` in their output; `scripts/rerun_backtests.py --run-id/--since` for D3 | UI, `packages/agents/.../tools.py`, `scripts/` |

**Tests:** one long trade that falls 15% (bar closes) and recovers → max DD ≈ 15%; fills 5 days apart →
5 daily returns; conservative 100-trade run → `fees_paid ≈ Σ notional × 10 bps`; `test_metrics_edge_cases`,
`test_identical_run_golden`, `test_full_stack_backtest_e2e` updated where they assert exact metric values.
**Done when:** the re-run `ema-cross` shows drawdown inside open trades and a benchmark row.

---

## Phase 3: AI decision accuracy (~1.5 days) — live path, timing per D5

| ID | Problem | Change | Files |
|---|---|---|---|
| R3.1 | Triage spends a model call on rules code can decide | Move `label()`'s rule part into `cryptonite_scanner.brief` as `hard_rejects(brief_data)`: R:R < 1.5, the two highest timeframes in the brief both trending down (spot is long-only), chop (candidate-timeframe volume ≤ 0.8x inside the bands); with the default universe that is exactly the eval's 15m/1h rule (N8). `build_brief` gains a structured twin (`brief_data()`); **the text it renders is unchanged**. The scanner stores it next to the text (`<brief_key>:data`, same TTL). In `gate.py`, after the cooldown check: if the data exists and every input a rule needs is present, any hit → `GateDecision(False, "TRIAGE_RULE_<NAME>")`, no model call, recorded like other gate rejections. **Missing data never rejects**: it falls through to the model exactly as today, so existing `test_debate_gate.py` cases (no entry/stop/target) and a debate worker deployed before the scanner both behave as now (N14). The `TRIAGE` prompt stays as is (C5), except the chop wording is aligned to 0.8x after re-running evals | `packages/scanner/.../brief.py`, `services/debate_worker/gate.py:96-108`, `scripts/build_triage_eval_set.py` (import the shared function) |
| R3.2 | The debate's On-Chain Analyst and the On-Chain chat persona claim data they do not have | Debate: analysts become `(MARKET_ANALYST, SENTIMENT_ANALYST)` (`main.py:231`). Chat: keep `/agents/onchain` (UI tab, `test_route_auth_guards`), rewrite `ONCHAIN_CHAT` to say it has no on-chain feed and answers from derivatives (funding, OI) and market data only | `services/debate_worker/main.py:231`, `debate_fleet.py:61,153` |
| R3.3 | Calibration counts a ±0.05% move as right or wrong, and scores HOLD exactly like SELL | Narrowed (N16): `score_decision(rec, return_pct, band=0.002) -> WIN/LOSS/NEUTRAL` replaces only `calibration._right`: BUY wins above +band and loses below −band; SELL (exit) mirrored; HOLD wins at ≤ +band, loses above it (a missed rise). NEUTRAL points are excluded from the reliability bins. `outcomes.signed_outcome` (continuous, used by reflections) is **unchanged**, so reflections, recall and `test_ai_outcomes.py` are untouched. `test_ai_calibration.py` (±2% moves) is unaffected by the band | `packages/execution/.../calibration.py:41-56` |
| R3.4 | — | Dropped (N16): no reflection migration, no D2 | — |
| R3.5 | Bull/Bear told to ignore the other side | Each names the strongest counter-argument it rejects and why; the PM cites which survived | `debate_fleet.py` BULL/BEAR/`research_manager` |

**Evals (S6):** `triage_v3` through `hard_rejects` + model: rule cases 100%, residual measured
separately (`triage_residual_agreement`); full-debate eval case with no on-chain data asserts no
persona mentions whale or exchange flows. **Tests:** `score_decision` table (BUY/SELL/HOLD × up/down/flat);
calibration excludes NEUTRAL; gate tests for each `TRIAGE_RULE_*` and that the model is not called.
**Done when:** a week of logs shows rule rejections with no model call, and 20 spot-checked rejections are correct.

---

## Phase 4: statistically honest gates (~1.5 days) — reporting safe now; enforcement per D6

| ID | Change | Files |
|---|---|---|
| R4.1 | `stats.py`: Wilson interval (win rate); stationary bootstrap CI (block ≈ √n, fixed seed) for mean trade return after fees and for Sortino minus BTC-hold Sortino | new `packages/execution/cryptonite_execution/stats.py` |
| R4.2 | Gate report shows value + 90% interval for expectancy and Sortino difference. New checks `expectancy_lower_bound > 0` and `sortino_diff_lower_bound > 0` are **informational** until D6 | `autonomy.py:174`, `autonomy_metrics.py`, `scripts/gate_status.py` |
| R4.3 | L3 calibration: show the ECE bootstrap CI and 5 equal-count bins now (informational); raising the minimum from 20 to 100 labelled decisions is a **policy change under D6** and comes with an updated `test_ai_calibration.py` (N15) | `calibration.py`, `autonomy.py` |
| R4.4 | Mission Control → Autonomy & Evals shows intervals and marks informational checks | `components/mission/AutonomyEvals.tsx` |
| R4.6 | `honest_paper` always `True` (N5): derive it (paper fills come from the version-locked matching engine with fees on, no manual fills inside the mandate's instance during the window) or show it as "not measured". Do not turn it into a failing check before D6 | `autonomy_metrics.py:66`, `autonomy.py:101` |
| R4.7 | Informational checks must not block: add `GateCheck.informational`; `GateReport.passed` ignores informational checks; the orchestrator's promotion proposal is therefore unchanged until D6 | `autonomy.py:33-48` |
| R4.5 | ADR 0004 amendment describing the new checks and when they become required | `docs/adr/0004-mandates-and-autonomy.md` |

**Tests:** fair-coin trades (n=30) fail the expectancy bound in ≥ 90% of seeds; +0.5%/trade (n=200)
passes; bootstrap deterministic with a fixed seed; existing autonomy tests unchanged while checks are informational.

---

## Phase 5: research loop validation (~2 days) — safe (research only proposes)

| ID | Change | Files |
|---|---|---|
| R5.0 | History for the windows (N13): extend the BTC/ETH 1m and 1h backfill to ≥ 180 days with `scripts/backfill_history.py` (Binance public klines, roughly 50% more than LE-1's 15–30 MB; operator approves the download), verify 100% completeness, and make the loop refuse a spec whose warm-up + IS + OOS + holdout exceeds the available history with a clear error instead of a `DataGapError` mid-run | `scripts/`, `research/queue.py:35`, `loop.py` |
| R5.1 | Walk-forward: 4 anchored folds inside in-sample; Optuna objective = mean fold Sharpe − 0.5·std; `MIN_TRADES` 30 per full window | `research/loop.py` |
| R5.2 | Deflated Sharpe Ratio from `n_trials` and trial-Sharpe variance; reject below 0.95 | `loop.py`, `stats.py` |
| R5.3 | Final 30-day holdout never seen by the loop; scored once, for PROMOTE candidates only | `loop.py`; migration (next number, S4): `research_reports.holdout jsonb`, `dsr numeric`; API model in `routes/research.py` |
| R5.4 | Count ideas tested per instrument/window and feed the total into DSR | `research_reports` query |
| R5.5 | Robustness: best params ±10% each; flag if Sharpe falls > 50% | `loop.py` |

**Tests:** a random-entry strategy with 50 trials is rejected by DSR; existing `test_research_loop` and
`test_research_reports` updated for the new fields. Runs only with `--profile research`.

---

## Phase 6: UI/UX (~3–4 days) — safe now (S2)

| ID | Change | Files |
|---|---|---|
| R6.1 | URL state `?ws=…&view=…` synced both ways with `workspaceMode`; back/forward work; `cryptonite:switch-workspace` pushes history. `page.tsx` is a client component: wrap `useSearchParams` in `<Suspense>` so `pnpm build` passes. `/operator` route unchanged | `app/page.tsx:44`, `useBrainStore` |
| R6.2 | Navigation groups: Trade (Terminal, Markets, Portfolio), Research (Lab, Scanner), AI (Brain, Mission Control), System (Operator, Alerts, Settings); command palette lists every view. Keep the existing tab labels as the visible text (e2e specs click them by name) or update the specs in the same PR | `page.tsx`, `CommandPalette.tsx`, `e2e/*` |
| R6.3 | Mission Control as landing: pending approvals, mandate health, kill switch, heartbeats (exit monitor, learner, local tier, sentiment), today's AI spend vs budget | `components/mission/*`, `/api/v1/ai/status`; sentiment has no Redis access by design (no secrets), so its health is the debate worker's last successful `get_news_sentiment` call, stored as `sentiment:last_ok` (N19) |
| R6.4 | Approval surfaces (`ActionInbox`, `ApprovalQueue`): live quote and slippage vs decision price, mandate room left, worst-case loss at stop, "may be resized to N%" note, 3 most recent past decisions on the same instrument and side with their outcomes (a plain SQL query: no model or embedding call in the approval path, so it cannot slow or fail an approval) | those components; approvals API adds `similar_decisions` (read-only, failure → omitted) |
| R6.5 | Readability: 11px floor for data, `tabular-nums`, contrast ≥ 4.5:1; replace the 332 `text-[8-10px]` uses via tokens | `globals.css`, components |
| R6.6 | Accessibility: every approve/reject control reachable and operable by keyboard with a confirm step (the README's "2-second hold-to-approve" exists only in the dead `ExecutionStagingCanvas`, N20; README corrected in R7.7), focus trap + Esc on the floating Copilot, labels on icon buttons; `@axe-core/playwright` in e2e | components, `e2e/` |
| R6.7 | Split `OrchestratorCanvas` (861 lines) and `TradingViewChart` (893) — last in the phase, behind the new tests | `components/brain/`, `TradingViewChart.tsx` |

**Done when:** axe shows 0 serious issues on main views; every view has a URL; Playwright covers R6.1 and R6.4.

---

## Phase 7: indicators, fills, operations, hygiene (~2–3 days)

| ID | Change | Timing | Files |
|---|---|---|---|
| R7.1 | `calc_supertrend` (unused, not a SuperTrend): delete | safe | `packages/scanner/.../indicators.py:77` |
| R7.2 | Indicator parity: Bollinger `ddof=0`; RSI/ATR seeded with an SMA; golden fixtures from TA-Lib; a parity test that the scanner, Nautilus indicators and the chart's `technicalindicators` agree within tolerance on the same bars (M1). Regenerate `test_indicators_golden` and the triage eval sets afterwards | **live path** (D5) | `indicators.py`, `tests/`, `TradingViewChart.tsx` |
| R7.3 | Depth-aware paper fills (walk the L2 book when present; else top-of-book + size-proportional slippage) | **live path** (D5); after the L2 review | `packages/execution/.../matching.py` |
| R7.4 | `decision_trace_id` from candidate → gate → triage → debate → approval → command → fill; stored on `execution_commands` as a nullable column **set at insert** (the API role has no UPDATE there, N17); migration (S4) | safe | services, migration |
| R7.5 | Heartbeat alerts (learner, exit monitor, local tier, `sentiment:last_ok`) through the existing `notifications_outbox`; alerts queue there while the opt-in notifier is stopped | safe | `services/notifier`, orchestrator |
| R7.6 | `git rm apps/web/fix*.py apps/web/test_indicators*.js apps/web/add_15_indicators.py apps/web/tsconfig.tsbuildinfo` (nothing references them); ignore `graphify-out/cache/` and `tsconfig.tsbuildinfo` | safe | `.gitignore` |
| R7.7 | README rewritten to the current truth; one user manual (`USER_MANUAL.md` as source, HTML/PDF generated) | safe | `README.md`, `docs/` |
| R7.8 | Ollama 0.35.1 (operator, sudo); deploy the distilled triage student only if it still beats the base on the R3.1 residual | operator | HANDOFF |
| R7.9 | **Backups (M2):** nightly `pg_dump -Fc` of `cryptonite` to `~/backups/` with 14-day rotation (systemd timer or cron), plus `data/` (catalog, lake, artifacts) weekly; secrets (`.env`, `.env.notifier`, `infra/security/*.pem`) in a separate **encrypted** archive (`age` or `gpg`), since the DB's TOTP secrets cannot be decrypted without the AES key (N7). Keep backups off the repo and out of cloud sync. One restore drill into a scratch DB, documented in `docs/runbooks/backup.md` | safe, **do first** | `scripts/backup.sh`, runbook |
| R7.10 | TOTP key fallback to zeros (N6): use `FALLBACK_DEV_KEY` only when `CRYPTONITE_ENV` is `dev`/`test` (set in the test config and the `test` compose service); otherwise refuse to start with a clear error. Verify the live `api` and `notifier` containers have the key **before** deploying | safe once verified | `services/api/.../auth.py:32-61`, `tests/conftest.py`, compose |

---

## Phase 8: features (after Phases 1–5, by value)

| ID | Feature | Notes |
|---|---|---|
| F1 | Regime tags (trend / chop / high-vol from ATR% and ADX) on decisions, backtest trades and outcomes; slice reports by regime | reporting first, no effect on trading |
| F2 | Shadow mode: candidate routes run in parallel on live traffic and are logged, never acted on; promote on labelled results | gateway; `ai_model_routes.shadow_model` |
| F3 | Scheduled live-vs-backtest report per mandate strategy in the daily digest | uses `compare_live_vs_backtest` |
| F4 | Correlation-aware exposure across mandates (BTC/ETH ~0.8): cap combined beta-to-BTC | an extra check before the dispatcher, never replacing it; live path (D5) |
| F5 | Calibrated conviction (isotonic) once ≥ 200 labels; show raw and calibrated | reporting first |
| F7 | Configurable risk limits (replaces R1.8's read-only card): stored per (environment, account), read by the paper risk checks, tighten-only without TOTP, loosen only with TOTP and never past the invariants (25% clamp, 20% backstop) | risk-engine change: design note + ADR, live path (D5) |
| F6 | Real on-chain / derivatives-flow inputs behind a tool; only then re-add the On-Chain Analyst | needs a data-source decision |

---

## Schedule (with D5 = hold live-path items)

| Week | Work |
|---|---|
| 1 (10-05 → 10-11) | R7.9 backups; Phase 1 (incl. R1.2c–d, R1.7, R1.8); Phase 2; R3.2 if approved; R7.1, R7.6, R7.10 |
| 2 (10-12 → 10-18) | Phase 4 (informational); Phase 3 built and evaluated, **not deployed** |
| 3 (10-19 → 10-25) | Phase 5; Phase 6 |
| 4 (10-26 → 11-01) | R7.4, R7.5, R7.7; L2 review on the unchanged process (~10-31) |
| 5 (11-02 →) | Deploy Phase 3, R7.2, R7.3 together, restart the next evaluation window on the new process; Phase 8 |

## Risks

- **Mark-to-market equity makes strategies look worse.** Expected; the benchmark row keeps it in context.
- **Rule triage may reject setups the model would have kept.** Rules mirror the eval labels exactly; spot-check 20 rejections.
- **Stricter gates may delay L2/L3.** That is the purpose; they stay informational until D6.
- **UI refactors can break Playwright selectors.** R6.7 runs last in its phase, behind new tests.
- **Removing the full debate's third analyst changes debate transcripts.** Only the full path; covered by S6 evals and held per D5.
