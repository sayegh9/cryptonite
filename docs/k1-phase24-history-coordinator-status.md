# K1 Phase 2.4: Historical Backfill Coordinator & Catalog Interval Gap Detection

**Status:** Completed & Validated
**Task ID:** `01a121fd-a218-75a3-b4d1-21ae9b9c4d7a`
**Reference Plan:** `symbol-onboarding-plan-v2.md` §2.4, §4.2 (S3 History), §8 (Acceptance Criteria), §9 (Risks)
**Branch:** `feat/k1-history-coordinator`

---

## 1. Executive Summary

Phase 2.4 establishes the historical backfill coordinator and storage gap-detection primitives required by the K1 symbol onboarding pipeline. 

Previously, `TwoStoreCoordinator.commit_batch` enforced a strict live checkpoint boundary guard (`if start_ns <= last_ts: return ""`), preventing any data older than the live stream checkpoint from being committed through the production coordinator. Additionally, whenever a batch was committed, `commit_batch` automatically inserted live stream events (`closed_bar_committed` and `nautilus:stream:bus` `Bar` records) into `outbox_events`, which would trigger downstream live scanners (F8 rescans) and cause live chart glitches if invoked during historical backfilling. Consequently, `scripts/backfill_history.py` had to run disconnected from Postgres (`pg_pool=None`) and kept its own local interval gap detection logic.

Phase 2.4 resolves these limitations by introducing `commit_history_batch` and moving catalog interval gap computation directly into `packages/storage`.

### Core Deliverables:
1. **Catalog Interval Gap Detection (`missing_runs`)**:
   - Factored out of `scripts/backfill_history.py` and moved into `packages/storage/cryptonite_storage/coordinator.py`.
   - Exported in `packages/storage/cryptonite_storage/__init__.py`.
   - Computes missing contiguous runs on the timeframe grid from catalog intervals, handling single-point live files (`[t, t]`) and arbitrary multi-file intervals.
   - Enforces positive step validation (`step_ns > 0`).

2. **Historical Commit Protocol (`TwoStoreCoordinator.commit_history_batch`)**:
   - **Bypasses Checkpoint Guard**: Allows writing historical bars older than the live ingest checkpoint watermark (`start_ns <= last_ts`).
   - **Covered Bars Never Rewritten**: Checks catalog intervals (`self.nautilus.get_intervals("bars", str(bar_type))`) and filters out already-covered bars. If all bars in a batch are covered, returns early without writing or mutating state.
   - **Zero Outbox Emission**: Suppresses outbox insertions into `outbox_events` (neither `closed_bar_committed` nor `nautilus:stream:bus` `Bar` events are inserted), preventing live scanner cascades and chart resets.
   - **Safe Checkpoint Widening**: Updates `ingest_checkpoints` using `first_timestamp_ns = LEAST(...)` and `last_timestamp_ns = GREATEST(...)`, cleanly extending the historical boundary backward without rolling back the live watermark.
   - **Durability & Batch Tracking**: Records and commits batch metadata in `ingest_batches` with payload checksums.
   - **Offline / Pool-Free Support**: Operates seamlessly when `pg_pool=None` (e.g. standalone CLI scripts or testing).

3. **CLI Script Migration (`scripts/backfill_history.py`)**:
   - Imports `missing_runs` from `cryptonite_storage.coordinator`.
   - Switched from `coordinator.commit_batch` to `coordinator.commit_history_batch`.
   - Updated documentation to reflect the new coordinator method.

4. **Preserved Invariant**:
   - Live `commit_batch` behavior remains 100% unchanged, maintaining the live checkpoint boundary guard and emitting standard outbox events.

---

## 2. Component Changes

### 2.1 Storage Coordinator (`packages/storage/cryptonite_storage/coordinator.py`)
- **`missing_runs(intervals, start_ns, end_ns, step_ns)`**:
  - Computes uncovered grid timestamps as contiguous runs of missing bars.
  - Added defensive validation `step_ns > 0`.
- **`TwoStoreCoordinator.commit_history_batch(...)`**:
  - New public async method accepting `bars`, `bar_type`, `venue`, `market_type`, `symbol`, `timeframe`.
  - Dispatches to `_execute_commit(..., is_history=True)`.
- **`TwoStoreCoordinator._execute_commit(...)`**:
  - Added `is_history: bool = False` parameter.
  - When `is_history=True`:
    - Queries `self.nautilus.get_intervals("bars", str(bar_type))` and filters out already-covered bars (`a <= b.ts_event_ns <= c`).
    - Skips execution if no uncovered bars remain.
    - Skips the live checkpoint boundary check (`start_ns <= last_ts`).
    - Passes `is_history=True` to `_commit_batch_and_checkpoint`.
- **`TwoStoreCoordinator._commit_batch_and_checkpoint(...)`**:
  - Added `is_history: bool = False` parameter.
  - Skips generation of `outbox_event_id` and `event_payload` when `is_history=True`.
  - Skips SQL `INSERT INTO outbox_events` for both `closed_bar_committed` and `nautilus:stream:bus` when `is_history=True`.
  - Performs `ingest_batches` status update and `ingest_checkpoints` upsert in a single transaction for both history and live.

### 2.2 Storage Package Export (`packages/storage/cryptonite_storage/__init__.py`)
- Imported and exported `missing_runs` in `__all__`.

### 2.3 CLI Backfill Script (`scripts/backfill_history.py`)
- Removed local `missing_runs` implementation.
- Imported `missing_runs` from `cryptonite_storage.coordinator`.
- Updated `backfill()` to call `coordinator.commit_history_batch`.
- Updated module docstrings.

---

## 3. Verification & Test Evidence

### 3.1 Dedicated Unit Tests (`tests/test_history_coordinator.py`)
Nine deterministic unit tests covering all Phase 2.4 invariants:
1. `test_missing_runs_empty_catalog`: Verifies full sequence returned when no intervals exist.
2. `test_missing_runs_fully_covered`: Verifies empty list returned when intervals completely cover range.
3. `test_missing_runs_with_gaps`: Verifies exact multi-run split across disjoint interval coverage.
4. `test_missing_runs_single_point_files`: Verifies single-bar `[t, t]` files properly identify gaps.
5. `test_missing_runs_invalid_step`: Verifies `ValueError` raised on non-positive `step_ns`.
6. `test_commit_history_batch_bypasses_checkpoint_guard`: Verifies `commit_batch` rejects pre-checkpoint bars while `commit_history_batch` commits and widens `first_timestamp_ns` via `LEAST`.
7. `test_commit_history_batch_emits_no_outbox_events`: Verifies `outbox_events` table receives 0 entries during `commit_history_batch`, while `commit_batch` emits `closed_bar_committed` and `nautilus:stream:bus` rows.
8. `test_covered_bars_never_rewritten`: Verifies re-committing identical bars returns `""` and skips writes; committing partially-covered batches writes only the uncovered delta.
9. `test_live_commit_batch_behavior_unchanged`: Verifies standard live `commit_batch` maintains contract schema version 2 and dual-store writes.

### 3.2 Regression Test Execution
- **Focused Suite**: `uv run pytest tests/test_history_coordinator.py tests/test_two_store_commit.py tests/test_closed_bar_event.py -v`
  - **Result**: `21 passed in 1.32s`
- **Linter Check**: `uvx ruff check packages/storage scripts/backfill_history.py tests/test_history_coordinator.py`
  - **Result**: `All checks passed!`
- **Whitespace / Diff Check**: `git diff --check`
  - **Result**: Clean (zero whitespace or indentation warnings).

---

## 4. Scope, Safety & Boundaries

- **Zero Live Mutations**: All unit tests run against local `tempfile.TemporaryDirectory` paths and mock connection pools (`FakePool`). No connection made to live Postgres or Redis databases.
- **Isolated Footprint**: Only `packages/storage`, `scripts/backfill_history.py`, `tests/test_history_coordinator.py`, and this document are modified/added.
- **No Concurrent Branch Contamination**: PR #45 (compose), PR #46/#50 (Brain), PR #49 (D6), PR #52 (`client.py`), and web packages are completely untouched.
- **No Merge / Deploy**: Code is prepared strictly on branch `feat/k1-history-coordinator` for operator review and PR submission.
