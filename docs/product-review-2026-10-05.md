# Cryptonite: Product Team Review (2026-10-05)

**Reviewed state:** `sayegh9/cryptonite` `main` @ `ecadd85` (PR #16 merged 2026-10-05 12:20 UTC), the running Docker stack, and the dev database `cryptonite` (Alembic `036`). All database and Redis access was read-only. Nothing was modified, committed or deployed.
**Lenses:** Product, Architecture/Backend, AI/ML and Quant, UI/UX and Research, QA/Reality check, DevOps.
**Method:** I read the code, configuration and documentation, then queried the live system (Postgres, Redis streams and counters, container state, the journal, cron, backups) and reproduced a data-alignment defect against Binance's own 1h bars. Every claim cites a file:line, a query result or a command output. Anything I could not check myself is marked **UNVERIFIED**.

---

## 1. Executive summary

Cryptonite has a strong **control plane**: mandates, the non-bypassable `GuardedOrderDispatcher`, TOTP-bound approvals, least-privilege database roles, a sandbox for model-written code, a cost-capped model gateway and decision traces. That is better safety engineering than most commercial retail bots have. The team's habit of critiquing its own work is also real. The fee fix in PR #10 turned a reported +0.16% backtest into −44%, and the team published that result.

The **decision plane**, which is where any edge would come from, is not yet trustworthy. It also produces too little activity to be evaluated:

1. **The scanner looks at the market one minute late.** The resampler treats the lake's `ts_init_ns` as the bar *open*, but it is the bar *close*. As a result, 16 of 23 BTC 1h bars on 2026-10-04 have the wrong close. Shifting the input back 60 s gives 0 mismatches. Every scanner signal, market brief, triage call and debate is built on these shifted candles. Candidates are emitted about 58 s *before* the bar they claim to describe has closed.
2. **The learning loop teaches the wrong lesson.** Full-debate verdicts are stored as `Overweight`. The outcome scorer treats anything that is not literally `BUY` as a short. Two "Overweight" entries that gained +0.98% and +0.82% in 24 h were recorded as LOSS. The LLM's reflections now say to "prioritize overbought conditions", and those lessons are recalled first in future debates.
3. **The L2 gate cannot be reached on the current design.** After 4.2 days at L1 there are 2 fills, **0 closed trades**, 3 recorded breaches, and exits only at a −5% stop or a +3%/−2% trail. Reaching 30 closed trades by 10-31 is not plausible. Meanwhile the D5 "hold until the L2 review" keeps the *pre-remediation* debate live: the On-Chain Analyst, which the team itself found argues from invented data, still produced 8 turns in the last 2 days. The hold protects an evaluation that cannot pass.
4. **Operations are fragile.** The workstation had **5 unclean crashes today** (`last -x`). The nightly backup cron has **never run**: there is no log, and all 4 dumps were made by hand on the same disk. Nothing alerts when the host dies, and the web UI is down after the latest reboot. CI runs **no tests**, only a secret scan.

**Verdict:**
- **Not ready for live capital** (1/10).
- **Not yet a credible paper-trading evaluation** (4/10). The fixes for 1, 2 and 4 are small, mostly S-sized. Once they land, the evaluation clock should restart on a corrected process.

### Readiness scorecard

| Dimension | Score | Justification |
|---|---|---|
| Product value / fit | 5 | Clear, differentiated concept: AI proposes, rules decide, autonomy is earned. But there is no demonstrated edge. Every core strategy loses to BTC buy-and-hold after fees (1m: −40% to −51% over 90 d; 1h: about flat; hold: +36.7%). The live AI book has 0 closed trades. The research loop has produced **0** reports in the DB. |
| Architecture / backend | 6.5 | Clean layering, outbox, idempotency and typed contracts. Weak spots: Redis stream consumers lose work on crash, the mark-to-market projector is never wired, test data leaks into the dev DB, and the scanner enqueues outside any transaction. |
| Security | 7 | TOTP, CSRF, least-privilege roles, localhost-only ports, Redis auth, sandboxing, lockout, and a zero TOTP key refused outside dev/test. Minus: 50 hardcoded dev passwords in compose, ingest connects as the DB owner, and secrets and backups sit on the same disk. |
| AI/ML signal accuracy | 3 | Shifted candles. Inverted labels. R:R is a constant 2.00 on all 15,469 candidates. Most rule conditions are *states* (always true in a trend), not events. The exits do not match the thesis the debate argued. |
| Data quality | 4 | 200 d of history with completeness checks (good), but the 1-minute misalignment, 10,081 small parquet files, and 82% stale "replay" candidates. |
| Backtest realism | 5 | Fees are now charged (v3), and there is a benchmark and mark-to-market. Slippage is "1 tick with 50% probability", which is about $0.01 on BTC (effectively zero). Fills are bar-based with no latency. The live AI pipeline itself has **no** historical replay. |
| UI/UX | 5.5 | Rich, consistent dark UI, honest "unknown" states after R1, and an axe check in e2e. But the header always says "ZERO TRADING AUTHORITY (READ ONLY)", unrealized PnL always reads 0.00, Telegram approvals lack price, stop and worst-case loss, and the F7 form claims limits apply when they do not. |
| QA / test evidence | 5.5 | About 1,045 pytest and 77 Playwright tests are *reported*, and tsc and eslint are clean (verified). But no test caught the two critical defects above, and CI enforces none of the suite. |
| DevOps / reliability | 3 | Single crashing host. Backups never scheduled in practice. No external dead-man alert. The web UI is not supervised. The launcher's `compose up -d` plus auto-migrate can undo the deploy hold. |
| **Overall (paper research platform)** | **4.5** | Safety: good. Evidence of value: absent. Several S-sized fixes would raise this to about 6.5 within 2 weeks. |
| **Overall (live trading)** | **1** | Blocked by edge, data, ops and host stability, in that order. |

---

## 2. Current state snapshot (verified 2026-10-05 ~13:50 UTC)

### Shipped since the last memory note
- **PR #16** (`fix/redis-self-heal`, merged 12:20 UTC). Redis repairs a damaged AOF at start; long-running services restart after a reboot. *New since the remediation memory.*
- PRs #12–#15 (catalog self-heal, R7.3 fill sizes, F4/F7, F2 shadow mode) were merged 07:33–07:57 UTC and deployed at about 10:15 UTC. DB at `036`.
- PRs #9–#11 (remediation, fees and metrics v3, Phase 8 reporting) were merged earlier today.

### Container state
| Service(s) | Image date (local, EEST) | Status |
|---|---|---|
| api, backtester, agent_worker, research_worker, ingest, outbox, notifier, derivatives_feed, exit_monitor, strategy_sandbox | 10-05 13:13 | Current |
| **orchestrator** | 10-05 07:24 | **Held**: R7.3, F4 and F7 are *not* live |
| **scanner** | 10-04 04:08 | **Held** |
| **debate_worker ×3, learner** | 10-04 17:42 | **Held**: pre-remediation (On-Chain Analyst live, no rule triage) |
| sentiment | 10-05 05:39 | Unchanged |

**Web UI is not running**: `curl localhost:3000` returns 000 after the 16:37 reboot.

### Status of the known context items
| Item | Status now |
|---|---|
| Phases 0–7 on main | Confirmed |
| Remediation PR #9 merged 10-05 | Confirmed (`7a3f5a4`), followed by #10–#16 |
| Deploy hold until L2 review (D5) | Still in force for orchestrator, scanner, debate_worker and learner. **The L2 review date is unreachable** (see F-H1). |
| R7.3 wiring "awaits approval" | **Changed**: merged as PR #13, but **not live**, because the orchestrator is held |
| Distilled model blocked on Ollama upgrade | Still blocked: `ollama` reports `0.33.2`; the adapter is at `~/cryptonite-train/out/triage-lora` |
| Telegram live checks (veto, foreign chat, kill-switch Critical) | Still open. `notifications_outbox` has never carried a CRITICAL alert; the only kinds are APPROVAL, APPROVAL_OUTCOME and `workstation_test`. |
| Sudo items (`python3.12-dev`, Ollama 0.35.1) | Ollama not upgraded. `python3.12-dev` not installed (`dpkg -l` returns nothing). |
| Gate 5 (14 d L1, 0 breaches), earliest 10-15 | **Cannot pass while the 3 BREACH events stand** (D4 undecided) |
| L2 candidacy, earliest 10-31 | `gate_status.py` (read-only): days 4.2/30, closed trades **0/30**, breaches **3**, Sortino "inf vs not measured" |
| Untracked `scripts/launcher/` | Still untracked. It contains `compose up -d` and `alembic upgrade head` (see F-M2). |
| In-flight work | Another session is working in `wt/instr` (`fix/heal-empty-instrument-files`) and ran pytest on `cryptonite_ci_instr` at 13:41 UTC. |

### Live activity (4.2 days at L1)
| Measure | Value |
|---|---|
| Candidates (all time) | 15,469 |
| Candidates on 10-05 | 248, of which **35 were fresh** (bar closed ≤ 15 min before creation) |
| Gate on 10-05 | STALE 206, COOLDOWN 19, TRIAGE_ACCEPTED 16, TRIAGE_REJECTED 7 |
| Debates | 59 total: 52 NO_TRADE, 4 COMPLETED, 1 REJECTED, **2 stuck RUNNING** |
| Approvals | 9: 8 APPROVED, 1 EXPIRED |
| Fills | 2 (BTC $800, ETH $500) |
| Approvals that failed after approval | 3 XRP approvals rejected `NO_LIVE_QUOTE_FOR_SIZING`; 3 BTC approvals became BREACH events |
| Open positions / closed trades | 2 / **0** |
| AI spend | $0.73 (10-04), $0.63 (10-05). The cap is $25/day. |
| Research reports in DB | **0** |
| Exit debates (`stream:exit_debate_queue`) | **0, ever** |

---

## 3. Strengths (with evidence)

1. **Deterministic safety core.**
   - `GuardedOrderDispatcher` clamps orders to 1–25% (`packages/execution/cryptonite_execution/risk.py`).
   - Mandate check before the dispatcher. DB constraints on mandates (`chk_ai_mandates_*`: drawdown ≤ 20%, position 1–25%, perps only at L3).
   - TOTP for risk-adding approvals (`services/api/cryptonite_api/routes/ai.py:279-283`). Login lockout after 5 attempts (`auth.py:84-94`).
   - The zero TOTP key is refused outside dev/test (`auth.py:32-70`).
2. **The AI is contained.** One gateway with a ledger and traces (`ai_decision_traces`, `llm_cost_ledger`). Model SQL runs as a read-only role. The sandbox has no network or secrets. Ollama binds to the docker bridge only. Ports are on 127.0.0.1 (`infra/docker-compose.yml`).
3. **Self-correcting culture.** The remediation plan ran 8 review passes (`docs/remediation-plan.md` §0). The metrics v3 fee fix exposed losses (`docs/remediation-execution.md`, Follow-ups). The informational gate checks (R4.7) and ADRs 0001–0006 show the same habit.
4. **Research-loop design is sound on paper.** Anchored walk-forward folds, Deflated Sharpe Ratio over all trials, a ±10% robustness check and a single-use holdout (`packages/agents/cryptonite_agents/research/loop.py:1-13, 42-47`).
5. **Data plumbing done carefully.**
   - The resampler applies strict completeness checks (`packages/scanner/cryptonite_scanner/resampler.py:127-139`).
   - Idempotent candidates (`uq_signal_candidate_idempotent`).
   - Ingest commits closed klines only (`services/ingest/cryptonite_ingest/stream.py:230-298`, `recovery.py:79,140`).
   - The outcome labeller correctly treats `ts_event_ns` as the bar close (`services/debate_worker/outcomes.py:33-50`).
6. **Cost discipline.** Under $1/day of AI spend. Local Gemma handles triage and summaries. The bake-off and eval sets are in HANDOFF (A6.2).
7. **Frontend hygiene.** `pnpm typecheck` and `pnpm lint` are clean (run today). 71 Playwright tests in 16 specs, including `a11y.spec.ts` with axe. Only 1 arbitrary-px text class is left, after R6.5.
8. **Good operator primitives.** Telegram approvals bound to a command, risk-adding approvals that time out to *no*, the ntfy channel, decision-trace links from answers ("Why?"), and Mission Control.

---

## 4. Weaknesses and risks

Severity scale: **Critical** means it invalidates results or can cause loss or outage. **High** means a material defect or a blocker. **Medium** means real, but contained. **Low** means hygiene.

### Critical

**F-C1: Scanner candles are shifted by one minute (data alignment / look-ahead labelling).**
- **Cause:** the lake stores `ts_init_ns = ts_event_ns = bar close`. A lake row showed `received_at` 02:34:59.675 and `ts_event` 02:35:00. `query_bars_df` returns these columns (`packages/storage/cryptonite_storage/queries.py:54-56`). The resampler has no `bar_open_ns` column to use, so it falls back to `ts_init_ns` *as the open* (`resampler.py:60-63`).
- **Effect:** every 15m and 1h window includes the previous window's last minute and drops its own last minute. A window becomes "complete" when the second-to-last minute closes.
- **Evidence:**
  - BTC 1h on 2026-10-04: **16 of 23 closes differ** from Binance's native 1h bars. After shifting the input −60 s: **0 of 23** for close, high and low.
  - 131 of 138 fresh 15m candidates and 31 of 33 fresh 1h candidates were created up to 58 s *before* `interval_end_exclusive_ns`.
- **Blast radius:**
  - Every scanner rule, the market brief sent to the LLM, the hard triage rules, debates and regime tags.
  - Backtests read the Nautilus catalog, which is correctly aligned, so **live signals and backtests disagree by construction**.
  - The R7.2 "indicator parity" test passed because it feeds the same bars to all three implementations. It never tests the lake-to-resampler path.

**F-C2: The outcome and calibration loop inverts BUY-like verdicts that are not spelled "BUY".**
- **Cause:**
  - The full debate stores the CRO's `conviction_level` (`Buy | Overweight | Hold | Underweight | Sell`; `packages/schemas/cryptonite_schemas/debate.py:27`) as `recommendation` (`services/debate_worker/main.py:321,328`).
  - `score_decision` (`packages/execution/cryptonite_execution/calibration.py:41-49`) treats anything other than `BUY`/`HOLD` as a short.
  - `signed_outcome` (`services/debate_worker/outcomes.py:54-56`) does the same.
  - `outcomes_by_regime` (`calibration.py:211`) repeats the error.
  - Routing does it right (`mandate_routing.py:41` includes `OVERWEIGHT` in `BUY_WORDS`), so trades are placed correctly but **graded backwards**.
- **Evidence:** `portfolio_decisions` mixes `HOLD` (51), `Hold` (2), `Buy` (2) and `Overweight` (3). Two Overweight decisions returned +0.985% and +0.823% at 24 h. Their reflections are stored as `outcome=LOSS` with `actual_outcome_pnl −0.0099 / −0.0082`. Their lessons read: "Even with strong bullish conviction … strictly prioritize immediate overbought conditions…".
- **Effect:** `recall_lessons` ranks lessons by `abs(alpha)` first (`main.py`, `recall_lessons`), so the wrong lessons are the ones most likely to be injected into future PM prompts. The L3 calibration, F1 regime tables and F5 isotonic map all inherit the inversion.
- **Tests miss it:** `tests/test_debate_mandate_routing.py` uses "Overweight" for routing, but no calibration or outcome test does.

**F-C3: The host is unstable and the trading stack runs only on it.**
- **Evidence:**
  - `last -x` shows **5 crashes on 2026-10-05**: sessions ending 14:47→crash, 15:55, 15:57→crash, 16:21→crash, plus the 07:05 freeze and hard reset.
  - `journalctl --list-boots` shows 6 boots today.
  - The journal has **no entries from 01:00 to 04:59 local** on 10-05.
  - Damage so far: a corrupted Redis AOF tail and 0-byte catalog files (memory note). PR #16 now auto-truncates the AOF.
- **Effect:** for a system with approval timeouts, stops and evaluation clocks, an unannounced outage is a correctness problem, not just an availability one. Exit stops are not enforced while the host is down.
- **Root cause: UNVERIFIED** (hardware, thermal, PSU, GPU driver or memory). The HP Z840 has 62 GB RAM and **no swap**, the load average was 14 during a concurrent pytest run, and an RTX 3080 Ti has 9.6/12 GB VRAM in use.

### High

**F-H1: The L2 gate cannot be reached, and the deploy hold freezes a process already known to be defective.**
- **Gate status:**
  - `gate_status.py`: 0 closed trades after 4.2 d, 2 fills in total, 3 breaches.
  - `honest_paper` reports `ok: true` even when "not measured" (`autonomy.py:112,216-221`).
- **Why trades never close:** exits come only from fixed rules: 5% hard stop, trail activates at +3% and gives back 2% (`services/scanner/cryptonite_scanner_service/exit_monitor.py:46-49`). There is no take-profit, no time stop and no thesis-based exit. Exit debates fire only at −2% and have **never** run (`stream:exit_debate_queue` length 0).
- **What the hold keeps live:**
  - The held `debate_worker` still runs the On-Chain Analyst (8 turns in 48 h). The remediation removed it as fabricated input (R3.2).
  - No rule triage (`gate.py` in the container has 0 `hard_rejects`).
  - The old inverted learner.
- **Effect:** D5 protects the "integrity" of a window that will fail on `closed_trades` and `mandate_breaches` regardless, and that measures a process the team has already rejected.

**F-H2: Signal construction gives the LLM nothing real to judge on reward-to-risk, and floods the queue.**
- **R:R is constant:** it is exactly **2.00 on all 15,469 candidates**, because stop and target are fixed ATR multiples (`packages/scanner/cryptonite_scanner/models.py:42-45`: 2×/4× ATR). The R3.1 "R:R < 1.5" rule can never fire. Triage outputs cite "a solid 2.0 reward-to-risk" as a reason to debate.
- **Conditions are states, not events:** every condition has `lookback: 0`, for example `macd-momentum-shift` = `close > ema_50 AND macd_hist > 0`, so it fires on every bar of an uptrend. That spec produced **8,956 candidates in 3 days**.
- **Test specs pollute production:** 11 `test-spec-*` specs are **active** in the dev DB, each a copy of `rsi-oversold-reversal`. Together they produced about 2,100 duplicate candidates in 3 days (LE-10 cleanup missed them).
- **Historical replays:** `ScannerEngine.evaluate` runs over the whole lookback (`services/scanner/cryptonite_scanner_service/worker.py:478-488`) and enqueues every not-yet-seen match. After restarts or backfills that means historical setups: 82% of 10-05 candidates were STALE, and on 10-03 there were 14,111. The stream is untrimmed (`xadd` without MAXLEN, `worker.py:607`; `stream:debate_queue` length 15,471). The enqueue also sits outside the DB transaction, so a failure is only logged (`worker.py:600-615`).

**F-H3: Marks are never updated, so the router, Mission Control and the Brain rail use entry prices.**
- **Cause:** `CoalescedMarkToMarketProjector` (`packages/execution/cryptonite_execution/bridge.py:585-640`) is exported but never started by any service (grep shows no instantiation).
- **Evidence:** `paper_positions.current_price = entry_price` and `unrealized_pnl = 0` for both open positions. `paper_accounts.updated_at` is frozen at 02:47 UTC.
- **Consumers of the stale marks:**
  - Debate-router room: `services/debate_worker/mandate_routing.py:73-82`
  - Mission Control `exposure_usd` and `budget_used_pct`: `services/api/cryptonite_api/routes/ai.py:217-219`
  - Fallback marks in `mandate_gate.py:100` and `cross_mandate.py:34`
  - Brain rail PnL: `apps/web/src/components/brain/BrainContextRail.tsx:91-98`
- The enforced orchestrator check prefers live `ref_prices`, so this is not a risk bypass. It is a mis-sizing and display defect of the same class as the 10-04 BREACH incident.

**F-H4: CI enforces nothing but secret scanning.**
- `.github/workflows/security.yml` runs TruffleHog and `git diff --exit-code`.
- pytest (~1,045 per reports), Playwright (77), ruff (495 findings today) and tsc/eslint are run by hand.
- 16 PRs in about 10 days, and 5 merged to main within about 5 h today. "Main CI green" (memory) means only "no secrets found".

**F-H5: Backups have never run on schedule and are not off-machine.**
- The crontab is installed (`15 2 * * * cryptonite-backup db …`), but `~/backups/cryptonite/backup.log` **does not exist**, the journal has no `(samx) CMD` cron line, and `drill-last.log` is 0 bytes.
- All 4 dumps (`…T2051Z`, `T0236Z`, `T0422Z`, `T1011Z`, about 540 MB each) were taken by hand during deploys.
- Dumps, the encrypted secrets archive and the passphrase (`~/.config/cryptonite-backup/passphrase`) are all on the same NVMe, which is 80% full.

**F-H6: The debate queue loses work on crash.**
- Each process takes a random consumer name (`services/debate_worker/main.py:38`) and reads only `>` (`main.py:444-447`). There is no `XAUTOCLAIM`/`XPENDING` recovery, and failed messages are never acked or retried.
- **Evidence:** 2 messages are stuck in the PEL, `debate_runs` has 2 rows `RUNNING` forever, and **41 consumers** are registered, 30 idle for more than 1 h.
- With 3 replicas and frequent crashes, this will keep growing.

**F-H7: Mandates and instances can be created on instruments or timeframes with no data feed.**
- Ingest covers only `BTCUSDT,ETHUSDT` at `1m,1h` (`infra/docker-compose.yml:71-75`). Mandate creation does not validate against that universe (`routes/ai.py:278-300`; `schemas/mandate.py:174-188`).
- **Evidence:**
  - The `XRPUSDT.BINANCE` mandate's 3 approved Brain orders were all rejected `NO_LIVE_QUOTE_FOR_SIZING`, created within 6 s of each other.
  - `INST-513B9013` (SOL 1m) and `INST-7DF91538` (BTC **15m**, a timeframe with no live bars) show `RUNNING` with `last_processed_bar_ns = 0`.
  - Instances carry `strategy_source_hash = 'dummy_hash'` (`services/orchestrator/cryptonite_orchestrator/server.py:709`), which undercuts the "version-locked" honest-paper claim.

**F-H8: F7 says limits apply when they do not, which reintroduces the N9 problem.**
- `RiskLimitsForm.tsx:96` shows "Saved. The orchestrator applies it within 60 s."
- The orchestrator is held on a pre-F7 image, so saved overrides are stored but not enforced. F4 cross-mandate checks are not running either.
- Latent today: `risk_limit_overrides` has 0 rows.

### Medium

| ID | Finding | Evidence |
|---|---|---|
| F-M1 | Nothing outside the host watches the host. R7.5 heartbeats run inside the orchestrator on the same host, so 5 crashes produced **0** alerts. The web UI is not supervised: it is down now. | `ops_monitor.py:11-13,133`; `notifications_outbox` kinds; `curl :3000` returns 000 |
| F-M2 | The untracked launcher runs `alembic upgrade head` and a bare `compose up -d` on every start (`scripts/launcher/cryptonite-up.sh:74,78`). It applies schema changes without review and recreates any held service whose compose config changed (the orchestrator gained a lake mount after its container was created). | file |
| F-M3 | Gate funnel counts live only in Redis with an 8-day TTL (`gate.py`, `_count`). No durable funnel analytics exist, and the 09-30 to 10-03 history will expire. | `debate_gate:*` keys |
| F-M4 | The slippage model is effectively zero: "conservative" means 1 tick with 50% probability (`packages/backtester/cryptonite_backtester/execution_profiles.py:41-51`), about $0.01 on BTC. `adverse_slippage` charges **0 fees**. Fills are bar-based with no latency or spread. | file |
| F-M5 | Debate structure is biased toward the bull: Bull opens, Bear rebuts, **Bull gets the final word** (`main.py:276-284`). Exit-debate personas are named in reverse (EXIT_BULL argues to cut). The design mirrors the TradingAgents paper but drops its symmetric rounds. | file |
| F-M6 | Verdict vocabulary is not normalized at write time (`HOLD/Hold/Buy/Overweight`), which is the root of F-C2. | `portfolio_decisions` |
| F-M7 | `price_precision=2` is hardcoded in the scanner (`worker.py:487`, `engine.py:66,135-142`) and in the orchestrator bar fallback (`server.py:1340-1343`). Prices for anything under about $10 (XRP, DOGE, ADA) get destroyed. This blocks universe expansion. | file |
| F-M8 | The research loop has never produced a report: 0 `research_reports` rows and no `data/research_reports`. The headline feature is unproven in production. | DB |
| F-M9 | The dev DB is polluted by tests: 80+ `execution_kill_switch` rows from 09-24/25 (ACC-RACE-*, ACC-CRASH-*, several still latched) and the 11 active test specs. | DB |
| F-M10 | Operational observability is thin: container logs use `json-file` with no rotation (`docker info`, no `logging:` in compose), there is no metrics stack, and only 4 healthchecks cover about 27 services. | compose |
| F-M11 | Telegram approval messages have no decision price, stop, worst-case loss, thesis line or mandate room left (`services/notifier/cryptonite_notifier/render.py:19-31`; sample: "BUY ETHUSDT.BINANCE 5% of mandate ($500) · conviction 0.75"). R6.4 added this context only on the web. | DB sample |
| F-M12 | Mode labels contradict the real state. The header is hardcoded "ZERO TRADING AUTHORITY (READ ONLY)" (`apps/web/src/app/page.tsx:336`). The Brain says "SIMULATION MODE (RISK-FREE)" (`AIBrainWorkspace.tsx:114`). Mission Control says "PAPER" while mandates hold approval-gated authority. | files, screenshots |
| F-M13 | Documentation has drifted. HANDOFF "Resume here" is dated 10-04 (pre-#9). The AUDIT_STATUS header says "pytest ⛔ not run" (09-23). The README Status omits the deploy hold. | files |
| F-M14 | Decision price comes from the cached `market:24h` last price, with a fallback straight to `api.binance.com` from inside the debate worker (`main.py:190-205`). That is unbounded staleness plus an undeclared egress path. | file |

### Low
- ruff: 495 findings (F401 128, E402 33, F841 12, one bare `except`).
- Small-file problem: 10,081 lake parquet files, 546 for one BTC day.
- 50 hardcoded `*_dev_pass` occurrences in compose. Ingest connects as the owner role `cryptonite` (`compose:66`).
- Triage `confidence` semantics are ambiguous (`confidence: 0.1` together with `worth_debate: false`).
- TOTP codes transit Telegram chat. Replay-protected, but the codes travel through a third party.
- The Brain rail prints raw quantities (`0.009399821403393336`, `0E-18`) (`BrainContextRail.tsx:96`).

---

## 5. Accuracy enhancement plan

**Principle:** prove that the *deterministic* signal has a base-rate edge after fees first. Then measure the *lift* each AI stage adds over that base rate. Today neither is measured.

### 5.1 Data
1. **Fix alignment (F-C1).** Derive `bar_open_ns = ts_event_ns − 60 s` for 1m lake rows in `query_bars_df` or in the resampler. Add a regression test that resamples a day of lake 1m and compares the result with ingest's native 1h bars, expecting 0 mismatches. Then regenerate `triage_v3` and `triage_lake_v1` and the brief fixtures.
2. **Emit only fresh candidates.** Enqueue a candidate only if its bar closed within 1 bar of now. Keep historical matches in `signal_candidates` for research, not in the debate queue. Add `MAXLEN ~ 10000` to the stream.
3. **Compact the lake daily.** One file per symbol per day cuts the 546 files per day.
4. **Universe-aware precision.** Read the tick and step size from `instruments`, not a hardcoded 2.

### 5.2 Signals
1. **Events, not states.** Use crossovers (`macd_hist` crosses above 0; `close` crosses above `bb_upper`) via `lookback ≥ 1` comparisons. Expect candidate volume to drop by about 10–50× and the fresh share to rise.
2. **Structure-based stops and targets.** Use swing low/high or the Donchian channel for the stop, and the next HTF level or ATR for the target. That makes R:R a real feature, and the R3.1 rule becomes meaningful.
3. **Thesis-consistent exits.** Persist the candidate's stop and target on the position. The exit monitor should enforce them, plus a time stop (for example 3×–6× the signal timeframe), as well as the 5% catastrophe stop. This makes closed trades possible at a rate the gate can measure.
4. **Archive test specs. Add a "max active specs" guard.** Deduplicate candidates across specs per (instrument, tf, bar).

### 5.3 Models and decisions
1. **Normalize verdicts (F-C2).** Map to `{BUY, SELL, HOLD}` at write time (`Overweight`→BUY, `Underweight`→SELL). Re-score existing outcomes. Delete or regenerate the inverted reflections; there are only 2, so this is cheap. Add a property test covering every `conviction_level` literal.
2. **Counterfactual labelling.** Label *every* candidate, including gate-rejected, triage-rejected and NO_TRADE ones, at 1h, 4h and 24h with the same labeller. This gives the base rate, triage precision and recall versus outcome, and debate lift, without paying for any extra model calls.
3. **Meta-labelling challenger.** A small gradient-boosted classifier on brief features predicts "a +1R hit before a −1R hit". Run it in F2 shadow mode beside Gemma triage, and promote whichever has the better Brier score after fees.
4. **Symmetric debate.** Alternate Bear/Bull openings, or give both the same number of turns and let the PM speak last. Require each side to put probabilities on stop-hit versus target-hit, and score those probabilities with Brier.
5. **Calibration.** Report Brier and log-loss along with ECE. Calibrate the PM's conviction against *trade outcome* (stop/target/time), not raw 24 h return.

### 5.4 Backtesting and validation
1. **Pipeline replay ("time machine").** Run the scanner with fixed exits over 200 days on catalog bars with conservative fees. This is the deterministic baseline, and it costs nothing in model calls. Then replay triage on a sampled subset with local Gemma, which is free.
2. **Realistic costs.** Model slippage as half the recorded spread plus impact proportional to √(size/volume). Ingest already records book tickers, so store spread histograms. Make the stress profile 2× fees and 3× spread.
3. **Statistics.** Purged and embargoed CV for research. Keep DSR. Add an SPRT or Bayesian sequential test, so the gate can stop early as either "no edge" or "edge", instead of waiting for a fixed 30 trades.

### 5.5 Evaluation design
- Introduce a **shadow book**: every BUY verdict is paper-filled automatically in a non-mandate ledger, with or without operator approval and regardless of headroom. Judge the *AI* on the shadow book, and judge the *mandate* (operator plus AI) on the real paper book. This multiplies sample size and separates operator behaviour from model skill.

---

## 6. Optimization plan (performance, cost, reliability)

| Area | Action | Expected effect |
|---|---|---|
| Queue | Stable consumer names (hostname plus replica), `XAUTOCLAIM` of entries idle > 10 min, retry with a cap and then dead-letter, MAXLEN on streams, a reaper that marks `RUNNING` runs older than 30 min as FAILED | No silently lost debates; bounded Redis memory |
| Scanner | Incremental evaluation (latest closed bar only, per tf); cache enriched frames; compact the lake | Lower CPU; no replay floods |
| Marks | Start `CoalescedMarkToMarketProjector` in the orchestrator, throttled to 1–5 s rather than 250 ms, or compute marks on read from Redis quotes | Correct exposure, room and PnL |
| DB | Partition or TTL `signal_candidates`, `ai_mandate_equity` (5,772 rows in 5 days) and `debate_gate` funnel rows; nightly `VACUUM ANALYZE` | Keeps the 21 GB `scan_runs` incident from recurring elsewhere |
| Cost | Spend is $0.7/day against a $25 cap. Spend the headroom on **measurement** (counterfactual Gemma triage on all fresh candidates, which is free locally), not on more Pro calls | Better evidence at about the same cost |
| Reliability | Supervise everything: run the web as a compose service (`next start`) with `restart: unless-stopped`; add healthchecks to every service; set `logging: max-size: 50m, max-file: 5` | Survives reboots; disks stay bounded |
| Host | Diagnose the crashes (memtest86+, `journalctl -k -b -1`, `nvidia-smi -q -d PERFORMANCE`, `sensors`, PSU). Add swap or zram. Move the always-on core (ingest, orchestrator, exit_monitor, notifier, Postgres, Redis) to a stable small VPS or NUC and keep the GPU box for models and training | Stops stops being missed while the box is down |

---

## 7. UI/UX recommendations

1. **Truthful mode banner.** Compute it from state: "PAPER · 2 mandates at L1 · approvals required", or "PAPER · kill switch latched". Remove "ZERO TRADING AUTHORITY" and "RISK-FREE".
2. **Telegram approval card v2.** Show side, size in $ and %, decision price versus live price (slippage), stop and worst-case $ loss, mandate room after the trade, a one-line thesis, and the 3 most recent same-side outcomes (already in the API as `similar_decisions`). Add a deep link to the trace, not "trace #abcd1234".
3. **Pre-approval feasibility checks.** If an instrument has no live quote or the timeframe has no feed, disable Approve and say why. Block the mandate or instance at creation (F-H7). Prevent double-submits: three XRP approvals went in within 6 s.
4. **Mission Control density.** Two-thirds of the screen is empty while the kill-switch button dominates. Put a funnel strip there (candidates → fresh → triaged → debated → approved → filled → closed), the gate checklist with an ETA, heartbeats, today's spend and open PnL. Move the kill switch behind a confirm step at a smaller size, or keep it sticky but secondary.
5. **Units and formatting.** Show drawdown as "0.19%", not "0.1933". Format quantities to step size and hide FLAT perp rows. Show Sortino as "n/a (0 closed trades)" instead of "inf". Grey out the calibration chart until n ≥ 20 instead of plotting 6 points.
6. **Honest F7 copy.** Read the orchestrator's `risk:effective_limits` back. Say "Saved. Not yet enforced (orchestrator version X)" until it reflects the override.
7. **Onboarding.** A first-run checklist: create an operator and TOTP, verify Telegram (approve, veto and a kill-switch test, which are the operator's open items), create a mandate on a supported instrument, see the first candidate.
8. **Mobile.** Telegram is the mobile UI today. A minimal responsive `/approve/:id` page, behind the same session and TOTP, would give a richer card than Telegram can render.
9. **Accessibility (UNVERIFIED in the browser).** The web is not running, so I could not run axe live. I relied on `a11y.spec.ts` and the screenshots. Re-check contrast on the zinc-on-black secondary text in Mission Control.

---

## 8. New features and functions (prioritized)

| # | Feature | Why | Effort |
|---|---|---|---|
| 1 | **Shadow book** (auto paper-fill every verdict, outside mandates) | Gives the AI statistical power and separates operator skill from model skill | M |
| 2 | **Counterfactual outcome labels for all candidates** | Base rate and lift per AI stage, with no extra model cost | S |
| 3 | **Decision funnel dashboard** plus a durable `gate_events` table | Makes throughput and stage quality visible, replacing Redis TTL counters | S–M |
| 4 | **External dead-man's switch** (healthchecks.io or a self-hosted ntfy cron ping from the host and from the orchestrator) | Alerts when the host itself is down | S |
| 5 | **Pipeline replay backtester** (scanner, fixed exits, optional local triage) | Historical evidence for the live process | L |
| 6 | **Instrument onboarding** (add symbol → backfill → ingest → scanner universe → precision) | Unblocks XRP/SOL properly | M |
| 7 | **Thesis-bound exits** (candidate stop, target and time stop on the position) | Real closed trades; R:R becomes meaningful | M |
| 8 | **Meta-label triage challenger in F2 shadow mode** | A cheap, calibratable alternative to the LLM | M |
| 9 | **Trade journal and post-mortem** (trace → fill → exit → lesson, with operator notes) | Better reflections; operator learning | M |
| 10 | **Volatility-targeted sizing** within the mandate cap | Risk-normalized PnL, comparable across assets | M |
| 11 | **Spread and slippage recorder** feeding backtest profiles | Realistic costs | S |
| 12 | **Daily digest v2 on Telegram** (PnL vs BTC hold, funnel, health, spend, gate ETA) | One glance, every morning | S |
| 13 | **Real on-chain or derivatives-flow source (F6)** before re-adding that analyst | Removes fabricated context for good | M–L |

---

## 9. Prioritized roadmap

### Now (next 2 weeks)
| # | Item | Acceptance criteria |
|---|---|---|
| N1 | Fix host stability, or move the always-on core | 7 consecutive days with no `crash` entries in `last -x`; root cause written down |
| N2 | External dead-man's switch and supervised web | A simulated outage (stop the orchestrator, or power off) produces a phone alert within 5 min; `:3000` is back after a reboot with no manual step |
| N3 | Backups that actually run, kept off-machine | 3 consecutive nightly `backup.log` entries; a weekly drill log that is not empty; an off-machine copy (restic/rclone to external disk or B2) restored once; passphrase stored off-machine |
| N4 | Fix 1m alignment (F-C1) with a regression test against native 1h bars | 0 close/high/low mismatches over 30 days for BTC and ETH; no candidate created before `interval_end_exclusive_ns`; eval sets regenerated and baselines recorded |
| N5 | Normalize verdicts and repair labels (F-C2) | Every value in `portfolio_decisions.recommendation` is one of {BUY, SELL, HOLD} (backfill plus a check constraint); the 2 inverted reflections are regenerated; a test covers all 5 `conviction_level` literals |
| N6 | CI that gates merges | GitHub Actions runs ruff on changed files, tsc, eslint, pytest (Postgres and Redis service containers, throwaway DB) and a Playwright smoke run; branch protection on `main` |
| N7 | Decide D4 and end the D5 hold | Operator decision recorded in ADR 0004. The held services are deployed with the fixes. "Evaluation v2" starts on a stamped date, and the gate reads only that window. |
| N8 | Debate stream recovery and run reaper (F-H6) | A killed worker's message is reclaimed within 10 min; 0 `RUNNING` runs older than 30 min; consumer count stays at or below the replica count |
| N9 | Wire marks (F-H3) and fix F7 copy (F-H8) | Unrealized PnL changes with price in the Portfolio view and Brain rail; the F7 form shows "not enforced" until `risk:effective_limits` reflects the override |
| N10 | Hygiene: archive test specs; fresh-only enqueue; stream MAXLEN; feed validation for mandates and instances (F-H7) | Fresh share of enqueued candidates ≥ 95%; creating a mandate on an un-ingested symbol returns a 422 that names the reason |

### Next (1–2 months)
- **Counterfactual labels and funnel dashboard** (features 2–3). AC: per-stage precision and lift reported weekly with confidence intervals.
- **Event-based rules and thesis-bound exits** (§5.2). AC: the median holding period is known; at least 1 closed trade per day on the shadow book.
- **Shadow book** (feature 1). AC: the gate report shows both books; the AI's expectancy CI is computed on at least 100 shadow trades.
- **Pipeline replay** of scanner plus fixed exits over 200 d with conservative costs. AC: a published baseline versus BTC hold; specs that cannot beat fees are archived.
- **Slippage from recorded spreads.** AC: backtest-to-paper fill price error under 2 bps median.
- **Telegram card v2 and truthful mode banner.** AC: a usability test with the operator; approval time and errors logged.
- **Observability:** log rotation, Prometheus with node and Postgres exporters, and Grafana panels for the funnel, latencies and spend.
- **Ollama 0.35.1 plus distilled triage**, evaluated on the *regenerated* (aligned) eval sets.
- **First real research-loop runs.** AC: at least 5 `research_reports` rows, including at least 1 holdout-scored candidate, or a documented "no edge found".

### Later (beyond 2 months)
- Instrument onboarding and universe expansion with precision handling.
- Meta-label challenger promoted through F2 if it wins on Brier score after fees.
- F6 data source and an on-chain analyst re-added.
- Volatility-targeted sizing, and a portfolio-level beta budget (F4 live).
- A live-venue readiness checklist: Binance demo reconciliation soak, latency budget, secrets in a vault, separate hosts. **Not before** the shadow book shows a positive expectancy lower bound after fees.

---

## 10. Open questions for the owner

1. **D4:** should the 3 BREACH events from the 2026-10-04 sizing bug count toward Gate 5 and L2? As long as they count, both gates fail.
2. **D5:** will you end the hold and restart the evaluation on the fixed process? I recommend doing so, given F-C1, F-C2 and F-H1.
3. **What exactly is the L2 gate certifying:** the AI's skill, or the operator plus AI? Should a shadow book count toward it?
4. **Host:** is the Z840 crash pattern new, possibly since the GPU training work on 10-04? Is moving the always-on services to a separate small machine or VPS acceptable?
5. **XRP mandate and SOL/BTC-15m instances:** archive them, or should onboarding those instruments become a feature?
6. **Target user:** is this a personal workstation for one operator, or a product for others? That changes the priority of multi-user and trace scoping (LE-8), onboarding and hosting.
7. **Ambition for live capital:** is there a date or a capital amount in mind? That sets how strict the "edge proven" bar must be (for example the DSR and expectancy lower bound on N trades).
8. **Timeframes:** given fee drag (1m strategies lose 40–51% per 90 d), will you drop 1m and 5m from automatic triggering and focus on 1h and 4h?
9. **Backups:** where can an off-machine copy live (external disk, NAS, B2/S3)? Who holds the passphrase?
10. **Launcher (`scripts/launcher/`):** keep it and commit it? If so, should it stop auto-migrating and recreating services?

---

## 11. What I could not verify

- **pytest and Playwright counts** (reported 1,045/0 and 77/77): **UNVERIFIED**. I did not re-run them. Another session was running pytest on the same Postgres at the time, and the host crashed repeatedly under load today. I counted 779 `def test_` functions and 71 Playwright `test(` calls statically. I did verify that `pnpm typecheck` and `pnpm lint` pass and that ruff reports 495 findings.
- **Live UI behaviour**: **UNVERIFIED**. The web was not running and I did not start servers. The UX findings come from code and the committed screenshots (`docs/screenshots/11,13,14`).
- **Root cause of the host crashes**: **UNVERIFIED**.
- **Telegram veto, foreign-chat and kill-switch alert paths**: **UNVERIFIED** (still open per HANDOFF).
- Whether the held debate worker's triage differs materially from the merged code beyond R3.1 and R3.2: inferred from the container contents (`gate.py` has 0 `hard_rejects`; On-Chain Analyst turns are present).
- Competitive comparison (Freqtrade/FreqAI, Hummingbot, 3Commas, Cryptohopper, TradingAgents-style multi-agent frameworks) is from general knowledge, not fresh research.
