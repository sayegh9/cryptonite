# E1 Live Release Preflight & Rollback Readiness Report (PR #45)

**Audit Timestamp:** 2026-10-09T21:32:00+03:00
**Task:** `01a121f2-4d76-78b2-bb20-2a79765c29b9`
**Auditor:** Antigravity (`01a112d9-a3b6-7ca3-af3c-c6e2c56916fe`)
**Preflight Verdict:** **CONDITIONAL GO** (Release-window backups & local restore drill required before deploy)

---

## 1. Executive Summary & Preflight Verdict

A read-only preflight check for the E1 PR #45 live release was performed against live runtime infrastructure (`infra-postgres-1`, `cryptonite_redis`, `cryptonite_ingest`).

### Verdict: **CONDITIONAL GO**

- **Prerequisite Gate**: Fresh release-window PostgreSQL and Redis backups and a local restore drill must be executed immediately prior to deployment per [`docs/operator-release-checklist.md`](file:///home/samx/Documents/projects/cryptonite/repo/docs/operator-release-checklist.md).
- **Snapshot Age Note**: The existing backup snapshot at `/home/samx/Documents/projects/cryptonite/bkup-pre-a3` was created on Oct 8 ~15:56 Cairo, while the preflight audit occurred on Oct 9 21:32 Cairo (~30 hours gap). A fresh release-window backup must be created before deploy.
- **Waived & Deferred Gates**: The user explicitly waived the three-night soak wait requirement. Off-machine backup restore testing remains deferred.
- **Target Merge**: PR #45 (`fix/d3-feed-timeframes`, HEAD `741802213705b1484a1288b94a8e5b87cbe94be6`) onto `origin/main` (`fbc80f52007eac70db460fdcb312fbae1942859e`).

---

## 2. Empirical Preflight Measurements

| Measurement Category | Empirical Value / Status | Verification Detail / Command |
|---|---|---|
| **Git Revision Main** | `fbc80f52007eac70db460fdcb312fbae1942859e` | `git rev-parse origin/main` |
| **Git Revision PR #45** | `741802213705b1484a1288b94a8e5b87cbe94be6` | `git rev-parse origin/fix/d3-feed-timeframes` |
| **Live Ingest Timeframes** | `INGEST_TIMEFRAMES=1m,1h` | `docker exec cryptonite_ingest env` |
| **Target Ingest Timeframes** | `INGEST_TIMEFRAMES=1m,15m,1h,4h` | PR #45 `infra/docker-compose.yml` |
| **Live Ingest Container ID** | `fd74d75df5c0` (`fd74d75df5c034c8aac14...`) | `docker inspect cryptonite_ingest` |
| **Live Ingest Image Digest** | `sha256:bee2d23192d45ba2ea316a1c53b2211987fdc703e1532b24b413a49c3b864d34` | `docker images --digests` |
| **Available Disk Space** | `13 GB` (`/dev/nvme0n1p3`, 95% used) | `df -h /` |
| **Redis Stream Consumer Lag** | `lag = 0`, `pending = 0` (all groups) | `XINFO GROUPS cryptonite:stream:closed_bar_committed` |
| **Redis Stream Last ID** | `1791570540518-0` (up-to-date) | `scanner-worker` & `api-fanout` consumer groups |
| **Live DB Revision** | `041` | `alembic_version` table in `cryptonite` DB |
| **Active Scan Specs** | `5` (`is_archived = false`) | `scan_specs` (`rsi-oversold`, `trend-pullback`, `macd`, `scalp-multi-tf`, `bollinger`) |
| **Active Spec Universes** | `BTCUSDT.BINANCE`, `ETHUSDT.BINANCE` | `scan_spec_versions` (15m, 1h, 4h timeframes) |
| **AI Mandates** | `3` (`BTC-ETH spot L1` ACTIVE, `WLDUSDT` ACTIVE, `XRPUSDT` ARCHIVED) | `ai_mandates` table |
| **Open Paper Positions** | `2` (`BTCUSDT.BINANCE` LONG 0.03, `BTCUSDT.BINANCE` LONG 0.01155) | `paper_positions WHERE status = 'OPEN'` |
| **Instruments in DB** | `4` (`BTCUSDT`, `ETHUSDT`, `XRPUSDT`, `WLDUSDT`) | `instruments` table |
| **Ingest Batches** | `17,175` | `ingest_batches` table |
| **Prior Backup Snapshot** | Stale Oct 8 ~15:56 Cairo (`bkup-pre-a3`) | Requires fresh backup before deploy |

---

## 3. Rollout & Rollback Strategy

### 3.1 Operator Deployment Command (After Pre-Deploy Backup)
```bash
docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate --build ingest
```

### 3.2 Rollback Levels & Baseline Targets

1. **Config-Only Rollback (Level 1 — Ingestion Recovery)**:
   - **Trigger**: Ingest service starts but encounters websocket or feed issues.
   - **Procedure**: Revert `infra/docker-compose.yml` `INGEST_TIMEFRAMES` to `1m,1h` and run:
     ```bash
     docker compose -f infra/docker-compose.yml up -d --no-deps --force-recreate ingest
     ```
   - **Impact**: Instant recovery, zero data loss, no database restore required.

2. **Image Baseline Rollback (Level 2 — Container Image Fallback)**:
   - **Trigger**: Container build failure or dependency break during rebuild.
   - **Target Baseline**:
     - Container: `fd74d75df5c0`
     - Image Digest: `sha256:bee2d23192d45ba2ea316a1c53b2211987fdc703e1532b24b413a49c3b864d34`
   - **Procedure**: Tag/re-point `infra-ingest:latest` to `sha256:bee2d231...` and recreate container.

3. **Full State Restore (Level 3 — Data Corruption Recovery)**:
   - **Trigger**: Database corruption or state divergence during deploy.
   - **Procedure**: Restore PostgreSQL database `cryptonite` and Redis state from the fresh release-window backup created during the pre-deploy step per [`docs/operator-release-checklist.md`](file:///home/samx/Documents/projects/cryptonite/repo/docs/operator-release-checklist.md).
