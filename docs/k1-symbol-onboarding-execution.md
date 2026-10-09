# K1 Symbol Onboarding Execution Ledger

> [!IMPORTANT]
> **Source Reference**: `symbol-onboarding-plan-v2.md` remains the authoritative specification. This ledger records execution baseline measurements, live database inventory, PR readiness status, and operational guardrails. It does not modify source plan requirements or mutate live system state.

---

## 1. System Baseline & Environment
- **Git Branch / Base**: `origin/main` at HEAD [`fbc80f5`](https://github.com/sayegh9/cryptonite/commit/fbc80f5).
- **Database Schema**: Alembic Revision **`041`** (`DB041`).
- **Target Database Container**: `infra-postgres-1` / Database: `cryptonite` (`psql -U cryptonite -d cryptonite`).

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
- **Required Storage Floor**: **15 GB** free disk space after estimated growth.

### 3.2 Live Onboarding Decision
> [!CAUTION]
> **ONBOARDING BLOCK**: Available disk space (13 GB) is strictly below the 15 GB floor.
> **No live symbol onboarding** may take place until disk space is expanded or cleaned to $\ge 15\text{ GB}$ available free space and release readiness checks pass.

---

## 4. Code & PR Status Ledger

| Component / Feature | Branch / PR | Status | Details |
|---|---|---|---|
| **E1 Feed Timeframes Fix** | PR #45 | **PROPOSED** | Adds `15m` and `4h` timeframes to live ingest configuration. **NOT merged or deployed** to live main/containers. Live stack remains 1m/1h. |
| **K1 Phase 1 Brain Klines Tool** | PR #46 | **CODE COMPLETE (OPEN)** | Implements `get_klines(symbol, interval, limit)` read-only tool in `feat/k1-brain-klines` ([`049086c`](https://github.com/sayegh9/cryptonite/commit/049086c)). All **6 CI checks GREEN** ([#46](https://github.com/sayegh9/cryptonite/pull/46)). **NOT merged or deployed**. |
| **K1 Execution Ledger** | `docs/k1-execution-ledger` | **IN PROGRESS** | Execution ledger documentation (`docs/k1-symbol-onboarding-execution.md`). |

> [!IMPORTANT]
> **Distinction Between Ready Code & Live Runtime**:
> - PR #46 code is complete, tested, and validated in CI.
> - Live runtime services continue running exact production main without `get_klines` or 4-timeframe feeds until PRs are formally merged and deployed by operator procedures.

---

## 5. Unresolved Operator Decisions (§11)
1. **Exact Top-10 Symbol List Selection**: Final selection of onboarding candidates remains an operator decision prior to initiating any live onboarding batch.
2. **Legacy Asset Quarantine Policy**: Presence of active `WLDUSDT` mandate requires coordinated mandate retirement before legacy tables/checkpoints can be archived.
