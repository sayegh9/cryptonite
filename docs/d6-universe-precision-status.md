# D6: Universe-Aware Price and Size Precision Status

## Overview
Implementation of K1 prerequisite **D6: Universe-Aware Price & Size Precision** (ref: `product-review-plan.md` §D6, F-M7). Eliminates hardcoded `price_precision=2` and volume precision `4` across scanner rule evaluation and orchestrator bar parsing, replacing them with validated per-instrument metadata fetched from the database `instruments` table. If precision metadata is missing or invalid, evaluation fails closed.

---

## Summary of Changes

### 1. `packages/scanner/cryptonite_scanner/engine.py`
- Updated `ScannerEngine.evaluate()` to require validated integer `price_precision`.
- Added fail-closed guard: if `price_precision is None` or invalid (`< 0`), `evaluate()` logs an error and returns `[]` immediately.
- Updated matched rule explanation formatting (`matched_explanations`) to format price fields (`close`, `open`, `high`, `low`, `ema_*`, `bb_*`, `donchian_*`, `atr`) using `price_precision` (e.g. 8 decimals for `SHIBUSDT`), avoiding price truncation to 2 decimals for sub-$0.01 symbols.

### 2. `services/scanner/cryptonite_scanner_service/worker.py`
- Added `_load_instrument_precisions()` method to `ScannerWorker` to fetch validated `price_precision` for target instruments from the `instruments` database table.
- Updated `_execute_scan()` to pass `inst_price_prec` to `ScannerEngine.evaluate()` for each scanned instrument.
- Added warning and fail-closed handling if instrument precision metadata is missing or inactive in the database.

### 3. `services/orchestrator/cryptonite_orchestrator/server.py`
- Added `_get_instrument_precisions()` helper in the orchestrator stream reader loop to fetch `price_precision` and `size_precision` from `instruments` for incoming bars.
- Updated the fallback JSON bar parsing block to construct Nautilus `Price` and `Quantity` objects using validated instrument precisions (`p_prec`, `s_prec`) instead of hardcoded `2` and `4`.
- Fails closed (logs error and skips bar dispatch) if precision metadata is missing for an instrument.

### 4. `tests/test_d6_universe_precision.py`
- Created comprehensive unit test suite verifying:
  1. `ScannerEngine.evaluate` fails closed when `price_precision` is `None` or invalid.
  2. `BTCUSDT` / `ETHUSDT` 2-decimal regression integrity.
  3. Sub-$0.01 symbol (`SHIBUSDT`, `price_precision=8`) entry price ($0.00001742) and matched rule explanations preserve 8-decimal precision without truncation to $0.00.
  4. Low-priced symbol (`WLDUSDT`, `price_precision=4`) entry price ($1.5432) preserves 4-decimal precision.
  5. `ScannerWorker._load_instrument_precisions` fails closed when database pool is unavailable or instrument missing.

---

## Verification & Testing
- **Unit Test Execution**:
  ```bash
  uv run pytest tests/test_d6_universe_precision.py tests/test_scanner_engine.py tests/test_precision_versioning.py tests/test_scanner_structure_d3.py
  ```
  Result: **19 passed in 7.16s**.
- **Formatting & Code Check**:
  `git diff --check` passed cleanly with 0 trailing whitespace errors.

---

## Architectural & Security Implications
- **Fail-Closed Security Guarantee**: System never defaults unvalidated instruments to dummy precision values. Missing metadata logs an error and halts signal candidate generation/ingestion for that instrument.
- **Universe Expansion Readiness**: Supports arbitrary price precisions (e.g. 4, 5, 8 decimals) required for low-priced and sub-$0.01 trading pairs (WLD, DOGE, SHIB, ADA, etc.).
- **Zero Database Migration Required**: Leverages existing `instruments.price_precision` and `instruments.size_precision` columns in DB schema revision `041`.
