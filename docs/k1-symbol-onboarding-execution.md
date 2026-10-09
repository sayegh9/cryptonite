# K1 Symbol Onboarding Execution Ledger

> [!IMPORTANT]
> **Source Reference**: `/home/samx/Documents/projects/cryptonite/symbol-onboarding-plan-v2.md` remains the authoritative specification. This ledger records execution baseline measurements, live database inventory, PR readiness status, and operational guardrails. It does not modify source plan requirements or mutate live system state.

---

## 1. System Baseline & Environment
- **Git Branch / Base**: `origin/main` at HEAD [`fbc80f5`](https://github.com/sayegh9/cryptonite/commit/fbc80f5).
- **Database Schema**: Alembic Revision **`041`** (`DB041`).
- **Target Database Container**: `infra-postgres-1` / Database: `cryptonite` (`psql -U cryptonite -d cryptonite`).
- **Live Ingest Container ID**: `fd74d75df5c0` (`fd74d75df5c034c8aac14...`).
- **Live Ingest Image Digest**: `sha256:bee2d23192d45ba2ea316a1c53b2211987fdc703e1532b24b413a49c3b864d34`.
- **Live Ingest Feed Timeframes**: `INGEST_TIMEFRAMES=1m,1h` (production runtime baseline).
- **Redis Stream State**: `cryptonite:stream:closed_bar_committed`, consumer lag = 0 across all groups (`scanner-worker`, `api-fanout`).

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
- **Available Disk Space**: **13 GB** available on `/` (`/dev/nvme0n1p3`, 95% utilization).
- **Required Storage Floor**: **15 GB** free disk space after estimated growth (§11 Decision 6).

### 3.2 Live Onboarding Decision
> [!CAUTION]
> **ONBOARDING BLOCK**: Available disk space (13 GB) is strictly below the 15 GB floor.
> **No live symbol onboarding** may take place until disk space is expanded or cleaned to $\ge 15\text{ GB}$ available free space and release readiness checks pass.

---

## 4. Code & PR Status Ledger

| Component / Feature | Branch / PR | Target Branch | Exact Head SHA | Status | Details |
|---|---|---|---|---|---|
| **E1 Feed Timeframes Fix** | PR #45 (`fix/d3-feed-timeframes`) | `main` | [`7418022`](https://github.com/sayegh9/cryptonite/commit/741802213705b1484a1288b94a8e5b87cbe94be6) | **PROPOSED (CONDITIONAL GO)** | Config change `INGEST_TIMEFRAMES=1m,15m,1h,4h`. Independent preflight report (PR #51) verified Conditional GO. **NOT merged or deployed**. Live runtime remains `1m,1h`. |
| **K1 Phase 1 Brain Klines Tool** | PR #46 (`feat/k1-brain-klines`) | `main` | [`51eea480`](https://github.com/sayegh9/cryptonite/commit/51eea48011f4c1dbe4a9c05a771b31fb5c3f705b) | **CODE COMPLETE (OPEN)** | Implements `get_klines` read-only tool in Brain. Stacked review PR #50 merged into author branch at `51eea480`. 6 CI checks green. **NOT merged or deployed**. |
| **K1 Execution Ledger** | PR #47 (`docs/k1-execution-ledger`) | `main` | [`55d07f1`](https://github.com/sayegh9/cryptonite/commit/55d07f1488c0b3339ab8da2130e7df8c84a8bdb1) | **IN PROGRESS** | Execution ledger tracking implementation status, gates, and metrics. |
| **K1 Phase 1 Web Chart States** | PR #48 (`feat/k1-web-price-chart-states`) | `main` | [`e940279`](https://github.com/sayegh9/cryptonite/commit/e940279d0ec5ef5477daeec4653609804b8ba26e) | **CODE COMPLETE (OPEN)** | Empty and stale card states for `PriceChartCard`. All CI checks green. **NOT merged or deployed**. |
| **D6 Universe Precision** | PR #49 (`feat/d6-universe-precision`) | `main` | [`34e7d78`](https://github.com/sayegh9/cryptonite/commit/34e7d78013d8a70c360da84f2231b67f13b63198) | **OPEN** | Scanner precision handling for symbols under $1. D6 prerequisite. **NOT merged or deployed**. |
| **PR #46 Hardening Review** | PR #50 (`review/k1-get-klines-hardening`) | `feat/k1-brain-klines` | [`8e9ce1e`](https://github.com/sayegh9/cryptonite/commit/8e9ce1e8c8febedf2bd4c6d14aa98aa618e21d2c) | **MERGED** | Stacked review for PR #46 (server time boundary, Redis universe parse/expiry, bounded cache). Merged into author branch via `51eea480`. |
| **E1 Preflight Status Report** | PR #51 (`docs/e1-pr45-preflight`) | `main` | [`d9d8383`](https://github.com/sayegh9/cryptonite/commit/d9d8383) | **OPEN** | Corrected preflight report documenting Conditional GO, rollback baseline, and restore drill requirements. |
| **K1 Phase 2.3 Ingest Rate Limiting** | PR #52 (`feat/k1-ingest-rate-limit-hardening`) | `main` | [`c1cd52ed`](https://github.com/sayegh9/cryptonite/commit/c1cd52edafb0a168326335dd379ffbd050fe3f8d) | **OPEN** | F19 429/418 handling and weight tracking. Stacked review PR #54 merged into branch via `c1cd52ed` (CI rerunning). **NOT merged or deployed**. |
| **K1 Phase 2.4 History Coordinator** | PR #53 (`feat/k1-history-coordinator`) | `main` | [`01de154`](https://github.com/sayegh9/cryptonite/commit/01de154101e4bb2e8798bfd3c690186c757c61bf) | **OPEN (UNDER REVIEW)** | `commit_history_batch` and `missing_runs` in `packages/storage`. Under independent safety review by OpenCode. **NOT merged or deployed**. |
| **PR #52 Hardening Review** | PR #54 (`review/k1-ingest-rate-limit-hardening`) | `feat/k1-ingest-rate-limit-hardening` | [`dba269d`](https://github.com/sayegh9/cryptonite/commit/dba269d2cdc3e42a542c8de42cf26fa256749f36) | **MERGED** | Stacked review for PR #52 (immediate 418 fail-closed, robust Retry-After date parsing, rate-budget pacing). Merged into PR #52 branch via `c1cd52ed`. |

> [!IMPORTANT]
> **Strict Distinction Between Ready Code & Live Runtime**:
> - Merged stacked reviews (PR #50 into PR #46, PR #54 into PR #52) integrate code into **isolated feature branches only**.
> - **Zero code from PRs #45–#54 is merged to `main` or deployed to live production containers.**
> - Live runtime services continue running exact production main (`fbc80f5`) with `1m,1h` feed until operator release procedures are executed.

---

## 5. Phase-by-Phase Execution Progress & Gap Analysis

| Phase | Description | Implementation Status | Unresolved Gaps / Next Steps |
|---|---|---|---|
| **Phase 0** | Baseline measurements & operator decisions | **COMPLETE** | Live DB041 inventory recorded; WebSocket spike latency confirmed; Postgres payload measured; active `WLDUSDT` mandate noted. |
| **Phase 1** | Any-symbol chat tool & web chart states | **CODE COMPLETE (OPEN)** | PR #46 (Brain `get_klines`) and PR #48 (web states) tested and validated. Deployment held for release window. |
| **Phase 2.1** | E1 release & 4-timeframe feed | **PROPOSED (CONDITIONAL GO)** | PR #45 ready. Live deploy requires fresh DB/Redis backup and local restore drill (PR #51). |
| **Phase 2.2** | D6 universe precision | **OPEN** | PR #49 ready for review/merge. |
| **Phase 2.3** | Ingest rate limiting (F19) & gap recovery (F4) | **PARTIALLY COMPLETE (OPEN)** | PR #52 (F19 429/418 handling, header weight tracking) complete and hardened via PR #54.<br>**GAP**: **Reconnect gap recovery (F4)** is NOT yet implemented. |
| **Phase 2.4** | Historical backfill coordinator | **OPEN (UNDER REVIEW)** | PR #53 implemented; independent safety review in progress (`01a12214-3ea9-7743-b5e3-d0305c326596`). |
| **Phase 2.5** | Feed check at order time (`require_fed`) | **NOT STARTED** | **GAP**: Not yet implemented. |
| **Phase 3** | Persisted universe (`ingest_universe` migration) | **NOT STARTED** | Migration and recorder integration not started. |
| **Phase 4** | Onboarding job engine S1–S6 | **NOT STARTED** | API routes and ingest job runner not started. |
| **Phase 5** | Legacy partial symbol quarantine | **NOT STARTED** | Blocked on Phase 4 and active `WLDUSDT` mandate resolution. |
| **Phase 6** | Backtest & paper trade verification | **NOT STARTED** | Blocked on Phases 2–5. |
| **Phase 7** | Symbol removal | **NOT STARTED** | Blocked on Phase 4. |

---

## 6. Revised Feed Timeframes & Multi-Timeframe Capacity Model

Under proposed PR #45, `INGEST_TIMEFRAMES` is updated from `1m,1h` to `1m,15m,1h,4h` to support automatic D3 scanner structure triggers.

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
- **Preflight Verdict**: **CONDITIONAL GO**.
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
- Root disk available space: **13 GB** (95% full).
- Storage floor: strictly $\ge 15\text{ GB}$ available space after estimated job growth.
- **Gating Status**: **HARD BLOCK**. Live onboarding is prohibited until disk expansion or cleanup restores $\ge 15\text{ GB}$ headroom.

---

## 8. Unresolved Operator Decisions (§11)
1. **Exact Top-10 Symbol List Selection**: Operator specified "top 10 symbols" for post-quarantine re-onboarding (§11 Decision 8); exact symbol identifiers must be approved before initiating live onboarding.
2. **Legacy Asset Quarantine Policy**: Presence of active `WLDUSDT` mandate blocks blind quarantine/cleanup of legacy symbol references in Phase 0.
