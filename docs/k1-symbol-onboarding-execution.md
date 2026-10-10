# K1 Symbol Onboarding Execution Ledger

> [!IMPORTANT]
> **Source Reference**: `/home/samx/Documents/projects/cryptonite/symbol-onboarding-plan-v2.md` remains the authoritative specification. This ledger records execution baseline measurements, live database inventory, PR readiness status, and operational guardrails. It does not modify source plan requirements or mutate live system state.

---

## 1. System Baseline & Environment
- **Git Branch / Base**: `origin/main` at HEAD [`fbc80f5`](https://github.com/sayegh9/cryptonite/commit/fbc80f5).
- **Database Schema**: Alembic Revision **`041`** (`DB041`).
- **Target Database Container**: `infra-postgres-1` / Database: `cryptonite` (`psql -U cryptonite -d cryptonite`).
- **Live Ingest Container ID**: `fd74d75df5c0` (2026-10-09 baseline). **[2026-10-10] now `23b96abe86d1`** (healthy; recreated for PR #45, `docker compose ... up -d --no-deps --force-recreate ingest`).
- **Live Ingest Image Digest**: `sha256:bee2d23192d45ba2ea316a1c53b2211987fdc703e1532b24b413a49c3b864d34` (unchanged by PR #45, which is config-only).
- **Live Ingest Feed Timeframes**: 2026-10-09 baseline `1m,1h`. **[2026-10-10] now `1m,15m,1h,4h`** after PR #45 (merge `893fb49`).
- **Redis Stream State**: `cryptonite:stream:closed_bar_committed`, consumer lag = 0 across all groups (`scanner-worker`, `api-fanout`).
- **E1 Core (live)**: main `fbc80f5` with DB `041`, D5 web, and the scanner backlog fast-forward are deployed (E1 deploy record, 2026-10-09). **[2026-10-10] PR #45** (`INGEST_TIMEFRAMES=1m,15m,1h,4h`) is merged as `893fb49` and deployed to ingest. Evidence: `docs/e1-feed-timeframes-deploy-2026-10-10.md` (commit `c55b5b7`; docs PR #57 merged as `99adceb`, so the evidence doc is on `main`).
- **Migration state**: live DB is at `041`. Migration `042` is in PR #56, which was merged into the PR #53 feature branch on 2026-10-10 (feature-branch merge only). It has NOT been applied to any database.

---

## 2. Phase 0 Live DB Inventory & Baseline Measurements

### 2.1 Live DB Counts & Asset Inventory
Re-validated directly against live container `infra-postgres-1` database `cryptonite`:

| Category | Item / Table | Count / Details |
|---|---|---|
| **Active Strategy Specs** | `strategy_specs` | **5 active specs** (all explicitly configured for `BTCUSDT` / `ETHUSDT`) |
| **Mandates** | `mandates` | **3 mandates** total (includes ACTIVE `WLDUSDT` mandate and ARCHIVED `XRPUSDT` mandate) |
| **Instruments** | `instruments` | **4 active rows**: `BTCUSDT`, `ETHUSDT`, `WLDUSDT`, `XRPUSDT` |
| **Ingest Batches** | `ingest_batches` | **`BTCUSDT`**: 8,301 1m / 140 1h<br>**`ETHUSDT`**: 8,290 1m / 140 1h |
| **Ingest Checkpoints** | `ingest_checkpoints` | **4 rows**: `BTCUSDT` (1m, 1h), `ETHUSDT` (1m, 1h) |
| **Paper Positions** | `paper_positions` | **2 OPEN** (`BTCUSDT`), **3 CLOSED** (`BTCUSDT`) |
| **Paper Orders** | `paper_orders` | **13 rows**: `BTCUSDT` (6), `BTC-PERP` (4), `ETHUSDT` (3) |
| **Strategy Instances** | `strategy_instances` | **2 old instances** (both `STOP`) |

> [!NOTE]
> The active `WLDUSDT` mandate blocks blind quarantine/cleanup of legacy symbol references in Phase 0.

### 2.2 Ingest Bar Payload Measurements
Measured per-bar JSON size over a 7-day sample window:
- **`BTCUSDT` 1m**: 792 Bytes / bar
- **`ETHUSDT` 1m**: 785 Bytes / bar
- **`BTCUSDT` 1h**: 657 Bytes / bar
- **`ETHUSDT` 1h**: 653 Bytes / bar

### 2.3 Section 0.3 Combined WebSocket URL Spike
Tested via throwaway separate client connection using the exact ingest combined stream URL pattern (`wss://stream.binance.com:9443/stream?streams=...` from `stream.py`):
- **SUBSCRIBE Latency** (`SOLUSDT@kline_1m`): **0.326s** ACK confirmation.
- **First Event Latency**: **1.296s** to first stream payload.
- **Clean UNSUBSCRIBE**: Verified response and connection teardown.
- **Impact**: Zero mutation on live ingest WebSocket connection or production Redis stream.

---

## 3. Storage Assessment & Onboarding Guardrails

### 3.1 Storage Measurement
- **Available Disk Space (dated snapshots, `/dev/nvme0n1p3`)**:
  - 2026-10-09: **13 GB** available (95% used). Snapshot from the Phase 0 baseline.
  - 2026-10-10, before operator expansion: **11 GB** available (96% used).
  - 2026-10-10, after operator expansion (`df -h /`): **259 GiB** available on a 494G root (46% used).
- **Required Storage Floor**: **15 GB** free disk space after estimated growth (§11 Decision 6). The floor is met on current free space. It still must be checked after estimated job growth before any job runs.

### 3.2 Live Onboarding Decision
> [!NOTE]
> **Disk floor (2026-10-10)**: met on current free space (259 GiB). The post-estimate ≥15 GB check is still required per job.
> **Onboarding remains blocked** by the unresolved top-10 decision (§8, item 1), the post-estimate disk check, and A3 (§8, item 3). The E1 / PR #45 release gates (§7.1) were executed on 2026-10-10. **No live symbol onboarding** may take place until all of these pass.

---

## 4. Code & PR Status Ledger

| Component / Feature | Branch / PR | Target Branch | Exact Head SHA | Status | Details |
|---|---|---|---|---|---|
| **E1 Feed Timeframes Fix** | PR #45 (`fix/d3-feed-timeframes`) | `main` | head [`7418022`](https://github.com/sayegh9/cryptonite/commit/741802213705b1484a1288b94a8e5b87cbe94be6); merged as `893fb49` | **MERGED and DEPLOYED (2026-10-10)** | Config change `INGEST_TIMEFRAMES=1m,15m,1h,4h`. Preflight PR #51 was CONDITIONAL GO; its gates were executed: fresh PG/Redis backups, local restore drill OK, ingest `23b96abe86d1` healthy with fresh 15m/4h committed bars. A3 remains open. |
| **K1 Phase 1 Brain Klines Tool** | PR #46 (`feat/k1-brain-klines`) | `main` | [`51eea480`](https://github.com/sayegh9/cryptonite/commit/51eea48011f4c1dbe4a9c05a771b31fb5c3f705b) | **CODE COMPLETE (OPEN)** | Implements `get_klines` read-only tool in Brain. Stacked review PR #50 merged into author branch at `51eea480`. 6 CI checks green. **NOT merged or deployed**. |
| **K1 Execution Ledger** | PR #47 (`docs/k1-execution-ledger`) | `main` | see PR #47 head (not pinned here) | **OPEN (CI green, merge CLEAN)** | Execution ledger tracking implementation status, gates, and metrics. |
| **K1 Phase 1 Web Chart States** | PR #48 (`feat/k1-web-price-chart-states`) | `main` | [`e940279`](https://github.com/sayegh9/cryptonite/commit/e940279d0ec5ef5477daeec4653609804b8ba26e) | **CODE COMPLETE (OPEN)** | Empty and stale card states for `PriceChartCard`. All CI checks green. **NOT merged or deployed**. |
| **D6 Universe Precision** | PR #49 (`feat/d6-universe-precision`) | `main` | [`34e7d78`](https://github.com/sayegh9/cryptonite/commit/34e7d78013d8a70c360da84f2231b67f13b63198) | **OPEN** | Scanner precision handling for symbols under $1. D6 prerequisite. **NOT merged or deployed**. |
| **PR #46 Hardening Review** | PR #50 (`review/k1-get-klines-hardening`) | `feat/k1-brain-klines` | [`8e9ce1e`](https://github.com/sayegh9/cryptonite/commit/8e9ce1e8c8febedf2bd4c6d14aa98aa618e21d2c) | **MERGED** | Stacked review for PR #46 (server time boundary, Redis universe parse/expiry, bounded cache). Merged into author branch via `51eea480`. |
| **E1 Preflight Status Report** | PR #51 (`docs/e1-pr45-preflight`) | `main` | [`9c8f111`](https://github.com/sayegh9/cryptonite/commit/9c8f11161e5c10b4618ed2f8b398b9da27f6e9df) | **OPEN** | Corrected preflight report documenting Conditional GO, rollback baseline, and restore drill requirements. |
| **K1 Phase 2.3 Ingest Rate Limiting** | PR #52 (`feat/k1-ingest-rate-limit-hardening`) | `main` | [`c1cd52ed`](https://github.com/sayegh9/cryptonite/commit/c1cd52edafb0a168326335dd379ffbd050fe3f8d) | **OPEN** | F19 429/418 handling and weight tracking. Stacked review PR #54 merged into branch via `c1cd52ed` (all 6 CI checks green: pytest, e2e-smoke, web, ruff, verify, CodeRabbit). **NOT merged or deployed**. |
| **K1 Phase 2.4 History Coordinator** | PR #53 (`feat/k1-history-coordinator`) | `main` | [`01de154`](https://github.com/sayegh9/cryptonite/commit/01de154101e4bb2e8798bfd3c690186c757c61bf) | **OPEN (BLOCKED; PR #56 MERGED INTO PR #53 BRANCH; PR #53 CI RUNNING)** | `commit_history_batch` and `missing_runs` in `packages/storage`. Independent safety review identified production blockers: F1 span-masked interior missing bars, F2 pooled history reconcile loses history flag, F3 offline no-pool path does not update `ingest_batches`/`ingest_checkpoints`. **NOT production-backfill ready**. PR #56 (head `4a974368`) was merged into this feature branch at 2026-10-10T09:15:25Z; PR #53 head is now `50e248b0`. Exact-head CI for `50e248b0` was still running at the last check (ruff, web, verify, CodeRabbit passed; pytest and e2e-smoke pending), so it is not yet green. PR #53 is still open against `main`. **NOT merged to main, NOT deployed.** |
| **PR #52 Hardening Review** | PR #54 (`review/k1-ingest-rate-limit-hardening`) | `feat/k1-ingest-rate-limit-hardening` | [`dba269d`](https://github.com/sayegh9/cryptonite/commit/dba269d2cdc3e42a542c8de42cf26fa256749f36) | **MERGED** | Stacked review for PR #52 (immediate 418 fail-closed, robust Retry-After date parsing, rate-budget pacing). Merged into PR #52 branch via `c1cd52ed`. |
| **K1 Phase 2.3 Reconnect Gap Recovery (F4)** | PR #55 (`feat/k1-ingest-reconnect-gap-recovery`) | `feat/k1-ingest-rate-limit-hardening` (PR #52 branch) | [`232499e0`](https://github.com/sayegh9/cryptonite/commit/232499e0e8ab992ba6827f5bd55990f0724f6bda) | **OPEN (under independent review)** | Implements F4 reconnect gap recovery and dynamic SUBSCRIBE. CI: six SUCCESS; merge CLEAN. **Implemented; independent review not yet passed; NOT merged, NOT deployed.** |
| **K1 Phase 2.4 History Coordinator Fix** | PR #56 (`review/k1-history-coordinator-hardening`) | `feat/k1-history-coordinator` (PR #53 branch) | [`4a974368`](https://github.com/sayegh9/cryptonite/commit/4a974368fac88e368d2cf46f726a5262bd7262ff) | **MERGED into PR #53 feature branch 2026-10-10T09:15:25Z** | Stacked fix for PR #53 covering F1–F6 per the author's report; adds migration `042` (see §7.3). Independent exact-head review PASS at `4a974368` before the merge. The feature-branch merge is **NOT a main merge**: **NOT merged to main, NOT deployed, migration `042` NOT applied.** |

> [!IMPORTANT]
> **Strict Distinction Between Ready Code & Live Runtime**:
> - Merged stacked reviews (PR #50 into PR #46, PR #54 into PR #52) integrate code into **isolated feature branches only**.
> - **Apart from PR #45 (merged `893fb49`, deployed 2026-10-10), no code from PRs #46–#56 is merged to `main` or deployed to live production containers.**
> - **[2026-10-10]** Ingest runs `893fb49` with the `1m,15m,1h,4h` feed after the PR #45 release. Other services are unchanged from their deployed images (E1 core `fbc80f5`, DB `041`).

---

## 5. Phase-by-Phase Execution Progress & Gap Analysis

| Phase | Description | Implementation Status | Unresolved Gaps / Next Steps |
|---|---|---|---|
| **Phase 0** | Baseline measurements & operator decisions | **MEASUREMENTS COMPLETE (UNRESOLVED TOP-10 DECISION)** | Live DB041 inventory recorded; WebSocket spike latency confirmed; Postgres payload measured; active `WLDUSDT` mandate noted. **GAP**: Operator decision #8 (exact top-10 symbols) remains unresolved; **strictly no live onboarding**. |
| **Phase 1** | Any-symbol chat tool & web chart states | **CODE COMPLETE (OPEN)** | PR #46 (Brain `get_klines`) and PR #48 (web states) tested and validated. Deployment held for release window. |
| **Phase 2.1** | E1 core (live) and 4-timeframe feed (PR #45) | **E1 core LIVE; PR #45 MERGED and DEPLOYED 2026-10-10** | Fresh PG/Redis backups and local restore drill passed (`docs/e1-feed-timeframes-deploy-2026-10-10.md`). A3 remains open. |
| **Phase 2.2** | D6 universe precision | **OPEN** | PR #49 ready for review/merge. |
| **Phase 2.3** | Ingest rate limiting (F19) & gap recovery (F4) | **PARTIALLY COMPLETE (OPEN)** | PR #52 (F19 429/418 handling, header weight tracking) complete and hardened via PR #54.<br>**F4 reconnect gap recovery** is implemented in open PR #55 (head `232499e0`), under independent review. Not merged, not live. |
| **Phase 2.4** | Historical backfill coordinator | **OPEN (BLOCKED; PR #56 MERGED INTO PR #53 BRANCH; PR #53 CI RUNNING)** | PR #53 implemented; independent safety review identified production blockers: F1 catalog file spans mask interior missing bars, F2 pooled history reconcile loses history flag on restart, F3 offline no-pool path does not update `ingest_batches`/`ingest_checkpoints` (the current offline consumer does not update Postgres checkpoints). **NOT production-backfill ready**. Stacked fix PR #56 (head `4a974368`) was merged into the PR #53 feature branch at 2026-10-10T09:15:25Z (PR #53 head `50e248b0`). Exact-head CI for `50e248b0` was still running at the last check, so do not treat it as green. PR #53 is still open against `main`: not merged to main, not live (see §7.3). |
| **Phase 2.5** | Feed check at order time (`require_fed`) | **NOT STARTED** | **GAP**: Not yet implemented. |
| **Phase 3** | Persisted universe (`ingest_universe` migration) | **NOT STARTED** | Migration and recorder integration not started. |
| **Phase 4** | Onboarding job engine S1–S6 | **NOT STARTED** | API routes and ingest job runner not started. |
| **Phase 5** | Legacy partial symbol quarantine | **NOT STARTED** | Blocked on Phase 4 and active `WLDUSDT` mandate resolution. |
| **Phase 6** | Backtest & paper trade verification | **NOT STARTED** | Blocked on Phases 2–5. |
| **Phase 7** | Symbol removal | **NOT STARTED** | Blocked on Phase 4. |

---

## 6. Revised Feed Timeframes & Multi-Timeframe Capacity Model

Under PR #45 (merged `893fb49`, deployed 2026-10-10), `INGEST_TIMEFRAMES` is updated from `1m,1h` to `1m,15m,1h,4h` to support automatic D3 scanner structure triggers.

### 6.1 Per-Symbol Ingest & Storage Metrics
- **Daily Committed Bars**:
  - `1m`: 1,440 bars / day
  - `15m`: 96 bars / day
  - `1h`: 24 bars / day
  - `4h`: 6 bars / day
  - **Total**: **1,566 bars / day per symbol** (vs 1,464 bars / day under `1m, 1h`, a modest **+7.0%** increase).
- **Daily Lake / Catalog File Storage**: ~**1.07 MB / day per symbol** (vs ~1.00 MB / day).
- **Postgres `ingest_batches` Storage**: ~**1.23 MB / day per symbol** (vs ~1.15 MB / day).

### 6.2 180-Day History Backfill Metrics (Job S3)
- **Total Backfill Bars**: 259,200 (1m) + 17,280 (15m) + 4,320 (1h) + 1,080 (4h) = **281,880 bars per symbol** (+7.0% vs 263,520).
- **Total REST Calls**: 260 (1m) + 18 (15m) + 5 (1h) + 2 (4h) = **285 REST calls per symbol** (at 1,000 bars/call).
- **REST Request Weight**: At 2 weight per call, 285 calls consume ~**570 weight**, comfortably below the 3,000 weight/min job threshold (50% of 6,000).
- **Backfill Storage Footprint**: ~**187 MB per symbol** (lake + catalog combined, vs ~175 MB).

### 6.3 WebSocket Stream Utilization
- Kline streams per symbol: 4 (`<sym>@kline_1m`, `<sym>@kline_15m`, `<sym>@kline_1h`, `<sym>@kline_4h`).
- Quote/depth streams per symbol: 1 (`<sym>@bookTicker`).
- **Total streams per symbol**: **5 streams** (vs 3 streams under `1m, 1h`).
- **Universe capacity**: At the 20-symbol cap (§11 Decision 7), total active streams are $20 \times 5 = 100\text{ streams}$, well below Binance's 1,024 stream ceiling per combined connection.

---

## 7. Operational Release Gating & Rollback Controls

### 7.1 E1 / PR #45 Release Gating (PR #51)
- **Preflight Verdict**: **CONDITIONAL GO** (PR #51). **Execution (2026-10-10)**: PR #45 merged `893fb49`; PG backup `cryptonite-2026-10-10T1544Z.dump`; Redis `redis-2026-10-10T1547Z.rdb` with SHA-256 manifest; drill `drill-2026-10-10T1547Z.log` OK; ingest `23b96abe86d1` healthy on `1m,15m,1h,4h`. The 3-night wait stays waived and off-machine restore stays deferred, so neither is a PASS.
- **Required Pre-Deploy Actions**:
  1. Fresh PostgreSQL dump (`pg_dump`) and Redis snapshot (`BGSAVE`) must be generated within the active release window.
  2. Local restore drill must validate backup integrity prior to container restart.
- **Waived / Deferred Items**:
  - 3-night soak/wait waived by operator decision.
  - Off-machine backup transfer deferred to regular operational cycles.
- **Rollback Baseline**:
  - Live Ingest Container: `fd74d75df5c0`
  - Image Digest: `sha256:bee2d23192d45ba2ea316a1c53b2211987fdc703e1532b24b413a49c3b864d34`
  - Config-only rollback: Revert `infra/docker-compose.yml` to `INGEST_TIMEFRAMES=1m,1h` and recreate container `cryptonite_ingest`.

### 7.2 Onboarding Disk Headroom Gate
- Root disk available space (dated snapshots):
  - 2026-10-09: **13 GB** (95% full).
  - 2026-10-10, before operator expansion: **11 GB** (96% full).
  - 2026-10-10, after operator expansion: **259 GiB** free on a 494G root (46% used), per `df -h /`.
- Storage floor: strictly $\ge 15\text{ GB}$ available space after estimated job growth.
- **Gating Status (2026-10-10)**: free-space floor met. The post-estimate check is still required per job. Onboarding also stays blocked by the top-10 decision (§8) and the E1 / PR #45 gates (§7.1).

### 7.3 Migration 042 Deployment Order (PR #56)
- PR #56 adds `infra/migrations/versions/042_ingest_batch_origin_timeframe.py` (revision `042`, `down_revision` `041`).
- Live DB is at `041`. Migration `042` has NOT been applied anywhere.
- Required order, all gated and approved separately:
  1. Exact-head review of PR #56 PASSED (`4a974368`), and PR #56 was merged into the PR #53 feature branch at 2026-10-10T09:15:25Z. That is a feature-branch merge only. Exact-head CI for PR #53 (`50e248b0`) was still running at the last check.
  2. Apply `042` to the live DB as its own approved migration step.
  3. Only then deploy any binary that reads the new columns.
- The PR #56 feature-branch merge does not change the steps above. PR #53 is still open against `main`, not merged to main or deployed, and migration `042` is not applied to any database.

---

## 8. Unresolved Operator Decisions (§11) & Operational Gates
1. **Phase 0 Baseline Measurements vs Decision #8**: Phase 0 empirical measurements (DB counts, schema baseline DB041, storage per bar, WebSocket combined URL spike) are **complete**. However, Operator Decision #8 (exact top-10 symbol list selection for post-quarantine onboarding) remains **unresolved**. **Strictly no live onboarding** is permitted.
2. **Legacy Asset Quarantine Policy**: Presence of active `WLDUSDT` mandate blocks blind quarantine/cleanup of legacy symbol references in Phase 0. Coordinate mandate retirement before legacy tables/checkpoints can be archived.
3. **A3 evidence (OPEN, 2026-10-10 audit)**: 1 of 3 consecutive scheduled nightly drills (`drill-2026-10-08T2345Z`); the 2026-10-10 02:45 EEST slot was missed while the machine was off. No off-machine backup copy or off-machine restore. Redis backups are manual only. Missing evidence is not PASS.
4. **No symbol onboarding and no historical backfill have been performed.**
