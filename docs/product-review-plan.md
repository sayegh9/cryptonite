# Product Review Plan (2026-10-05)

Source: [`docs/product-review-2026-10-05.md`](product-review-2026-10-05.md), the product team review of `main` @ `ecadd85`. Finding IDs (F-C1, F-H3, F-M7 …) refer to that report. This plan turns every finding, recommendation, UI/UX item and feature idea in the report into an executable work item, and names the agent that should do it.

`main` has since moved to `5298191` (PR #17, empty instrument files quarantined at start-up). Nothing in this plan depends on PR #17.

The three rules stay: **AI and UI propose, deterministic rules and risk controls decide, NautilusTrader executes.** No item weakens `GuardedOrderDispatcher`, the 1–25% clamp, the mandate check, the kill switch, TOTP approvals or the outbox.

---

## 0. How to run this plan

### Working rules (apply to every item)
1. **One item → one branch → one PR**, named `fix/<id>-<slug>` or `feat/<id>-<slug>`. Implementers work in their own git worktree (`isolation: worktree`) so parallel items do not collide.
2. **Tests first for defects.** Every Critical or High fix starts with a failing test that reproduces the finding, then the fix.
3. **Tests never touch the dev DB `cryptonite`** and never call paid models (CLAUDE.md). Use a throwaway DB (`cryptonite_ci_<item>`).
4. **Every PR has a reviewer agent** (column "Review") before merge. Critical items also get a **Reality Checker** pass that re-runs the acceptance check against real data.
5. **Merges:** feature PRs are merged by Claude; merges to `main` that the classifier blocks are handed to the operator as a `gh api` command (see memory: merges-to-main).
6. **No deploys of held services** (orchestrator, scanner, debate_worker, learner) until Phase E. Code merges to `main` freely; deployment is a separate, approved step.
7. **Operator-only actions** (sudo, hardware, BIOS, decisions, Telegram live tests, off-machine storage accounts) are marked **Operator**. Agents prepare the commands and checklists; the operator runs them.
8. **Hotspot files are serialized**, not parallel: `services/debate_worker/main.py` (B2, B5, B6, I1), `services/scanner/cryptonite_scanner_service/worker.py` (D1, D2, D6), `services/orchestrator/cryptonite_orchestrator/server.py` (B3, D5, E2).

### Agent roster

| Agent | Role in this plan |
|---|---|
| **Senior Project Manager** | Owns this plan, sequencing, status, phase exit reviews, decision log |
| **Product Manager** | Open questions with the operator, target user, success metrics, roadmap trade-offs |
| **Software Architect** | Host split, evaluation-v2 design, pipeline replay design, ADRs |
| **Backend Architect** | Services, Redis streams, orchestrator wiring, API validation |
| **AI Engineer** | Signals, triage, debate, calibration, labels, models |
| **Multi-Agent Systems Architect** | Debate topology and turn structure, worker recovery semantics |
| **Experiment Tracker** | Shadow book, sequential tests, gate statistics, evaluation protocol |
| **Database Optimizer** | Migrations, backfills, constraints, partitioning, retention |
| **Platform Engineer** | CI, compose supervision, backups, logging, observability, launcher |
| **Performance Benchmarker** | Host crash diagnosis support, scanner and DB performance |
| **Test Automation Engineer** | Playwright, CI smoke runs, flake control |
| **code-modernization:test-engineer** | Regression and characterization tests for the critical defects |
| **code-modernization:security-auditor** | Secrets, roles, dev passwords, egress paths |
| **Frontend Developer** | Web UI changes |
| **UI Designer** | Telegram card v2, Mission Control layout, mode banner |
| **UX Researcher** | Operator usability test, approval-time measurement |
| **UX Architect** | Onboarding flow, mobile approve page structure |
| **Accessibility Auditor** | Live axe and contrast re-check |
| **Data Visualization Engineer** | Funnel dashboard, calibration charts, Grafana panels |
| **DX Engineer** | Docs drift, runbooks, first-run checklist |
| **Code Reviewer** | Default PR reviewer |
| **Reality Checker** | Evidence-based sign-off on Critical items and phase exits |
| **Evidence Collector** | Screenshots and reproducible proof for UI items |

---

## 1. Decisions needed before work starts (Phase 0)

**Owner:** Product Manager with the Operator. **Recorder:** Senior Project Manager (writes answers into `docs/adr/0004-mandates-and-autonomy.md` and this file).

| # | Decision | Blocks | Recommendation |
|---|---|---|---|
| Q1 / D4 | Do the 3 BREACH events from the 10-04 sizing bug count toward Gate 5 and L2? | E3 | No: they come from a fixed bug. Record them with `excluded_reason`, do not delete them. |
| Q2 / D5 | End the deploy hold and restart the evaluation on the fixed process? | E1–E3 | Yes, once Phases B and D land. |
| Q3 | What does L2 certify: AI skill, or operator plus AI? Does the shadow book count? | F4, E3 | Both, reported separately: the AI on the shadow book, the mandate on the real paper book. |
| Q4 | Is moving the always-on core to a second small machine or VPS acceptable? | A1b | Yes, if crashes persist after A1a. |
| Q5 | XRP mandate and SOL 1m / BTC 15m instances: archive or onboard? | D4 | Archive now; onboarding is K1. |
| Q6 | Single operator or product for others? | G7, K-items | Single operator for this plan. |
| Q7 | Live-capital date or amount? | K5 | None until the shadow book shows a positive expectancy lower bound. |
| Q8 | Drop 1m/5m from automatic triggering? | D3 | Yes: trigger on 15m, 1h and 4h. Keep 1m for ingest and exits. |
| Q9 | Off-machine backup target, and who holds the passphrase? | A3 | External disk now, B2 later; passphrase in the operator's password manager. |
| Q10 | Keep the launcher? | A5 | Keep and commit it, without auto-migrate and without a bare `compose up -d`. |

**Done when:** every row has an answer recorded with a date.

---

### Answers recorded for the execution run (2026-10-05)

The operator delegated Phase 0 to the plan's recommendations and set these run rules:

- **Q1–Q10:** the "Recommendation" column above is adopted as the decision.
- **Merging:** each item's PR merges into the integration branch `review-plan` after its specialist review. One merge of `review-plan` into `main` is handed to the operator at the end.
- **Live system:** the run may apply migrations, clean test data, regenerate the inverted reflections and deploy the held services (E1), each only after a fresh backup and a Reality Checker pass.
- **Off-machine parts are out of scope:** A2 uses in-host alerts only (no external dead-man's switch), A3 fixes the local nightly backup and restore drill only. A1b is design-only. A6 and the hardware part of A1a stay with the operator.
- **Branch protection:** the run enables required CI checks on `main` and `review-plan` once C1 lands.
- **Phase K is out of scope** for this run (gated on evidence).

---

## 2. Phases at a glance

| Phase | Theme | Window | Gate to exit |
|---|---|---|---|
| **0** | Decisions | Day 1–2 | §1 answered |
| **A** | Ops safety net | Week 1–2 | Alerts reach the phone; backups run; no unreviewed migrations |
| **B** | Critical correctness | Week 1–2 | F-C1, F-C2, F-H3, F-H6 fixed and verified on real data |
| **C** | CI and hygiene | Week 1 (C1 first) | CI blocks merges on red |
| **D** | Signal pipeline | Week 2 | Fresh share ≥ 95%; R:R varies; no un-fed mandates |
| **E** | Deploy and evaluation v2 | End of week 2 | Held services deployed; v2 window stamped |
| **F** | Evidence layer | Week 3–6 | Per-stage lift reported; shadow book live |
| **G** | UI/UX | Week 3–6 | Usability test passed; UI tells the truth |
| **H** | Backtest realism and research | Week 4–8 | Pipeline replay baseline published |
| **I** | AI accuracy | Week 6–10 | Challengers judged on Brier after fees |
| **J** | Optimization and observability | Week 5–10 | Dashboards live; retention in place |
| **K** | Later | After week 10 | Gated on evidence |

C1 (CI) goes first so every later PR is gated by it.

---

## Phase A: Ops safety net

### A1a. Diagnose host crashes (F-C3)
- **Owner:** Operator, supported by **Performance Benchmarker** (prepares and reads diagnostics). **Review:** Reality Checker.
- **Change:** run `memtest86+` overnight; collect `journalctl -k -b -1` for each crashed boot; `sensors` and `nvidia-smi -q -d PERFORMANCE,TEMPERATURE,POWER` under load; check PSU and BIOS event log. Add 16 GB swap or zram. Cap GPU training jobs (`nvidia-smi -pl`) while the trading stack runs. Write findings to `docs/runbooks/host-stability.md`.
- **Done when:** a root cause (or ranked suspects) is written down, and swap is active.
- **Effort:** M (mostly wall-clock).

### A1b. Move the always-on core off the GPU box (F-C3, Q4)
- **Owner:** **Software Architect** (design), **Platform Engineer** (implementation). **Review:** code-modernization:security-auditor.
- **Change:** a second compose profile `core` (Postgres, Redis, ingest, orchestrator, exit_monitor, notifier, outbox, api) for a small machine or VPS; the GPU box keeps Ollama, training, agent and research workers, reached over WireGuard or Tailscale. Update ports, secrets and `docs/run-and-deployment-changes.md`.
- **Done when:** 7 consecutive days without a `crash` line in `last -x` on the core machine, and a GPU-box reboot does not stop stops being enforced.
- **Depends:** A1a result, Q4. **Effort:** L.

### A2. External dead-man's switch (F-M1, recommendation 2, feature 4)
- **Owner:** **Platform Engineer**. **Review:** Code Reviewer.
- **Change:** a cron ping every minute from the host and a ping from the orchestrator heartbeat loop (`ops_monitor.py`) to healthchecks.io or a self-hosted ntfy on another machine; a missed ping for 5 minutes alerts the phone. Add a CRITICAL test alert path.
- **Tests:** unit test for the heartbeat sender; manual drill (stop orchestrator; power-off).
- **Done when:** both drills produce a phone alert in ≤ 5 minutes; drill logged in the runbook.
- **Effort:** S.

### A3. Backups that actually run, off-machine (F-H5, recommendation 3)
- **Owner:** **Platform Engineer**; **Operator** provides target and passphrase storage (Q9). **Review:** code-modernization:security-auditor.
- **Change:** find why the `15 2 * * *` cron never fired (cron PATH, user crontab vs. system, `cryptonite-backup` not on PATH, cron service state); fix it and log to `~/backups/cryptonite/backup.log`. Add `restic` or `rclone` copy to the off-machine target. Move the passphrase off the disk. Add a weekly restore drill into a throwaway DB that writes `drill-last.log`. Prune local dumps to keep the disk under 70%.
- **Done when:** 3 consecutive nightly log entries; a non-empty drill log; one restore from the off-machine copy verified (row counts match).
- **Effort:** S–M.

### A4. Supervise the web UI and every service (F-M1, F-M10)
- **Owner:** **Platform Engineer**. **Review:** Code Reviewer.
- **Change:** add `web` as a compose service (`next start`, `restart: unless-stopped`, healthcheck). Add healthchecks to every long-running service (about 27; only 4 have one). Add `logging: {driver: json-file, options: {max-size: 50m, max-file: "5"}}` via an `x-logging` anchor.
- **Done when:** `:3000` answers after a reboot with no manual step; `docker inspect` shows a healthcheck on every long-running service; log files are bounded.
- **Effort:** S.

### A5. Make the launcher safe and commit it (F-M2, Q10)
- **Owner:** **Platform Engineer**. **Review:** Senior Project Manager (checks it respects the hold).
- **Change:** remove `alembic upgrade head` from `scripts/launcher/cryptonite-up.sh:74` (migrations become an explicit, reviewed step); replace the bare `compose up -d` (`:78`) with `--no-recreate` and an explicit service list that excludes held services; commit the launcher.
- **Done when:** running the launcher on a stack with a pending migration and a held service changes neither.
- **Effort:** S.

### A6. Operator open items carried over
- **Owner:** **Operator**; checklist prepared by **DX Engineer**.
- Items: Telegram veto, foreign-chat and kill-switch CRITICAL live checks; install `python3.12-dev`; upgrade Ollama to 0.35.1 (unblocks I5).
- **Done when:** each item is ticked in `HANDOFF.md` with the date and evidence (message id or command output).

---

## Phase B: Critical correctness

### B1. Fix the 1-minute bar shift (F-C1, recommendation 4)
- **Owner:** **AI Engineer** (quant data). Test first by **code-modernization:test-engineer**. **Review:** Code Reviewer, then **Reality Checker**.
- **Change:**
  1. In `packages/storage/cryptonite_storage/queries.py:54-56` (preferred) return `bar_open_ns = ts_event_ns − 60 s` for 1m lake rows, so callers never guess.
  2. In `packages/scanner/cryptonite_scanner/resampler.py:60-63` stop falling back to `ts_init_ns` / `ts_event_ns` as the open; require `bar_open_ns` and raise otherwise.
  3. Guard: never emit a candidate whose `interval_end_exclusive_ns` is in the future.
- **Tests:**
  - Regression: resample a stored lake day for BTC and ETH and compare with ingest's native 1h bars → 0 close/high/low mismatches.
  - Property: a window becomes complete only after its last minute closes.
  - Extend the R7.2 indicator parity test to go through lake → `query_bars_df` → resampler.
- **Follow-ups (same PR or next):** regenerate `triage_v3`, `triage_lake_v1` and brief fixtures; record new baselines. Flag that the distilled triage model must be re-evaluated on the new sets (I5).
- **Done when:** 0 mismatches over 30 days for BTC and ETH; 0 candidates created before their bar closed (query on `signal_candidates` after deploy).
- **Effort:** S (fix) + M (eval sets).

### B2. Normalize verdicts and repair the learning loop (F-C2, F-M6, recommendation 5)
- **Owner:** **AI Engineer**; migration by **Database Optimizer**. Test first by **code-modernization:test-engineer**. **Review:** Code Reviewer, then **Reality Checker**.
- **Change:**
  1. Add `normalize_verdict()` in `packages/schemas` mapping `Buy|Overweight|STRONG_BUY|LONG → BUY`, `Sell|Underweight → SELL`, `Hold → HOLD`, and use it at write time in `services/debate_worker/main.py:321,328`.
  2. Make `score_decision` (`calibration.py:41-49`), `outcomes_by_regime` (`calibration.py:211`) and `signed_outcome` (`outcomes.py:54-56`) call it defensively. Keep HOLD's continuous semantics as decided in remediation N16.
  3. Keep the original CRO wording in a new `conviction_level` column, so the sizing multiplier (`mandates.py:33`, `risk.py:585`) keeps working.
  4. Migration `037`: add `conviction_level`, backfill `recommendation` from the existing values, add `CHECK (recommendation IN ('BUY','SELL','HOLD'))`.
  5. Delete and regenerate the 2 inverted reflections through the gateway (local tier), and recompute calibration, regime and isotonic artifacts.
  6. Make `mandate_routing.BUY_WORDS`, `shadow._DIRECTION` and `routes/agents._BUY_RECOMMENDATIONS` use the same helper so there is one mapping.
- **Tests:** a parametrized test over all 5 `conviction_level` literals through scoring, signed outcome, regime tables and routing; a migration test with mixed-case legacy rows.
- **Done when:** every `portfolio_decisions.recommendation` is in {BUY, SELL, HOLD}; no reflection says LOSS for a BUY-like decision with positive return.
- **Effort:** S–M.

### B3. Wire mark-to-market (F-H3)
- **Owner:** **Backend Architect**. **Review:** Code Reviewer.
- **Change:** start `CoalescedMarkToMarketProjector` (`packages/execution/cryptonite_execution/bridge.py:585-640`) in the orchestrator, throttled to 1–5 s, fed by live quotes. Make `mandate_routing.py:73-82`, `routes/ai.py:217-219`, `mandate_gate.py:100` and `cross_mandate.py:34` prefer live quotes and fall back to the stored mark with an explicit staleness flag.
- **Tests:** integration test that a price move updates `paper_positions.current_price`, `unrealized_pnl` and `paper_accounts.updated_at`.
- **Done when:** unrealized PnL moves with price in Portfolio and the Brain rail (after E1).
- **Effort:** S–M.

### B4. Debate queue recovery and run reaper (F-H6, recommendation 10)
- **Owner:** **Backend Architect**, design check by **Multi-Agent Systems Architect**. **Review:** Code Reviewer.
- **Change:** stable consumer names (`hostname-replica`, `main.py:38`); on start and every minute `XAUTOCLAIM` entries idle > 10 min; retry up to 3 times with a counter, then move to `stream:debate_dlq` and ack; ack on terminal failure (`main.py:444-447`); delete idle consumers older than 1 h. A reaper marks `debate_runs` `RUNNING` > 30 min as `FAILED` with a reason. Clean the 2 stuck messages and 2 stuck runs once.
- **Tests:** kill a worker mid-message → message reclaimed by another worker; poison message → DLQ after 3 tries.
- **Done when:** 0 `RUNNING` runs older than 30 min; consumer count ≤ replica count; a killed worker's message completes within 10 min.
- **Effort:** M.

### B5. Bounded, declared decision price (F-M14)
- **Owner:** **Backend Architect**. **Review:** code-modernization:security-auditor.
- **Change:** in `services/debate_worker/main.py:190-205` read the price from the same live-quote source as the orchestrator with a max age (for example 10 s); remove the direct `api.binance.com` fallback; if stale, mark the decision `NO_PRICE` instead of guessing.
- **Tests:** stale quote → `NO_PRICE`; no outbound HTTP from the worker (mocked transport asserts no calls).
- **Effort:** S.

---

## Phase C: CI and hygiene

### C1. CI that gates merges (F-H4, recommendation 6) — first item of the plan
- **Owner:** **Platform Engineer** (workflow), **Test Automation Engineer** (Playwright smoke). **Review:** Code Reviewer; flakes triaged by **Test Results Analyzer**.
- **Change:** `.github/workflows/ci.yml` with jobs: `ruff` on changed files (full run is informational until C2), `pnpm typecheck`, `pnpm lint`, `uv run pytest tests -q` with Postgres 16 + pgvector and Redis 7 service containers and a throwaway DB, Alembic upgrade from empty, and a Playwright smoke subset against a built web with the API stubbed. Cache uv and pnpm. Enable branch protection on `main` requiring these checks (**Operator** enables it in GitHub settings).
- **Done when:** a PR with a failing test cannot merge; median CI time under 15 minutes.
- **Effort:** M.

### C2. Ruff clean-up (Low)
- **Owner:** **code-simplifier:code-simplifier**. **Review:** Code Reviewer.
- **Change:** fix the 495 findings in mechanical batches by rule (F401, E402, F841, the bare `except`), then make ruff blocking on the whole tree.
- **Effort:** S.

### C3. Remove test pollution from the dev DB (F-M9, part of F-H2)
- **Owner:** **Database Optimizer**. **Review:** Senior Project Manager (operator-visible data).
- **Change:** archive the 11 active `test-spec-*` specs; clear the 80+ `execution_kill_switch` test rows (ACC-RACE-*, ACC-CRASH-*), un-latching only test accounts; add a guard so specs named `test-*` cannot be ACTIVE outside test DBs; find which tests wrote to `cryptonite` and fix their DB fixture.
- **Done when:** 0 active test specs; 0 test kill-switch rows; a test that targets `cryptonite` fails fast.
- **Effort:** S.

### C4. Documentation drift (F-M13)
- **Owner:** **DX Engineer**. **Review:** Senior Project Manager.
- **Change:** update `HANDOFF.md` "Resume here", the `AUDIT_STATUS.md` header, README Status (deploy hold, evaluation v2), and link this plan and the review. Add a "docs updated" checkbox to the PR template.
- **Effort:** S.

---

## Phase D: Signal pipeline

### D1. Fresh-only enqueue and bounded stream (F-H2)
- **Owner:** **Backend Architect**. **Review:** Code Reviewer.
- **Change:** in `services/scanner/cryptonite_scanner_service/worker.py:478-488` keep evaluating history for `signal_candidates`, but enqueue only candidates whose bar closed within one bar of now. `xadd` with `MAXLEN ~ 10000` (`:607`). Move the enqueue into an outbox row written in the same transaction (`:600-615`), relayed by the existing outbox service. Trim `stream:debate_queue` once.
- **Tests:** restart after a backfill enqueues 0 historical candidates; failure between DB write and enqueue does not lose the candidate.
- **Done when:** ≥ 95% of enqueued candidates are fresh over 3 days.
- **Effort:** S–M.

### D2. Events, not states (F-H2, §5.2.1)
- **Owner:** **AI Engineer**. **Review:** Code Reviewer.
- **Change:** support crossover conditions (`lookback ≥ 1`: `crosses_above`, `crosses_below`) in the scanner condition grammar; rewrite the seed specs (for example `macd-momentum-shift` → `macd_hist crosses above 0 AND close > ema_50`). Deduplicate candidates per (instrument, tf, bar) across specs. Add a max-active-specs guard.
- **Tests:** a trending fixture produces one candidate per crossover, not one per bar.
- **Done when:** candidate volume drops 10–50× against the 10-02..10-04 baseline.
- **Effort:** M.

### D3. Structure-based stop and target (F-H2, §5.2.2, Q8)
- **Owner:** **AI Engineer**. **Review:** Code Reviewer, **Experiment Tracker** (checks R:R distribution).
- **Change:** replace the fixed 2×/4× ATR in `packages/scanner/cryptonite_scanner/models.py:42-45` with swing low/high or Donchian stop, and the next higher-timeframe level (ATR fallback) for the target. Persist stop, target and R:R on the candidate. Restrict automatic triggering to the timeframes chosen in Q8.
- **Tests:** R:R varies across a fixture set; the R3.1 "R:R < 1.5" rule now fires on some candidates.
- **Done when:** R:R has a real distribution in production (not a constant 2.00).
- **Effort:** M.

### D4. Refuse what cannot be traded (F-H7, UI item 3)
- **Owner:** **Backend Architect**. **Review:** Code Reviewer.
- **Change:** an `ingest_universe` source of truth (from the ingest config at `infra/docker-compose.yml:71-75`, published to Redis or a table). Mandate creation (`routes/ai.py:278-300`, `schemas/mandate.py:174-188`) and strategy instance creation return 422 naming the reason when the symbol or timeframe has no feed. Replace `strategy_source_hash = 'dummy_hash'` (`server.py:709`) with the real source hash. Archive the XRP mandate and the SOL 1m / BTC 15m instances (Q5). Approvals are idempotent per decision (stops the 3-in-6-seconds double submit).
- **Tests:** 422 on un-fed symbol and timeframe; double approve → one order.
- **Effort:** S–M.

### D5. Honest risk-limits form (F-H8, UI item 6)
- **Owner:** **Frontend Developer** + **Backend Architect** (expose effective limits). **Review:** Code Reviewer.
- **Change:** the orchestrator publishes `risk:effective_limits` with its version; the API exposes it; `RiskLimitsForm.tsx:96` shows "Saved. Not yet enforced (orchestrator vX)" until the effective limits reflect the override, then "Enforced since <time>".
- **Tests:** Playwright: saved-but-not-enforced state; enforced state after the stub reports it.
- **Effort:** S.

### D6. Universe-aware price precision (F-M7)
- **Owner:** **Backend Architect**. **Review:** Code Reviewer.
- **Change:** read tick and step size from `instruments` instead of `price_precision=2` in `worker.py:487`, `engine.py:66,135-142` and `server.py:1340-1343`.
- **Tests:** a $0.50 instrument keeps its precision through scanner and orchestrator.
- **Effort:** S.

---

## Phase E: Deploy and evaluation v2

### E1. Deploy the held services (recommendation 7, D5)
- **Owner:** **Platform Engineer**, run sheet by **Senior Project Manager**; **Operator** approves. **Review:** Reality Checker.
- **Precondition:** Phase B and D1–D5 merged; CI green; a fresh backup (A3) taken.
- **Change:** back up, apply migrations up to head, rebuild and recreate orchestrator, scanner, debate_worker ×3 and learner. Sequencing (B5 cross-service contract): rebuild and recreate the ingest producer with or before the debate worker, so `quote:tick:*` snapshot keys exist before any B5 reader deploys — without the producer every entry records fail-closed `NO_PRICE`. Verify: On-Chain Analyst gone from full debate (R3.2), rule triage active (R3.1), R7.3, F4 and F7 live, mark-to-market running, and `quote:tick:<instrument>` keys present with fresh `arrival_ns`.
- **Done when:** a post-deploy checklist passes with evidence (container image dates, a debate without the On-Chain Analyst, a triage hard reject, moving unrealized PnL).
- **Effort:** S.

### E2. Thesis-bound exits (F-H1, §5.2.3, feature 7)
- **Owner:** **AI Engineer** (rules), **Backend Architect** (exit monitor). **Review:** Code Reviewer, **Experiment Tracker**.
- **Change:** carry the candidate's stop, target and a time stop (3×–6× the signal timeframe) onto the position; `exit_monitor.py:46-49` enforces them in addition to the 5% catastrophe stop and the trail. Exit debates fire on the stated triggers; add a test that the exit-debate stream is actually consumed.
- **Tests:** a position closes on target, on stop and on time stop, each recorded with its exit reason.
- **Done when:** closed trades appear at a measurable rate (≥ 1 per day on the shadow book once F4 is live).
- **Effort:** M.

### E3. Evaluation v2 start and gate honesty (F-H1, Q1–Q3)
- **Owner:** **Experiment Tracker**, gate code by **Backend Architect**. **Review:** Reality Checker.
- **Change:** stamp `evaluation_v2_start` (config and ADR 0004); gate metrics read only that window. Apply the D4 decision (breaches with `excluded_reason`). `honest_paper` reports **not passed** when unmeasured (`autonomy.py:112,216-221`). Sortino shows "n/a (0 closed trades)".
- **Tests:** gate tests for unmeasured `honest_paper`, excluded breaches and the window boundary.
- **Done when:** `gate_status.py` shows the v2 window and no check passes on missing data.
- **Effort:** S.

---

## Phase F: Evidence layer

### F1. Counterfactual outcome labels for every candidate (feature 2, §5.3.2)
- **Owner:** **AI Engineer**. **Review:** Experiment Tracker.
- **Change:** a scheduled labeller labels every candidate (fresh, gate-rejected, triage-rejected, NO_TRADE, traded) at 1h, 4h and 24h, plus "+1R hit before −1R", using the existing labeller (`outcomes.py:33-50`). Table `candidate_outcomes`.
- **Done when:** weekly report of base rate, triage precision/recall versus outcome and debate lift, with confidence intervals.
- **Effort:** S–M.

### F2. Durable funnel events (F-M3, feature 3)
- **Owner:** **Database Optimizer** (table), **Backend Architect** (writers). **Review:** Code Reviewer.
- **Change:** a `gate_events` table (candidate id, stage, decision, reason, timestamp) written next to the Redis counters in `gate.py`; export the existing `debate_gate:*` keys before they expire.
- **Effort:** S.

### F3. Funnel dashboard (feature 3, UI item 4)
- **Owner:** **Data Visualization Engineer**, built by **Frontend Developer**. **Review:** UI Designer.
- **Change:** a funnel strip in Mission Control (candidates → fresh → triaged → debated → approved → filled → closed) with stage conversion and per-stage lift from F1.
- **Effort:** M.

### F4. Shadow book (feature 1, §5.5, Q3)
- **Owner:** **Experiment Tracker** (design), **Backend Architect** (implementation). **Review:** Reality Checker; **code-modernization:security-auditor** confirms it cannot reach the dispatcher.
- **Change:** every BUY/SELL verdict is paper-filled automatically in a separate, non-mandate ledger with the same fees and exits (E2), independent of approval and headroom. It never goes through the order path. The gate report shows both books.
- **Done when:** ≥ 100 shadow trades and an expectancy confidence interval after fees in the gate report.
- **Effort:** M.

### F5. Sequential test for the gate (§5.4.3)
- **Owner:** **Experiment Tracker**. **Review:** AI Engineer.
- **Change:** add an SPRT or Bayesian sequential test on shadow-book expectancy after fees, reporting "edge", "no edge" or "continue". Informational at first; promote to a gate check after an ADR update.
- **Effort:** S–M.

---

## Phase G: UI/UX

### G1. Truthful mode banner (F-M12, UI item 1)
- **Owner:** **UI Designer** (copy and states), **Frontend Developer**. **Review:** Evidence Collector (screenshots of each state).
- **Change:** replace the hardcoded "ZERO TRADING AUTHORITY (READ ONLY)" (`apps/web/src/app/page.tsx:336`) and "SIMULATION MODE (RISK-FREE)" (`AIBrainWorkspace.tsx:114`) with a banner computed from state, for example "PAPER · 2 mandates at L1 · approvals required" or "PAPER · kill switch latched".
- **Effort:** S.

### G2. Telegram approval card v2 (F-M11, UI item 2)
- **Owner:** **UI Designer**, **Backend Architect** (`services/notifier/cryptonite_notifier/render.py:19-31`). **Review:** UX Researcher.
- **Change:** side, size in $ and %, decision price vs live price, stop and worst-case $ loss, mandate room after the trade, one-line thesis, last 3 same-side outcomes (`similar_decisions`), and a deep link to the trace.
- **Tests:** render snapshot tests; length within Telegram limits.
- **Effort:** S–M.

### G3. Mission Control layout (UI item 4)
- **Owner:** **UI Designer**, **Frontend Developer**. **Review:** UX Researcher.
- **Change:** put the funnel strip (F3), gate checklist with ETA, heartbeats, today's spend and open PnL in the empty two-thirds; make the kill switch smaller with a confirm step, sticky but secondary.
- **Effort:** M.

### G4. Units and formatting (UI item 5, Low raw quantities)
- **Owner:** **Frontend Developer**. **Review:** Code Reviewer.
- **Change:** drawdown as "0.19%"; quantities formatted to step size (`BrainContextRail.tsx:96`); hide FLAT perp rows; Sortino "n/a (0 closed trades)"; calibration chart greyed out until n ≥ 20.
- **Effort:** S.

### G5. Pre-approval feasibility (UI item 3)
- **Owner:** **Frontend Developer**, uses D4's API. **Review:** Code Reviewer.
- **Change:** disable Approve with a reason when there is no live quote or feed; single-flight approve buttons.
- **Effort:** S.

### G6. First-run onboarding checklist (UI item 7)
- **Owner:** **UX Architect** (flow), **Frontend Developer**, copy by **DX Engineer**. **Review:** UX Researcher.
- **Change:** create operator and TOTP → verify Telegram (approve, veto, kill-switch test) → create a mandate on a supported instrument → see the first candidate. Each step reads real state.
- **Effort:** M.

### G7. Mobile approve page (UI item 8)
- **Owner:** **UX Architect**, **Frontend Developer**. **Review:** code-modernization:security-auditor (session and TOTP), Accessibility Auditor.
- **Change:** a responsive `/approve/:id` behind the same session and TOTP, showing the G2 card in full.
- **Effort:** M.

### G8. Live accessibility re-check (UI item 9)
- **Owner:** **Accessibility Auditor**. **Review:** Evidence Collector.
- **Change:** run axe and keyboard checks against the running web (after A4); fix contrast of the zinc-on-black secondary text in Mission Control.
- **Effort:** S.

### G9. Usability test with the operator
- **Owner:** **UX Researcher**. **Review:** Product Manager.
- **Change:** a scripted session covering approval (web and Telegram), kill switch, mandate creation and reading the gate; log approval time and errors; feed fixes back into G1–G7.
- **Effort:** S.

### G10. Daily digest v2 (feature 12)
- **Owner:** **Backend Architect** (notifier), **UI Designer** (layout). **Review:** UX Researcher.
- **Change:** a morning Telegram digest: PnL vs BTC hold, funnel, health, spend, gate ETA.
- **Effort:** S.

---

## Phase H: Backtest realism and research

### H1. Spread and slippage recorder (feature 11, F-M4)
- **Owner:** **Backend Architect** (ingest book tickers into spread histograms). **Review:** AI Engineer.
- **Effort:** S.

### H2. Realistic cost model (F-M4, §5.4.2)
- **Owner:** **AI Engineer**. **Review:** Experiment Tracker.
- **Change:** slippage = half the recorded spread + impact ∝ √(size/volume) in `packages/backtester/cryptonite_backtester/execution_profiles.py:41-51`; `adverse_slippage` charges fees; a stress profile at 2× fees and 3× spread.
- **Done when:** backtest-to-paper fill price error under 2 bps median.
- **Effort:** M.

### H3. Pipeline replay backtester (feature 5, recommendation 9)
- **Owner:** **Software Architect** (design), **AI Engineer** (implementation). **Review:** Reality Checker.
- **Change:** replay scanner + D3 stops/targets + E2 exits over 200 days of catalog bars with conservative costs; optional triage replay on a sample with local Gemma (free). Compare against BTC buy-and-hold.
- **Done when:** a published baseline; specs that cannot beat fees are archived.
- **Effort:** L.

### H4. First real research-loop runs (F-M8)
- **Owner:** **AI Engineer**. **Review:** Experiment Tracker.
- **Change:** run the research loop end to end; add purged and embargoed CV (§5.4.3); keep DSR and the single-use holdout.
- **Done when:** ≥ 5 `research_reports` rows including ≥ 1 holdout-scored candidate, or a written "no edge found".
- **Effort:** M.

### H5. Lake compaction (Low, §5.1.3)
- **Owner:** **Database Optimizer**. **Review:** Performance Benchmarker.
- **Change:** a daily job compacting to one parquet file per symbol per day (10,081 files today, 546 for one BTC day), atomic swap, quarantine on failure.
- **Effort:** S.

---

## Phase I: AI accuracy

### I1. Symmetric debate (F-M5, §5.3.4)
- **Owner:** **Multi-Agent Systems Architect**, implemented by **AI Engineer**. **Review:** Experiment Tracker.
- **Change:** equal turns with alternating opener, PM speaks last (`main.py:276-284`); rename exit-debate personas to match what they argue; each side states probabilities for stop-hit vs target-hit, scored with Brier.
- **Validation:** A/B in F2 shadow mode on fresh candidates, judged by F1 labels.
- **Effort:** M.

### I2. Calibration on trade outcomes (§5.3.5)
- **Owner:** **AI Engineer**, charts by **Data Visualization Engineer**. **Review:** Experiment Tracker.
- **Change:** report Brier and log-loss with ECE; calibrate PM conviction against stop/target/time outcome (E2), not raw 24 h return.
- **Effort:** S–M.

### I3. Meta-label challenger (feature 8, §5.3.3)
- **Owner:** **AI Engineer**. **Review:** Experiment Tracker, Reality Checker before promotion.
- **Change:** gradient-boosted classifier on brief features predicting "+1R before −1R", trained on F1 labels with purged CV; runs in F2 shadow mode beside Gemma triage; promoted only if Brier after fees is better.
- **Effort:** M.

### I4. Trade journal and post-mortem (feature 9)
- **Owner:** **Frontend Developer** + **Backend Architect**. **Review:** UX Researcher.
- **Change:** trace → fill → exit → lesson view with operator notes; operator notes feed reflections.
- **Effort:** M.

### I5. Distilled triage model (carry-over)
- **Owner:** **AI Engineer**. **Depends:** A6 (Ollama 0.35.1), B1 (regenerated eval sets). **Review:** Reality Checker.
- **Change:** re-evaluate the `triage-lora` adapter on the aligned eval sets; ship through F2 shadow mode only if it matches or beats Gemma.
- **Effort:** S–M.

### I6. Triage confidence semantics (Low)
- **Owner:** **AI Engineer**. **Review:** Code Reviewer.
- **Change:** define `confidence` as probability the candidate is worth debating; enforce consistency with `worth_debate` in the schema.
- **Effort:** S.

---

## Phase J: Optimization, observability, security

### J1. Incremental scanner (§6)
- **Owner:** **Performance Benchmarker** (profile), **Backend Architect** (change). **Review:** Code Reviewer.
- **Change:** evaluate only the latest closed bar per timeframe; cache enriched frames. Measure CPU before and after.
- **Effort:** M.

### J2. Retention and partitioning (§6)
- **Owner:** **Database Optimizer**. **Review:** Performance Benchmarker.
- **Change:** partition or TTL `signal_candidates`, `ai_mandate_equity` and `gate_events`; nightly `VACUUM ANALYZE`; alert on table size growth (prevents a repeat of the 21 GB `scan_runs` incident).
- **Effort:** M.

### J3. Metrics stack (F-M10)
- **Owner:** **Platform Engineer**, panels by **Data Visualization Engineer**. **Review:** Code Reviewer.
- **Change:** Prometheus with node, Postgres and Redis exporters; Grafana panels for funnel, latencies, stream lag, spend and host health; alert rules routed to ntfy.
- **Effort:** M.

### J4. Security hygiene (Low)
- **Owner:** **code-modernization:security-auditor**. **Review:** Backend Architect.
- **Change:** move the 50 `*_dev_pass` values in compose to `.env` with generated values; ingest connects with a least-privilege role instead of the owner `cryptonite` (`compose:66`); document the TOTP-over-Telegram trade-off and offer the G7 page as the stronger path.
- **Effort:** S–M.

---

## Phase K: Later (gated on evidence)

| ID | Item | Owner | Gate |
|---|---|---|---|
| K1 | Instrument onboarding (symbol → backfill → ingest → scanner universe → precision) (feature 6) | Backend Architect | D4, D6 done |
| K2 | Real on-chain or derivatives-flow source (F6), then re-add the analyst (feature 13) | AI Engineer + Software Architect | A source with provenance exists |
| K3 | Volatility-targeted sizing inside the mandate cap (feature 10) | AI Engineer; Review: Reality Checker | Shadow book live |
| K4 | Portfolio beta budget (F4 live) | Backend Architect | E1 done |
| K5 | Live-venue readiness checklist: Binance demo reconciliation soak, latency budget, secrets vault, separate hosts | Software Architect + security-auditor | Shadow book shows a positive expectancy lower bound after fees (Q7) |

---

## 3. Schedule and dependencies

```
Week 1   C1 ─┬─ A2, A4, A5, A3 ───────────────┐
             ├─ B1 ─ (eval sets) ─────────────┤
             ├─ B2 ─ B3                       │
             └─ C3, C4, A1a (operator)        │
Week 2   B4, B5, D1 ─ D2 ─ D3, D4, D5, D6     │
         ── E1 (deploy) ─ E3 (v2 start) ─ E2 ─┘
Week 3–6  F1 ─ F2 ─ F3 ; F4 ─ F5 ; G1–G10 ; H1 ─ H2
Week 4–8  H3, H4, H5 ; A1b if needed
Week 6–10 I1–I6 ; J1–J4
Later     K1–K5
```

Critical path: **C1 → B1/B2 → D1–D3 → E1 → E2/E3 → F4 → F5**. Everything else runs in parallel around it.

## 4. Phase exit reviews

At the end of each phase the **Senior Project Manager** collects evidence and the **Reality Checker** gives a PASS / NEEDS WORK verdict against the phase gate in §2. A phase marked NEEDS WORK does not block parallel phases, except that **E1 requires A3, B and C1 to have passed**.

## 5. Traceability: every finding to an item

| Finding / recommendation | Item(s) |
|---|---|
| F-C1 bar shift | B1 |
| F-C2 inverted verdicts | B2 |
| F-C3 host | A1a, A1b |
| F-H1 L2 unreachable, hold | E1, E2, E3, Q1, Q2 |
| F-H2 signals | D1, D2, D3, C3 |
| F-H3 marks | B3 |
| F-H4 CI | C1 |
| F-H5 backups | A3 |
| F-H6 queue | B4 |
| F-H7 un-fed mandates | D4, G5 |
| F-H8 F7 copy | D5 |
| F-M1 no external alert, web down | A2, A4 |
| F-M2 launcher | A5 |
| F-M3 funnel TTL | F2 |
| F-M4 slippage | H1, H2 |
| F-M5 debate bias | I1 |
| F-M6 verdict vocabulary | B2 |
| F-M7 precision | D6 |
| F-M8 research loop | H4 |
| F-M9 test pollution | C3 |
| F-M10 observability | A4, J3 |
| F-M11 Telegram card | G2 |
| F-M12 mode labels | G1 |
| F-M13 docs drift | C4 |
| F-M14 decision price | B5 |
| Low: ruff | C2 |
| Low: small files | H5 |
| Low: dev passwords, ingest owner role, TOTP via Telegram | J4 |
| Low: triage confidence | I6 |
| Low: raw quantities | G4 |
| UI 1–9 | G1, G2, G5, G3/F3, G4, D5, G6, G7, G8 |
| Features 1–13 | F4, F1, F2/F3, A2, H3, K1, E2, I3, I4, K3, H1, G10, K2 |
| §5.4.3 statistics | F5, H4 |
| §6 optimization table | B4, J1, B3, J2, (cost → F1), A4, A1a/A1b |
| Carry-overs (Telegram checks, sudo, Ollama, distilled model) | A6, I5 |
| Open questions 1–10 | §1 |
