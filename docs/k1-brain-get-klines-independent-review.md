# Independent Security & Correctness Review — K1 `get_klines` (PR #46)

**Reviewer:** OpenCode (slot `01a112ce-9b06-7142-8401-a7fb6b8b5c50`) · **Task:** `01a121a6-d2bb-7861-b70f-122244188f5d`
**Date:** 2026-10-09 · **Status:** findings delivered; **minimal-fix proposal on branch `review/k1-get-klines-hardening`** (OPEN, unmerged, non-author branch).

## 0. Exact tested head

- **PR #46 exact head:** `049086ce881b809c6d19695835d01b43946b2b23` (`feat/k1-brain-klines`, base `fbc80f5`)
- Reviewed in a **detached worktree** at that exact SHA; all tests re-run there read-only w.r.t. the author's branch.
- Proposal branch based on the same SHA; only additive changes (tools.py hardening, new test file, additive conftest fixture).
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
| Proposal branch test set (PR tests + `test_get_klines_hardening.py` + both brain suites) | **41 passed, 1 failed** (same pre-existing env failure) |
| `uvx ruff` on changed files | pass |
| `git diff --check` | pass |

## 3. Findings (prioritized, with evidence at head `049086ce`)

### Critical
None. The SSRF surface is closed as designed and verified:
- host constants (`exchangeInfo` `tools.py:118`, `klines` `tools.py:171`); `httpx` URL-encodes `params`, redirects not followed; symbol gated by `^[A-Z0-9]{3,20}$` (`tools.py:99,141`) after venue-suffix split (`:140`); membership check `:144-146`; no API keys; no secrets logged (only `logger.warning` with the exception at `:126`).
- **Brain-only exposure verified:** `GET_KLINES = _register(RiskClass.READ)` (`tools.py:463`); added only to `BRAIN_TOOLS` (`brain/toolset.py:69-71`); debate fleet uses a fixed `DEBATE_TOOLS` tuple without it (`debate_fleet.py:22`); no other module imports it.

### Major — robustness (proposal fixes both, see §4)
1. **M1 — Weight gate is post-request only; no shared budget, no pre-flight.** The `X-MBX-USED-WEIGHT-1M > 3000` check (`tools.py:173-180`) runs *after* `httpx.get` (`:171`) has already consumed weight. No recorded weight, no in-process budget, no pre-request refusal. Also, a **429 carrying weight >3000 is masked** by the weight message (probe 2) — the actionable `Retry-After` is lost. Unlocked caches (`:107,:227`) make a concurrent burst/stampede overshoot the budget before any gate trips.
2. **M2 — exchangeInfo membership fails open on outage.** `_get_valid_symbols()` returns `set()` when a refresh fails with no cache (`tools.py:129`), and the membership check is skipped for an empty set (`tools.py:145` `if valid_symbols and …`). Probe 3: with exchangeInfo down, an unlisted symbol is forwarded to `/klines` (Binance 400s it — not exploitable, but the advertised "valid active trading pair" guarantee silently lapses and garbage symbols burn weight).

### Moderate
3. **C1 — Closed-candle boundary depends on the client clock.** `now_ms = time.time()*1000` (`tools.py:190`) vs server close_times; filter `close_time <= now_ms` (`:193`). Probe 4b: client clock 1h ahead → the still-forming candle reported as **closed** (`last_closed_close_time` = forming candle's future close_time, `candle_count` includes it). Probe 7: client 1h behind → the just-closed candle is **dropped** (3→2 candles). Use a server-time boundary (e.g. `/api/v3/time`, or treat only candles whose close_time < the *second-to-last* close_time as settled).
4. **T1 — `in_feed` is hardcoded, not derived from the live feed universe.** `_FED_SYMBOLS = {"BTCUSDT","ETHUSDT"}` (`tools.py:101,:201`) duplicates the ingest-universe config. Trivially accurate today, but it will lie the moment the universe changes (K1 plan expands it). Read the live `ingest:universe` / shared constant instead.
5. **C2 — Caches are unbounded and unlocked.** `_KLINES_CACHE` (`:107`) never evicts; TTL only governs reads (`:161-164`). Long-lived process with arbitrary `(symbol, interval, limit)` combos grows memory; missing per-key locks allow stampedes that amplify M1.
6. **C3 — `limit=500` returns 499 candles.** `fetch_limit = min(limit_val + 2, 500)` (`tools.py:169`); with a forming candle the `+2` headroom is capped away (probe 9: requested 500 → `candle_count` 499).

### Low
7. **Retry-After** is reported but not acted on (no backoff — acceptable for a tool, but "honor" is report-only); value passed through unvalidated (Binance sends numeric). **418 handled but untested** (probe 1 confirms the message).
8. **exchangeInfo ignores `permissions`**: a `TRADING`-status, non-SPOT symbol passes membership then 400s on klines (caught by the error branch, `:186-187`).
9. **Test gaps** vs the required failure paths: 418; non-200 (400/500); all-candles-forming; boundary clock skew both directions; empty-set fail-open; "fed" flag path; cache-TTL expiry (verified working, probe 5); cap-at-500 count; non-numeric weight header (benign, probe 6); 429-mask (probe 2).

## 4. Minimal-fix proposal — branch `review/k1-get-klines-hardening`

Authorized by task; pushed as an **isolated review branch** off `049086ce` (does **not** touch `feat/k1-brain-klines`). Changes:

| # | File | Change |
|---|---|---|
| 1 | `packages/agents/cryptonite_agents/tools.py` | Shared-budget globals `_WEIGHT_LIMIT/_LAST_USED_WEIGHT/_LAST_WEIGHT_AT/_WEIGHT_WINDOW_S`; **pre-flight gate** — refuses a new request (before any HTTP) while the last recorded weight is at/over the cap within the 60s window |
| 2 | same | Weight header recorded from **every** response for the next call's pre-flight |
| 3 | same | **429/418 handled before the weight gate** so `Retry-After` is never masked (probe 2 now correct); 200-with-over-cap still fails closed (original semantics kept) |
| 4 | same | **Fail-closed membership**: empty `valid_symbols` ⇒ refusal, no request issued (probe 3 now correct); stale cache still honored on refresh failure |
| 5 | `tests/test_get_klines_hardening.py` | 4 new tests (429-not-masked; pre-flight skip with exactly 1 HTTP call; window expiry re-allows; fail-closed) |
| 6 | `tests/conftest.py` | additive autouse fixture resetting the shared-weight globals **required to integrate the shared-budget state** into the PR's test file (otherwise the threshold test trips the gate inside its neighbor `tests/test_get_klines.py`) |

**Integration note for author/lead:** apply 1–4 to the PR; also port the reset fixture (6) into `test_get_klines.py` (or keep it in conftest) or the PR suite's `test_get_klines_used_weight_threshold_block` will bleed state into `test_get_klines_rate_limit_429_retry_after`.

Proposal verified: PR tests + hardening tests + both brain suites = **41 passed** (only the pre-existing env failure), ruff + `git diff --check` clean.

## 5. Recommendations

- **Before merge:** apply M1+M2 hardening (proposal branch ready to cherry-pick) — they are robustness gaps in the tool's own advertised rate-limit and membership guarantees, not exploits.
- **Track for K1:** C1 server-time boundary, T1 feed-derived `in_feed`, C2 bounded caches, C3 cap-at-500.
- **Add missing failure tests** enumerated in §3.9 (many are one-mock lines).
- Author's "38 passed" claim: not reproducible in this environment on the exact head; the one differing test fails identically on base (Redis-auth env prerequisite) — not a PR defect.