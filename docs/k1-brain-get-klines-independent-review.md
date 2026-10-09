# Independent Security & Correctness Review — K1 `get_klines` (PR #46)

**Reviewer:** OpenCode (slot `01a112ce-9b06-7142-8401-a7fb6b8b5c50`)
**Tasks:** `01a121a6-d2bb-7861-b70f-122244188f5d` (review PR #46) · `01a121ba-fbcf-7ad1-8edd-c4c22524182b` (harden before PR #46 integration — extends the same branch)
**Date:** 2026-10-09 · **Status:** findings + minimal-fix proposal delivered on branch `review/k1-get-klines-hardening`; **stacked hardening PR combining M1 M2 C1 T1 C2** targets `feat/k1-brain-klines` (no edits to the author's branch).

## 0. Exact tested heads

- **PR #46 exact head (reviewed):** `049086ce881b809c6d19695835d01b43946b2b23` (`feat/k1-brain-klines`, base `fbc80f5`).
- Reviewed in a **detached worktree** at that exact SHA; all tests re-run there read-only w.r.t. the author's branch.
- Proposal branch based on the same SHA; only additive changes (tools.py hardening, tests, conftest fixture, docs).
- **No** live Binance requests, **no** live cluster access, **no** edits to `feat/k1-brain-klines`, PR #48 files, or D6 work; **no** merge/deploy.

## 1. Method

1. Read-only code review of the full PR diff (tools.py, brain/toolset.py, brain/agent.py, tests).
2. Reproduced the test suite on the exact head.
3. Ran additive, fully-mocked adversarial probes for every required focus area (no external traffic).

## 2. Verification results

| Check | Result |
|---|---|
| `uv run pytest tests/test_get_klines.py` (PR tests, head `049086ce`) | **8 passed** |
| Author-claimed set `test_get_klines.py test_brain.py test_brain_api.py` | **37 passed, 1 failed** (`test_brain_stream_endpoint_emits_typed_sse_events`) |
| Same failing test on **base `fbc80f5`** | **fails identically** (Redis `AuthenticationError` at harness setup — environment prerequisite, unrelated to PR #46; author's "38 passed" likely ran with a working local Redis/env) |
| **After hardening** `tests/test_get_klines.py` + `tests/test_get_klines_hardening.py` | **22 passed** (6 author tests adapted to the new C1 contract + 16 hardening tests, 14 of them new) |
| `test_brain.py` / `test_brain_api.py` after hardening | **22 passed 1 failed** (same pre-existing env failure) / **7 passed** |
| Full `tests/` tree locally | All non-klines failures/errors are the Redis `HELLO … AUTH` env class (worktree test REDIS_URL carries no password; live queue/outbox/stream/scanner suites can't auth). Same root cause as the known SSE failure; **none touch the changed files**. |
| `uvx ruff@…` on changed files | pass |
| `git diff --check` | pass |

## 3. Findings (prioritized, with evidence at head `049086ce`)

### Critical
None. The SSRF surface is closed as designed and verified:
- host constants (`exchangeInfo` `tools.py:118`, `klines` `tools.py:171`); `httpx` URL-encodes `params`, redirects not followed; symbol gated by `^[A-Z0-9]{3,20}$` (`tools.py:99,141`) after venue-suffix split (`:140`); membership check `:144-146`; no API keys; no secrets logged (only `logger.warning` with the exception at `:126`).
- **Brain-only exposure verified:** `GET_KLINES = _register(RiskClass.READ)` (`tools.py:463`); added only to `BRAIN_TOOLS` (`brain/toolset.py:69-71`); debate fleet uses a fixed `DEBATE_TOOLS` tuple without it (`debate_fleet.py:22`); no other module imports it.

### Major — robustness (fixes in the proposal)
1. **M1 — Weight gate is post-request only; no shared budget, no pre-flight.** The `X-MBX-USED-WEIGHT-1M > 3000` check (`tools.py:173-180`) runs *after* `httpx.get` (`:171`) has already consumed weight. A **429 carrying weight >3000 is masked** by the weight message (probe 2) — the actionable `Retry-After` is lost. Unlocked caches (`:107,:227`) make a burst/stampede overshoot the budget before any gate trips. → **Fixed**: recorded-weight pre-flight gate under a lock; 429/418 handled before the gate.
2. **M2 — exchangeInfo membership fails open on outage.** `_get_valid_symbols()` returns `set()` when a refresh fails with no cache (`tools.py:129`), and the membership check is skipped for an empty set (`:145`). Probe 3: with exchangeInfo down, an unlisted symbol is forwarded to `/klines` (Binance 400s it — not exploitable, but the "valid active trading pair" guarantee silently lapses and garbage symbols burn weight). → **Fixed**: empty set ⇒ refusal, no request issued.

### Moderate
3. **C1 — Closed-candle boundary depends on the client clock.** `now_ms = time.time()*1000` (`tools.py:190`) vs server close_times; filter `close_time <= now_ms` (`:193`). Probe 4b: client clock 1h ahead → the still-forming candle reported as **closed**; probe 7: client 1h behind → the just-closed candle is **dropped**. → **Fixed**: boundary = Binance response's RFC 7231 `Date` header, second-granularity conservative, fail-closed on missing/unparseable. (Details §4.)
4. **T1 — `in_feed` is hardcoded, not derived from the live feed universe.** `_FED_SYMBOLS = {"BTCUSDT","ETHUSDT"}` (`tools.py:101,:201`) duplicates the ingest-universe config and will lie at K1 symbol expansion. → **Fixed**: read the authoritative live `ingest:universe` (Redis) with a fail-soft 60s cache; unavailable ⇒ `"unknown"`, never a guess. (Details §4.)
5. **C2 — Caches are unbounded and unlocked.** `_KLINES_CACHE` (`:107`) never evicts; TTL only governs reads (`:161-164`). Missing per-key locks allow stampedes that amplify M1. → **Fixed**: LRU-bounded (256) OrderedDict + one lock serializing cache-read, pre-flight gate and fetch — the gate cannot be bypassed by parallel requests. (Details §4.)
6. **C3 — `limit=500` returns 499 candles.** `fetch_limit = min(limit_val + 2, 500)`; with a forming candle the `+2` headroom is capped away (probe 9: requested 500 → `candle_count` 499). → **Deferred** (see §5): one-line, low severity.

### Low
7. **Retry-After** is reported but not acted on (no backoff — acceptable for a tool); value passed through unvalidated (Binance sends numeric). **418 handled but untested** (probe 1 confirms the message).
8. **exchangeInfo ignores `permissions`**: a `TRADING`-status, non-SPOT symbol passes membership then 400s on klines (caught by the error branch).
9. **Test gaps** vs the required failure paths: 418; non-200 (400/500); all-candles-forming; boundary clock skew both directions; empty-set fail-open; "fed" flag path; cache-TTL expiry (verified working, probe 5); cap-at-500 count; non-numeric weight header (benign, probe 6); 429-mask (probe 2). *(The new hardening tests cover the previously-missing skew, fail-open, fed-flag, TTL-expiry and 429-mask cases; 418, generic non-200 and the 499-cap remain enumerated gaps.)*

## 4. Hardening proposal — branch `review/k1-get-klines-hardening` (stacked PR → `feat/k1-brain-klines`)

Authorized by task `01a121ba`; pushed as an **isolated review branch** off `049086ce` (does **not** touch `feat/k1-brain-klines`). Changes:

| # | File | Change |
|---|---|---|
| 1 | `packages/agents/cryptonite_agents/tools.py` | **M1** Shared-budget globals `_WEIGHT_LIMIT/_LAST_USED_WEIGHT/_LAST_WEIGHT_AT/_WEIGHT_WINDOW_S`; **pre-flight gate** refuses a new request (before any HTTP) while the last recorded weight is at/over the cap within the 60s window |
| 2 | same | Weight header recorded from **every** response for the next call's pre-flight; **429/418 handled before the weight gate** so `Retry-After` is never masked; 200-with-over-cap still fails closed |
| 3 | same | **M2 Fail-closed membership**: empty `valid_symbols` ⇒ refusal, no request issued; stale cache still honored on refresh failure |
| 4 | same | **C1 Server-time boundary**: `_parse_server_date_ms()` parses the Binance response's RFC 7231 `Date` header; closed-candle filter is `close_time <= server_time_ms` (second granularity — conservative: a candle that closed inside the current server second is dropped, never reported closed early); **missing/unparseable `Date` ⇒ fail-closed error, nothing cached** |
| 5 | same | **T1 `in_feed` truthfulness**: `_fed_symbols()` reads Redis `ingest:universe` (same key + `IngestUniverse.parse` contract as API creation checks; TTL 180s live), 60s fail-soft local cache; unavailable/unreadable ⇒ empty ⇒ `in_feed="unknown"`; `_FED_SYMBOLS` removed |
| 6 | same | **C2 Bounded cache + serialized gate**: `_KLINES_CACHE` → `OrderedDict` LRU (cap 256; `move_to_end` on hit, oldest evicted past the cap); one `threading.Lock` covers cache-read + pre-flight gate + HTTP fetch + weight record + cache write, so parallel calls cannot interleave around the budget check and identical concurrent calls coalesce to one request |
| 7 | `tests/test_get_klines.py` | Author's two success-path mocks gain a `Date` header — required by the new C1 contract (every 200 response must carry server time); no behavioral change |
| 8 | `tests/test_get_klines_hardening.py` | **16 tests**: 4 previous + 14 new — clock-independence (±1 day both directions ⇒ identical candle selection), boundary-at-second included, missing/unparseable `Date` fail-closed (nothing cached), `_fed_symbols` unit (authoritative parse; degrade-to-empty on failure), `in_feed` unknown/fed-by-universe, LRU bound+eviction order, 4-thread burst ⇒ exactly 1 HTTP call under the gate |
| 9 | `tests/conftest.py` | additive autouse fixture resetting shared-weight **and** `_IN_FEED_CACHE` globals (required so the PR's threshold test doesn't bleed 3005 into neighbors, and the cached universe can't leak between tests) |

**Design rationale for the C1 contract (lead-approved option):** the Binance response's own `Date` header is per-response server time — no extra round-trip, no `/api/v3/time` bootstrap, no client clock anywhere in the boundary. Second granularity errs in the safe direction only (a just-closed candle is *dropped* for <1s, never reported closed early); unparseable/missing server time fails closed. `fetched_at` remains client-side wall-clock as a record of *when the tool ran* — it is never used as the boundary.

**Design rationale for C2:** serializing all `get_klines` calls behind one lock makes the shared-budget gate airtight — a request is issued only when the gate passed for that exact request under the same lock, and the weight from the previous response is visible to every waiter. Cost (~ms for cache hits; HTTP latency for misses, serialized) is acceptable for a low-frequency Brain tool and the cache doubles as a request coalescer.

Proposal verified: **22/22 klines-related tests**, brain suites unchanged (`22+1` env fail / `7`), ruff + `git diff --check` clean. Full tree red is the pre-existing Redis-auth env class, none in changed files.

## 5. Remaining / deferred

- **C3 (deferred on this branch):** `limit=500 → 499` under a forming candle. Fix is `fetch_limit = min(limit_val + 2, 502)` (Binance accepts up to 1000) or documenting the 499 semantics; left to the author to keep the proposal narrow. Low severity, one line.
- **Track for K1:** the enumerated missing failure-path tests (418, generic non-200, all-forming, 499-cap) and — if desired — a post-request `Retry-After` backoff.
- **Author's "38 passed" claim** remains not reproducible on the exact head; the one differing test fails identically on base (Redis-auth env prerequisite) — not a PR defect.
- Proposal is **unmerged and unpushed-to-mainline**: adopt via the stacked PR (or cherry-pick) at the author's/lead's discretion. No merge/deploy performed by agent.