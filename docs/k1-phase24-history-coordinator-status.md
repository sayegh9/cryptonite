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
   - **Covered Bars Never Rewritten (fail-closed)**: Checks catalog intervals (`self.nautilus.get_intervals("bars", str(bar_type))`) and filters out already-covered bars. If the catalog cannot be queried, the batch is **refused** (`RuntimeError`) instead of being written unfiltered — the guarantee never silently stops applying. If all bars in a batch are covered, returns early without writing or mutating state.
   - **Contiguity-Safe Writes (span-masking fix)**: Bars are validated to lie on the timeframe grid (duplicates / sub-cadence / off-grid close stamps are rejected with `ValueError`) and split into strictly contiguous runs (`split_contiguous_runs`). Each run is committed as its own batch/file, so a catalog file span always exactly equals its contents. Combined with live ingest's single-bar writes, **no catalog span can ever mask an interior missing bar after a partial write**; re-runs of `missing_runs` keep seeing every genuine gap (verified empirically and by regression tests).
   - **Zero Outbox Emission**: Suppresses outbox insertions into `outbox_events` (neither `closed_bar_committed` nor `nautilus:stream:bus` `Bar` events are inserted), preventing live scanner cascades and chart resets.
   - **Safe Checkpoint Widening (pool mode only)**: With a Postgres pool, updates `ingest_checkpoints` using `first_timestamp_ns = LEAST(...)` and `last_timestamp_ns = GREATEST(...)`, cleanly extending the historical boundary backward without rolling back the live watermark.
   - **Durability & Batch Tracking (pool mode only)**: With a Postgres pool, records and commits batch metadata in `ingest_batches` with payload checksums, tagging each row `origin='history'` and its real `timeframe` (migration 043) so a later `reconcile_pending_batches` replays it through the history path (guard bypassed, right bar type / timeframe / outbox suppression) and settles fully-covered re-runs instead of leaving permanently-pending rows.
   - **Offline / Pool-Free Support**: `scripts/backfill_history.py` intentionally constructs the coordinator with `pg_pool=None` — it runs without any Postgres connection, so **no `ingest_batches` row and no checkpoint update are made** in that mode. The Nautilus catalog is the source of truth for history; live startup recovery plus a future pooled run keep the checkpoint consistent when the operator chooses to wire Postgres in.

3. **CLI Script Migration (`scripts/backfill_history.py`)**:
   - Imports `missing_runs` from `cryptonite_storage.coordinator`.
   - Switched from `coordinator.commit_batch` to `coordinator.commit_history_batch`.
   - **Timeframe support (F6)**: The hard-coded `TF_MS` map is replaced with the shared `timeframe_ns` validator, so `15m` and `4h` (from the E1 timeframe expansion) are backfillable alongside `1m`/`1h`/`1d`/`1w`; calendar-month `1M` is rejected.
   - **Accurate written metric (F5)**: Only counts bars toward `written` when the coordinator returns a real batch id (i.e. it actually wrote bars rather than reporting the whole page already covered).
   - **Honest offline docs**: The module docstring now states explicitly that the script runs `pg_pool=None` and that in that mode no `ingest_batches` row / checkpoint update occurs.
   - Updated documentation to reflect the new coordinator method.

4. **Preserved Invariant**:
   - Live `commit_batch` behavior remains 100% unchanged, maintaining the live checkpoint boundary guard and emitting standard outbox events.

---

## 2. Component Changes

### 2.1 Storage Coordinator (`packages/storage/cryptonite_storage/coordinator.py`)
- **`missing_runs(intervals, start_ns, end_ns, step_ns)`**:
  - Computes uncovered grid timestamps as contiguous runs of missing bars.
  - Added defensive validation `step_ns > 0`.
  - Handles mid-grid `start_ns`/`end_ns` and mid-grid interval bounds by rounding to the grid.
- **`split_contiguous_runs(bars, step_ns)`** (new, public):
  - Splits validated bars into maximal runs whose close stamps are consecutive on the grid.
  - Guarantees every history `write_bars` call is a complete contiguous sequence, so a catalog file
    span can never mask an interior missing bar after a partial write.
- **`TwoStoreCoordinator.commit_history_batch(...)`**:
  - Validates the timeframe grid (fail-closed on duplicates / sub-cadence / off-grid stamps).
  - Filters covered bars fail-closed (`RuntimeError` if the catalog cannot be queried).
  - Splits uncovered bars into contiguous runs and commits each as its own batch.
  - Returns the last non-empty batch id (returns `""` iff nothing was written).
- **`TwoStoreCoordinator._execute_commit(...)`**:
  - Kept `is_history: bool = False`; the live-path behavior is unchanged (checkpoint guard + outbox).
  - Coverage filtering now lives in `commit_history_batch`; `_execute_commit` writes exactly what it
    is given (important for idempotent reconcile replay).
  - `_insert_pending_batch` now persists `timeframe` and `origin` (`'live'|'history'`).
- **`TwoStoreCoordinator.reconcile_pending_batches(...)`**:
  - Selects and replays each pending row with its own `bar_type` (parsed from the stored canonical
    string), `timeframe`, and `origin`. Pre-checkpoint history rows replay via `is_history=True`
    (guard bypassed, outbox suppressed, LEAST/GREATEST widening). Multi-timeframe pending batches no
    longer all replay as the caller's current iteration bar type / `1m`.
  - Restart-safe settlement: a history replay that returns `""` (batch already covered) marks the row
    `committed`, so no history batch can linger as `pending` forever.
- **`TwoStoreCoordinator._commit_batch_and_checkpoint(...)`**:
  - Unchanged: skips generation of `outbox_event_id`/`event_payload` and both outbox inserts when
    `is_history=True`; performs `ingest_batches` status update and `ingest_checkpoints` upsert
    (GREATEST/LEAST) in a single transaction.
- **Migration `043` (`infra/migrations/versions/043_ingest_batch_origin_timeframe.py`)**:
  - Adds `ingest_batches.timeframe TEXT NOT NULL DEFAULT '1m'` and
    `ingest_batches.origin TEXT NOT NULL DEFAULT 'live'` (CHECK `origin IN ('live','history')`).
  - Required for pooled pre-checkpoint history reconciliation to be restart-safe and correct for
    15m/4h batches; existing live rows default to `live`/`1m`, preserving current behavior.

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
Eighteen deterministic unit tests covering all Phase 2.4 invariants plus the independent-review
hardening (F1 span-masking, F4 fail-closed coverage, F2 pooled restart-safe reconciliation, F6 four
timeframes, mid-grid `missing_runs`):
1. `test_missing_runs_empty_catalog`: Verifies full sequence returned when no intervals exist.
2. `test_missing_runs_fully_covered`: Verifies empty list returned when intervals completely cover range.
3. `test_missing_runs_with_gaps`: Verifies exact multi-run split across disjoint interval coverage.
4. `test_missing_runs_single_point_files`: Verifies single-bar `[t, t]` files properly identify gaps.
5. `test_missing_runs_invalid_step`: Verifies `ValueError` raised on non-positive `step_ns`.
6. `test_commit_history_batch_bypasses_checkpoint_guard`: Verifies `commit_batch` rejects pre-checkpoint bars while `commit_history_batch` commits and widens `first_timestamp_ns` via `LEAST`.
7. `test_commit_history_batch_emits_no_outbox_events`: Verifies `outbox_events` table receives 0 entries during `commit_history_batch`, while `commit_batch` emits `closed_bar_committed` and `nautilus:stream:bus` rows.
8. `test_covered_bars_never_rewritten`: Verifies re-committing identical bars returns `""` and skips writes; committing partially-covered batches writes only the uncovered delta.
9. `test_live_commit_batch_behavior_unchanged`: Verifies standard live `commit_batch` maintains contract schema version 2 and dual-store writes.
10. `test_missing_runs_mid_grid_start_and_end`: Verifies grid alignment when `start_ns`/`end_ns` and interval bounds fall mid-grid.
11. `test_split_contiguous_runs_splits_on_interior_gaps`: Pure-function verification of the contiguous-run splitter.
12. `test_commit_history_batch_gappy_input_never_masks_interior_gap`: **F1 regression** — committing close stamps k=1 and k=3 (k=2 absent) leaves k=2 visible to `missing_runs`; no catalog interval contains it.
13. `test_commit_history_batch_rejects_off_grid_and_sub_cadence`: **F1 enforcement** — off-grid close stamps and duplicate/sub-cadence bars are rejected and nothing is written.
14. `test_commit_history_batch_fails_closed_when_coverage_unreadable`: **F4 regression** — a catalog interval-query failure raises and writes nothing (no fail-open).
15. `test_commit_history_batch_supports_four_timeframes`: **F6 regression** — 1m/15m/1h/4h contiguous commits produce exact coverage (written closes covered, next close missing).
16. `test_reconcile_history_batch_replays_as_history_with_origin_and_timeframe`: **F2 regression** — a pooled pre-checkpoint 15m history batch replays with its own bar type/timeframe, bypasses the guard, emits zero outbox, and inserts `origin='history'`, `timeframe='15m'`.
17. `test_reconcile_history_batch_settles_fully_covered`: **F2 regression** — an all-covered history replay (returns `""`) settles the row to `committed` instead of leaving it pending forever.
18. `test_reconcile_live_batch_preserves_live_guard`: Live pending rows keep the pre-existing live reconcile behavior (guard skip, no settlement).

### 3.2 Regression Test Execution
- **Focused Suite**: `uv run pytest tests/test_history_coordinator.py tests/test_two_store_commit.py tests/test_closed_bar_event.py -v`
  - **Result**: `30 passed` (18 history-coordinator + 12 adjacent coordinator/live-bar tests)
- **Linter Check**: `uvx "ruff@0.11.10" check packages/storage scripts/backfill_history.py tests/test_history_coordinator.py infra/migrations/versions/043_ingest_batch_origin_timeframe.py`
  - **Result**: `All checks passed!`
- **Whitespace / Diff Check**: `git diff --check`
  - **Result**: Clean (zero whitespace or indentation warnings).
- **Runtime probes (real Nautilus catalog, temp dirs)**:
  - `write_bars` of several bare bars in one call yields one **span** interval `[min, max]` — the masking trigger the split eliminates.
  - `write_bars` per bar yields **point** intervals; `get_intervals` does not merge across gaps. The split produces per-piece files, so no span can hide an interior missing bar.

---

## 4. Scope, Safety & Boundaries

- **Zero Live Mutations**: All unit tests run against local `tempfile.TemporaryDirectory` paths and mock connection pools (`FakePool`). No connection made to live Postgres or Redis databases.
- **Isolated Footprint**: Only `packages/storage`, `scripts/backfill_history.py`, `tests/test_history_coordinator.py`, and this document are modified/added.
- **No Concurrent Branch Contamination**: PR #45 (compose), PR #46/#50 (Brain), PR #49 (D6), PR #52 (`client.py`), and web packages are completely untouched.
- **No Merge / Deploy**: Code is prepared strictly on stacked branch `review/k1-history-coordinator-hardening`
  (off exact PR #53 head `01de154`) for operator review and PR submission; PR #53's author branch
  (`feat/k1-history-coordinator`) and `main` are not modified.

---

## 5. Independent-Review Hardening (task `01a1221f`)

Verification report for PR #53 (`feat/k1-history-coordinator` @ `01de154`) and the decision ledger for
the hardening stacked PR. Findings F1–F7 and dispositions:

- **F1 — `get_intervals` returns file spans, not per-bar points (Moderate)**: a multi-bar `write_bars`
  produces one merged `[min, max]` interval, so a batch with an interior hole would mask genuinely
  missing bars from `missing_runs`. **Fixed at `commit_history_batch` layer**: fail-closed grid
  validation (`_assert_history_grid` rejects off-grid / duplicate / sub-cadence bars) + contiguous-run
  splitting (`split_contiguous_runs`); each run commits as its own batch. Empirically verified: a
  gappy input now yields per-point catalog intervals and the interior hole stays visible. Commit
  pieces that survive a crash are always contiguous (never a span with interior holes).
- **F2 — pooled pre-checkpoint history batch stuck `pending` forever (Moderate, latent)**: the live
  recorder's startup `reconcile_pending_batches` replayed history rows through the live path
  (`is_history` never set) under the caller's iteration bar type and hard-coded `1m`. **Fixed**:
  migration 043 persists `origin` (`'live'|'history'`) and the real `timeframe` on `ingest_batches`;
  reconcile replays each row with its own parsed bar type, its own timeframe, and `is_history`
  derived from `origin`; an all-covered history replay returns `""` and is **settled to `committed`**
  instead of lingering. Byproduct live-path fix: pending 1h/15m/4h rows now replay as their own
  timeframe/bar type rather than the caller's current iteration.
- **F3 — LEAST/GREATEST widening + batch tracking are no-ops in the bundled script (Moderate)**: the
  offline script runs `pg_pool=None`, so no `ingest_batches` row and no checkpoint update happen.
  **Honed docs**: `commit_history_batch` docstring + this status document now say exactly that; with a
  pool, both features apply and are restart-safe (see F2).
- **F4 — covered-filter fails open (Low)**: a `get_intervals` exception previously degraded to writing
  unfiltered. **Fixed**: coverage now fails closed (`RuntimeError`, nothing written).
- **F5 — `written` overcounts (Low)**: the script counted a page's bars even when the coordinator
  returned `""` (whole page already covered). **Fixed**: count only on a real batch id.
- **F6 — `TF_MS` hard-codes 1m/1h (Low)**: replaced with the shared `timeframe_ns` validator, so
  15m/4h (E1 timeframe expansion) backfill correctly; `1M` calendar months rejected.
- **F7 — test gaps**: added 9 regression tests (span-hole visibility, pooled reconcile origin/timeframe
  round-trip, covered-filter fail-closed, mid-grid `missing_runs`, four timeframes, settle, live-path
  preservation, splitter unit test).

**Unresolved risks (accepted, outside this PR)**: (a) a reconciled **live** pending row skipped by the
checkpoint guard still stays `pending` (pre-existing semantics; live behavior intentionally preserved);
(b) old `pending` rows written before migration 043 take the `DEFAULT 1m/live` — correct for existing
live ingest, and any pre-043 history rows would not exist yet (history origin ships with this PR);
(c) operators must apply migration 043 before deploying the new ingest binary so `reconcile_pending_batches`
can read the new columns — standard migration-before-deploy sequencing.
