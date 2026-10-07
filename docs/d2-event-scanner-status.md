# D2: Event-Based Scanner Conditions & Candidate Deduplication

**Status:** In Review (Implementation Complete; Multi-Spec Combined Volume Reduction Partial at 6.8x–9.5x; E1 Sequencing Held)
**Task ID:** `01a115b8-0555-7e40-b20f-987dd0f8d56a`
**Reference Plan:** `docs/product-review-plan.md` §D2 and `docs/product-review-2026-10-05.md` §5.2.1 (Finding F-H2)
**Branch:** `feat/d2-event-scanner-dedupe`

---

## 1. Executive Summary

Phase D2 transitions the scanner from **state-based** conditions (which fire continuously on every bar of an established trend, flooding downstream triage and debate queues) to **event-based** crossover conditions (`lookback >= 1`).

Key deliverables and hardening implemented:
1. **Grammar & Crossover Semantics**: Added `RuleOperator.CROSSES_ABOVE` and `CROSSES_BELOW` aliases alongside `CROSS_ABOVE` / `CROSS_BELOW`. Enforced `lookback >= 1` validation on crossover conditions with a default of `lookback = 1`. Hardened Polars condition expressions with `.fill_null(False)` to prevent null leakage on shifted bars.
2. **Preset Specs Rewritten**: Updated all 4 seed presets in `packages/scanner/cryptonite_scanner/presets.py` to use crossover triggers with `lookback = 1`.
3. **Cross-Spec Deduplication & Deadlock-Free Serialization**: Added `deduplicate_candidates` utility and integrated deduplication into `ScannerWorker._execute_scan`. Candidates are sorted deterministically by `(instrument_id, timeframe, bar_close_inclusive_ns)` before acquiring PostgreSQL transaction advisory locks (`pg_advisory_xact_lock(hashtextextended(...))`), enforcing a global lock order that eliminates deadlocks and SELECT-then-INSERT races across parallel workers/jobs. Removed all `try/except` fail-open blocks on lock acquisition. Verified with a deterministic concurrent race test.
4. **Max Active Specs Guard & Signal Preservation**: Defined `MAX_ACTIVE_SPECS = 10`. Enforced rejection with HTTP 400 in API `create_scanner` and `activate_preset_version` routes, serialized via transaction advisory lock (`pg_advisory_xact_lock(hashtext('scan_specs:active_limit'))`). The worker never slices or drops existing active specs (preserving trading signals in flight); it processes all active specs with prominent `logger.error` alerts when the cap is exceeded.
5. **Read-Only Scanners GET & Idempotent Preset Upgrade**: Removed all write/mutation side-effects from `GET /api/v1/scanners`. `ensure_presets_seeded()` is restricted strictly to fresh database bootstrapping. Added explicit operator-only upgrade endpoint `POST /api/v1/scanners/presets/upgrade` supporting `dry_run` mode (for E1 sequencing), safely transitioning unedited presets while strictly preserving user-edited custom specs and honoring active spec caps.
6. **Volume Reduction Assessment (10-02..10-04)**: Evaluated 1m lake data across 2026-10-02 to 2026-10-04 for BTCUSDT and ETHUSDT without live writes. Demonstrated a **16.5x reduction** on BTCUSDT MACD momentum candidates (99 -> 6). Across all presets combined, volume reduction achieved **9.5x on BTCUSDT** and **6.8x on ETHUSDT**, which represents an honest partial result relative to the 10–50x exit goal. E1 remains held. Full multi-spec candidate reduction down to the 10–50x exit gate is prospective and planned for downstream Phase D3 via structure-based stop/target R:R filtering and higher-timeframe restriction (15m/1h/4h only); combined multi-spec reduction remains below the D2 phase gate (at 6.8x–9.5x) until D3 is implemented and measured.

---

## 2. Component Changes

### 2.1 Grammar & Validation (`packages/scanner/cryptonite_scanner/models.py`)
- Added `RuleOperator.CROSSES_ABOVE` and `RuleOperator.CROSSES_BELOW` enum members.
- Added `MAX_ACTIVE_SPECS = 10` default limit.
- Added Pydantic model validator `validate_lookback` on `RuleCondition`:
  - Enforces `lookback >= 1` for crossover operators.
  - Automatically defaults `lookback = 1` if unspecified for crossover operators.
  - Rejects negative `lookback < 0` for all operators.
- Added `deduplicate_candidates(candidates)` helper that filters candidates by `(instrument_id, timeframe, bar_close_inclusive_ns)`, preserving first occurrence.

### 2.2 Scanner Engine (`packages/scanner/cryptonite_scanner/engine.py`)
- Hardened `_build_condition_expr` for crossover conditions:
  - Supports `CROSS_ABOVE`, `CROSSES_ABOVE`, `CROSS_BELOW`, `CROSSES_BELOW`.
  - Computes `prev_left = pl.col(cond.field).shift(cond.lookback if cond.lookback >= 1 else 1)`.
  - Appends `.fill_null(False)` to both inequality clauses so boundary/shifted rows with nulls evaluate cleanly to False instead of null.
  - Supports lookback shifts on non-crossover comparisons (`cond.lookback > 0`).
- Exported `deduplicate_candidates` on `ScannerEngine`.

### 2.3 Preset Scan Specs (`packages/scanner/cryptonite_scanner/presets.py`)
Rewrote the 4 seed presets to trigger on events rather than states:
1. `macd-momentum-shift`:
   - Trigger: `macd_hist CROSS_ABOVE 0.0` (lookback=1)
   - Trend confirmation: `close > ema_50`
2. `bollinger-breakout`:
   - Trigger: `close CROSS_ABOVE bb_upper` (lookback=1)
   - Confirmation: `volume_ratio >= 1.5`, `close > open`
3. `rsi-oversold-reversal`:
   - Trigger: `close CROSS_ABOVE bb_lower` (lookback=1)
   - Confirmation: `rsi <= 35.0`, `volume_ratio >= 1.0`
4. `trend-pullback`:
   - Trend: `close > ema_200`, `ema_20 > ema_50`
   - Trigger: `rsi CROSS_ABOVE 40.0` (lookback=1)
   - Confirmation: `close > open`

### 2.4 API Route Guard & Operator Upgrade (`services/api/cryptonite_api/routes/scanners.py`)
- `GET /api/v1/scanners`: Strictly read-only; no database mutations or seeding side-effects.
- In `create_scanner`, `update_scanner`, and `archive_scanner`: acquires `pg_advisory_xact_lock(hashtext('scan_specs:active_limit'))` directly (no fail-open try/except) inside the transaction, serializing all scan spec mutations (creates, updates, upgrades, activations, archives). In `update_scanner`, additionally row-locks `scan_specs` via `SELECT ... FOR UPDATE` before appending new version numbers.
- In `ensure_presets_seeded()`: Bootstraps presets only on a fresh, unseeded database (`count == 0`), returning immediately on existing databases.
- Added `POST /api/v1/scanners/presets/upgrade` (guarded by `OPERATOR_WRITE`):
  - Supports `dry_run: bool = False` for E1 dry-run verification.
  - Locks preset rows `FOR UPDATE OF s` and checks `active_version_id = $4` during updates to guarantee concurrent user edits are never overwritten.
  - Transitions presets whose active version matches known prior built-in hashes (`PRESET_PREVIOUS_HASHES`) to new event-crossover versions.
  - Strictly preserves user-edited custom specs without overwriting (`custom_preserved`).
  - Honors active spec limits and never unarchives specs accidentally.
- In `POST /api/v1/scanners/presets/{slug}/activate` (guarded by `OPERATOR_WRITE`): Serializes via advisory lock and enforces `MAX_ACTIVE_SPECS` limit before activating or unarchiving.
- **Operator Tooling & E1 Runbook**:
  - Provided `scripts/upgrade_scanner_presets.py` supporting dry-run inspection (default) and `--apply` mode with printed rollback SQL commands.
  - Documented complete pre-scanner-start deployment sequence, verification steps, and rollback criteria in `docs/runbooks/e1_preset_upgrade.md`.

### 2.5 Scanner Worker Daemon (`services/scanner/cryptonite_scanner_service/worker.py`)
- In `reconcile_and_prime` and `_handle_stream_message`: Queries active specs deterministically (`ORDER BY s.created_at ASC, s.slug ASC`). Never slices or drops active specs to ensure zero signal loss; logs prominent `logger.error` on cap overflow.
- In `_execute_scan`:
  - Runs in-memory `deduplicate_candidates(all_candidates)` after unclosed bar filtering.
  - Deterministically sorts candidates by `(instrument_id, timeframe, bar_close_inclusive_ns)` before acquiring advisory locks to guarantee global canonical lock ordering and prevent deadlocks across concurrent workers.
  - In candidate database persistence loop: acquires `pg_advisory_xact_lock(hashtextextended($1, 0))` directly on `signal_candidate:{inst}:{tf}:{bar_close_ns}`, then verifies if candidate exists across any spec. If found, logs debug and skips insertion and debate outbox queuing.

---

## 3. Historical Lake Volume Reduction Assessment (2026-10-02 .. 2026-10-04)

Evaluated against enriched 1m lake data (`data/analytics_lake/binance_klines_enriched/binance/spot/1m/` for BTCUSDT and ETHUSDT) across dates `2026-10-02` through `2026-10-04` (4,321 1m bars each; 287 resampled 15m bars each):

| Spec / Universe | Timeframe | Old State Candidates | New Crossover Candidates | Reduction Factor |
|---|---|---|---|---|
| `macd-momentum` (BTCUSDT) | 15m | 99 | 6 | **16.5x** |
| `macd-momentum` (ETHUSDT) | 15m | 101 | 13 | **7.8x** |
| `bollinger-breakout` (BTCUSDT) | 15m | 12 | 7 | 1.7x |
| `bollinger-breakout` (ETHUSDT) | 15m | 12 | 7 | 1.7x |
| `rsi-oversold` (BTCUSDT) | 15m | 3 | 0 | 3.0x |
| `rsi-oversold` (ETHUSDT) | 15m | 2 | 1 | 2.0x |
| **All Specs Combined (BTCUSDT)** | 15m | **114** | **12** (deduped) | **9.5x** |
| **All Specs Combined (ETHUSDT)** | 15m | **115** | **17** (deduped) | **6.8x** |

### Assessment & Honest Partial Result
1. **Single-Spec Momentum Success**: The transition to event crossover rules achieved **16.5x reduction** on BTCUSDT momentum (from 99 candidates down to 6), successfully falling within the 10–50x reduction goal for that standalone spec.
2. **Multi-Spec Combined Volume (Partial Result)**: Across all 4 seed specs combined, the reduction was **9.5x on BTCUSDT** (114 down to 12) and **6.8x on ETHUSDT** (115 down to 17). This falls below the plan's 10–50x exit goal when evaluating multiple presets simultaneously. E1 remains held.
3. **Root Cause Analysis**:
   - Presets like Bollinger breakout and RSI oversold reversal have naturally lower starting candidate counts (12 and 3 respectively) and narrower differences between crossing the band vs closing outside the band over a 3-day window.
   - Cross-spec deduplication prevents duplicate submissions for the same bar, but when different specs trigger on different bars across 15m intervals, the total candidate count sums to 12 (BTC) and 17 (ETH) over 3 days (approx 4–6 candidates/day).
4. **Path to Full 10–50x Multi-Spec Target (Phase D3)**:
   - Full candidate suppression into the 10–50x range across all combined specs is prospective and planned for Phase D3 controls:
     - Enforcing trigger timeframes restricted to 15m/1h/4h only.
     - Structure-based stop/target calculations with mandatory R:R >= 1.5 filtering (rejecting candidates where R:R < 1.5).
   - Until Phase D3 is implemented and measured against the historical baseline, the multi-spec aggregate volume reduction (6.8x–9.5x) remains below the D2 phase exit gate, and E1 deployment sequencing remains held.

---

## 4. Test Verification Summary

All tests executed against an isolated throwaway database (`cryptonite_ci_d2_*`) and isolated Redis DB 15:

```bash
REDIS_URL="redis://:...@127.0.0.1:6379/15" uv run --extra dev pytest tests/test_scanner_events_d2.py tests/test_scanner_fresh_enqueue.py tests/test_scanner_closed_bar_guard.py tests/test_scanner_engine.py tests/test_api_endpoints.py -v
```

Results across the scanner test suite:
- `tests/test_scanner_events_d2.py`: **21 passed**
  - `test_rule_condition_lookback_validation`: Enforces lookback >= 1, rejects negative, defaults to 1.
  - `test_deterministic_crossover_produces_one_candidate_not_continuation`: Crossover event produces exactly 1 candidate across 250 trending bars.
  - `test_deterministic_cross_below_produces_one_candidate`: Cross-below event semantics verified.
  - `test_deduplicate_candidates_in_memory`: In-memory candidate deduplication per (instrument, timeframe, bar).
  - `test_cross_spec_candidate_deduplication_in_worker`: Cross-spec candidate deduplication in worker database and outbox.
  - `test_max_active_specs_constant`: `MAX_ACTIVE_SPECS == 10`.
  - `test_worker_processes_all_active_specs_above_cap_without_dropping_signals`: Worker executes all active specs even when exceeding cap (no trading signals dropped).
  - `test_preset_specs_are_crossover_triggers`: All 4 presets verified to use crossover triggers with lookback >= 1.
  - `test_volume_reduction_crossover_vs_state_comparison`: Verified >= 5x volume reduction on trending oscillation.
  - `test_create_scanner_api_guard_rejects_at_max_active_specs`: HTTP 400 rejection on 11th active spec.
  - `test_cross_spec_dedupe_concurrent_race_serialized`: Two concurrent worker scans racing on different specs for the same bar serialize via advisory lock, persisting exactly 1 candidate.
  - `test_worker_active_specs_deterministic_order_and_visible_error`: Deterministic ordering and visible ERROR log with processed count on cap breach.
  - `test_ensure_presets_seeded_bootstraps_fresh_db_and_noop_when_populated`: Fresh DB bootstrapped; already populated DB left unmutated.
  - `test_get_scanners_is_read_only`: `GET /api/v1/scanners` performs zero mutations on active versions or specs.
  - `test_preset_upgrade_operator_endpoint_dry_run_and_apply`: `dry_run=True` reports planned transitions without DB mutations; `dry_run=False` transitions matching presets and preserves custom specs.
  - `test_activate_preset_version_operator_route_and_cap_guard`: Guarded operator route explicitly activates preset version and rejects with HTTP 400 when active cap is reached.
  - `test_ordering_user_edit_then_preset_upgrade_preserves_custom_edit`: Deterministic ordering 1: user custom edit commits first; subsequent preset upgrade recognizes custom hash and marks as `custom_preserved`.
  - `test_ordering_preset_upgrade_then_user_edit_preserves_custom_edit`: Deterministic ordering 2: preset upgrade commits first; subsequent user edit appends new version and updates active version.
  - `test_concurrent_race_conditional_update_guard_preserves_custom_edit`: Deterministic ordering 3: simulates race where upgrade update encounters changed active version id, evaluates to `UPDATE 0`, and safely falls back to preserving user customization.
  - `test_operator_script_upgrade_presets_dry_run_and_apply`: Standalone operator script `scripts/upgrade_scanner_presets.py` verified in dry-run mode (zero mutations) and apply mode (commits target crossover versions, verifies committed state, and prints conditional rollback SQL).
  - `test_preset_upgrade_handles_archived_builtin_preset_without_false_failure`: Archived built-in presets upgraded to crossover versions preserving archived state; DB and HTTP verifications succeed without false failure and do not count against active cap.
- `tests/test_scanner_fresh_enqueue.py`: **18 passed** (fresh enqueue, relay lease, bounded queue).
- `tests/test_scanner_closed_bar_guard.py`: **5 passed** (closed bar enforcement, future candidate filtering).
- `tests/test_scanner_engine.py`: **1 passed** (scanner engine baseline candidate generation).
- `tests/test_api_endpoints.py`: **6 passed** (API health, ready, instruments, scanners CRUD/run enqueue, bars, scanners include_archived auth guard).
- **Total:** **51 passed**, **0 failed**.
- **Code hygiene:** `uvx ruff check` clean; `git diff --check` clean (0 trailing whitespace issues).
