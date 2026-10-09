# K1 Phase 2.3: Ingest REST Rate-Limit Hardening (F19)

**Status:** PR OPEN (branch `feat/k1-ingest-rate-limit-hardening`, base `origin/main` `fbc80f5`). **No live deploy, no main merge.**

**Task:** `01a121f6-eb95-7500-97bf-9eb29265df6f` (K1 Phase 2.3 ingest REST rate-limit hardening).

---

## 1. Problem Addressed (F19)

In `services/ingest/cryptonite_ingest/client.py`, `BinanceRestClient.fetch_klines` had the following vulnerabilities:
1. **UnboundLocalError on 429 Exhaustion**: If three consecutive 429 (Too Many Requests) responses occurred, the retry loop exited without defining `raw_rows`, leading to an `UnboundLocalError` when attempting to iterate over `raw_rows`.
2. **Unhandled 418 (IP Ban)**: HTTP 418 responses were passed through `resp.raise_for_status()`, causing immediate generic retries without honoring `Retry-After` or logging an explicit IP ban error.
3. **Fragile `Retry-After` Parsing**: `int(resp.headers.get("Retry-After", "5"))` could crash with `ValueError` if the header contained malformed or non-integer values.
4. **Unmonitored Request Weight**: `X-MBX-USED-WEIGHT-1M` response headers were ignored, missing early warning signals when approaching Binance REST weight limits (6000 weight/min).

---

## 2. Implementation Summary

`services/ingest/cryptonite_ingest/client.py`:
- Added `self.last_used_weight_1m: int` attribute on `BinanceRestClient`.
- Added `_update_weight(resp)` helper to parse and track `X-MBX-USED-WEIGHT-1M` safely across both `fetch_klines` and `get_exchange_info`. Malformed or non-numeric headers log a warning without raising exceptions.
- Added `_parse_retry_after(resp, default)` helper to safely extract integer seconds from `Retry-After` headers, falling back cleanly to defaults if absent or malformed.
- Updated `fetch_klines`:
  - Handles 429 status by parsing `Retry-After`, logging warnings, sleeping `retry_after` seconds, and setting `last_error`.
  - Handles 418 status by parsing `Retry-After` (defaulting to 60s), logging error, sleeping `retry_after` seconds, and setting `last_error`.
  - Captures `last_error` and raises `httpx.HTTPStatusError` cleanly if all retries are exhausted (eliminates `UnboundLocalError`).
  - Pre-request guard: if `last_used_weight_1m >= 1000`, logs warning and backs off 1.0s before sending requests.

---

## 3. Files Modified

| File | Change |
|---|---|
| `services/ingest/cryptonite_ingest/client.py` | Add weight tracking, safe `Retry-After` parsing, 429/418 retry handling |
| `tests/test_ingest_rate_limiting.py` | **new** — 6 deterministic unit tests (happy path, 3x 429s, 418 IP ban, malformed headers, high weight backoff, exchangeInfo weight) |
| `docs/k1-phase23-ingest-rate-limit-status.md` | **new** — Status documentation for F19 hardening |

---

## 4. Test Verification

Run command:
`uv run pytest tests/test_ingest_rate_limiting.py tests/test_gap_recovery.py -v`

Result:
- `test_fetch_klines_happy_path`: PASSED
- `test_fetch_klines_handles_three_429s_without_crashing`: PASSED
- `test_fetch_klines_handles_418_ip_ban`: PASSED
- `test_fetch_klines_handles_malformed_headers`: PASSED
- `test_fetch_klines_high_weight_backoff`: PASSED
- `test_get_exchange_info_updates_weight`: PASSED
- All 6 existing `test_gap_recovery.py` tests: PASSED (12/12 passed in 1.08s).

Lint & formatting:
`uvx ruff check services/ingest/cryptonite_ingest/client.py tests/test_ingest_rate_limiting.py` $\rightarrow$ All checks passed!

---

## 5. Architectural & Scope Boundaries

- Scope bounded strictly to F19 (`client.py`, tests, docs).
- Did **NOT** modify compose files, DB schemas, live ingestion, Brain files (PR #46/#50), or D6 files (PR #49).
- Zero live deploy, zero main merge.
