# K1 Phase 2.3: Reconnect Gap Recovery for All Fed Symbols & Timeframes (F4)

**Status:** Implementation & Tests Complete (branch `feat/k1-ingest-reconnect-gap-recovery`, base PR #52 `c1cd52edafb0a168326335dd379ffbd050fe3f8d`). **No live deploy, no main merge.**

**Task:** `01a12227-c0e2-7741-9ce2-7495e07c8240` (K1 Phase 2.3 reconnect gap recovery for all fed symbols and timeframes).

---

## 1. Problems Addressed (F4)

1. **Missing Reconnect Triggering in Ingest Recorder**: `IngestRecorder` previously executed subscribe-before-backfill gap recovery only once at initial startup. Any subsequent WebSocket disconnection, network drop, or reconnect in `BinanceStreamClient` resulted in unrecovered gaps for live feeds.
2. **Critical Queue Draining Data Loss in `recovery.py`**: When draining `closed_bar_buffer` during gap recovery for a specific `(symbol, timeframe)`, items not matching that exact tuple were popped via `get_nowait()` and discarded, permanently dropping live closed bars belonging to other symbols or timeframes.
3. **Absence of Dynamic `SUBSCRIBE` for Incremental Symbol Onboarding**: `BinanceStreamClient.add_symbol` previously only forced a full socket disconnect/reconnect. This interrupted active streaming feeds (e.g. BTC/ETH) whenever an instrument was onboarded at runtime.
4. **Checkpoint Race Condition Under Concurrent Stream Delivery**: If a live closed bar arrived while gap recovery was fetching historical REST klines, `_on_closed_bar_event` could advance the database checkpoint before the recovered gap batch committed, causing the coordinator to reject the gap batch as `start_ns <= last_ts`.
5. **REST Gap Pagination**: Gaps exceeding 1,000 bars (e.g. extended network outages) only retrieved the first page without paginating up to the gap end timestamp.

---

## 2. Implementation Summary

### `services/ingest/cryptonite_ingest/stream.py`
- Added `on_reconnect` callback parameter to `BinanceStreamClient.__init__`.
- Added `reconnect_delay: float = 1.0` parameter to allow deterministic, rapid test execution while maintaining 1.0s production backoff.
- Hooked `is_reconnect` detection in `_listen()` to asynchronously invoke `_safe_on_reconnect()` upon any successful reconnection.
- Updated `add_symbol(symbol: str) -> bool` to attempt dynamic WebSocket `SUBSCRIBE` frame sending on open connections without closing sockets. Falls back to closing sockets (forced reconnect) only if no open socket exists or send fails.
- Preserved existing spot quotes (`@bookTicker`) and USD-M perp quote streaming (`quote_ws_url`).

### `services/ingest/cryptonite_ingest/recovery.py`
- Fixed queue draining in `recover_gaps_for_stream()`: Unmatched items in `closed_bar_buffer` are collected in `unmatched` and re-queued via `put_nowait()`, preserving all bars for other streams.
- Added paginated REST retrieval: Loops up to `gap_end_ms` with step size calculated per timeframe (`1m`, `15m`, `1h`, `4h`) bounded by `max_pages=20` (up to 20,000 bars).
- Cleanly dedupes REST and buffered live bars, and skips commit only if both are empty.

### `services/ingest/cryptonite_ingest/recorder.py`
- Added `_recovery_lock = asyncio.Lock()` and `_pending_recovery_symbols = set()` on `IngestRecorder`.
- Implemented `trigger_recovery(symbols: list[str] | None = None)`:
  - Idempotent and bounded: coalesces rapid concurrent triggers into `_pending_recovery_symbols`.
  - Serializes recovery under `_recovery_lock` and tracks `recovered_in_session` to avoid redundant re-runs.
  - Recovers across all configured timeframes (`1m`, `15m`, `1h`, `4h`) and calls `heal_empty_files()` beforehand.
- Implemented `_on_stream_reconnect()`: Invoked by `stream_client` to trigger recovery for all fed symbols upon reconnect.
- Implemented `add_symbol(symbol: str)`:
  - Registers symbol, syncs instruments to Nautilus catalog and Postgres DB.
  - Dynamically subscribes on `stream_client`.
  - If dynamic subscribe succeeded without reconnect: triggers isolated gap recovery for that new symbol without interrupting other feeds.
  - If dynamic subscribe fell back to reconnect: `_on_stream_reconnect` recovers all symbols.
- Guarded `_on_closed_bar_event` with `async with self._recovery_lock:`: Ensures incoming live closed bars wait for any active gap recovery batch to commit and advance checkpoint in proper chronological order.
- Unified startup gap recovery to use `trigger_recovery(self.symbols)`.

---

## 3. Files Modified

| File | Change |
|---|---|
| `services/ingest/cryptonite_ingest/stream.py` | Add `on_reconnect` callback, `reconnect_delay`, and dynamic `SUBSCRIBE` without reconnect |
| `services/ingest/cryptonite_ingest/recovery.py` | Fix queue draining bug to preserve other streams; add paginated REST klines for large gaps |
| `services/ingest/cryptonite_ingest/recorder.py` | Add `_recovery_lock`, `trigger_recovery`, `_on_stream_reconnect`, dynamic `add_symbol`, locked live bar commit |
| `tests/test_reconnect_gap_recovery.py` | **new** — 8 deterministic unit and integration tests |
| `docs/k1-phase2-3-reconnect-recovery-status.md` | **new** — Implementation status and verification evidence |

---

## 4. Test Verification

### Test Suite Execution
Run command:
```bash
uv run pytest tests/test_reconnect_gap_recovery.py tests/test_gap_recovery.py tests/test_ingest_perp_quotes.py tests/test_ingest_rate_limiting.py -v
```

Results:
- `tests/test_reconnect_gap_recovery.py`:
  - `test_closed_bar_buffer_draining_preserves_other_streams`: PASSED (preserves all non-matching bars)
  - `test_forced_websocket_reconnect_triggers_gap_recovery_all_symbols_and_timeframes`: PASSED (covers all 8 streams across 1m, 15m, 1h, 4h)
  - `test_dynamic_subscribe_on_open_socket_without_reconnect`: PASSED (sends SUBSCRIBE frame, keeps socket open)
  - `test_fallback_reconnect_when_socket_not_open_on_add_symbol`: PASSED (closes socket when not open)
  - `test_recorder_dynamic_add_symbol_triggers_isolated_recovery`: PASSED (recovers new symbol without interrupting existing)
  - `test_bounded_idempotent_reconnect_coalescing`: PASSED (5 concurrent triggers coalesce into 2 bounded runs)
  - `test_recovery_lock_serializes_live_closed_bar_event`: PASSED (live bar waits for recovery batch commit)
  - `test_paginated_rest_gap_recovery_large_gap`: PASSED (2,500 bars across 3 pages)
- `tests/test_gap_recovery.py`: 6/6 PASSED
- `tests/test_ingest_perp_quotes.py`: 6/6 PASSED
- `tests/test_ingest_rate_limiting.py`: 14/14 PASSED
- **Total: 34 passed in 1.92s**.

### Code Quality & Formatting
Run commands:
```bash
uv run ruff format --check services/ingest/cryptonite_ingest/stream.py services/ingest/cryptonite_ingest/recorder.py services/ingest/cryptonite_ingest/recovery.py tests/test_reconnect_gap_recovery.py
uv run ruff check services/ingest/cryptonite_ingest/stream.py services/ingest/cryptonite_ingest/recorder.py services/ingest/cryptonite_ingest/recovery.py tests/test_reconnect_gap_recovery.py
```
Output:
- `4 files already formatted`
- `All checks passed!`

---

## 5. Scope & Boundary Compliance

- **No merge to `main`**: All work performed on isolated branch `feat/k1-ingest-reconnect-gap-recovery`.
- **Target branch**: Stacked on PR #52 feature branch (`feat/k1-ingest-rate-limit-hardening`).
- **Files untouched**:
  - `services/ingest/cryptonite_ingest/client.py` (rate-limit changes preserved untouched)
  - `packages/storage/cryptonite_storage/coordinator.py` (PR #53 storage coordinator untouched)
  - External plan `symbol-onboarding-plan-v2.md` untouched
  - PR #47 execution ledger untouched
- **No live deployment or live database mutation**.
