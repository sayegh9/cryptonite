# D3: Structure-Based Stop/Target, Zero-Lookahead HTF, and Trigger Timeframes

**Status:** Completed & Validated (Phase D2/D3 Exit Gate Achieved across All Active Presets and Timeframes)  
**Task ID:** `01a115b8-0555-7e40-b20f-987dd0f8d56b`  
**Reference Plan:** `docs/product-review-plan.md` §D3 and `docs/product-review-2026-10-05.md` §5.2.1 (Finding F-H2)  
**Branch:** `feat/d3-structure-stop-target`  

---

## 1. Executive Summary

Phase D3 completes the candidate filtering and quality framework initiated in Phase D2. While D2 transitioned the scanner from continuous state-based triggers to event-based crossover triggers with cross-spec deduplication (achieving a single-spec reduction of 16.5x but an aggregate multi-spec reduction of 6.8x–9.5x below the 10–50x exit gate), D3 introduces structural risk/reward controls, zero-lookahead higher-timeframe (HTF) targets, and automatic trigger timeframe restrictions.

Key architectural deliverables:
1. **Structure-Based Stop & Target Levels**:
   - Replaced fixed ATR stops with structural Donchian levels:
     - **Long**: 20-bar Donchian channel lower band for stop; higher-timeframe (HTF) resistance (HTF Donchian high / HTF Bollinger upper) for target.
     - **Short**: 20-bar Donchian channel upper band for stop; HTF support (HTF Donchian low / HTF Bollinger lower) for target.
   - Stop distance bounded within `[0.5 * ATR, 4.0 * ATR]`, falling back to `2.0 * ATR` if structural stop is out of bounds or invalid.
   - Target distance bounded within `[1.5 * ATR, 6.0 * ATR]`, falling back to `3.0 * ATR` if structural target is out of bounds or invalid.
   - Fallback stop/target yields an exact Risk:Reward ratio of **1.50** (`3.0 / 2.0 = 1.50`).
   - Finite positive ATR validation (`atr > 0` and `math.isfinite(atr)`).
   - Quantized price levels matching instrument precision (`price_precision = 2`).
   - Full provenance tracked in candidate metrics (`stop_type`, `target_type`, `stop_distance`, `target_distance`, `risk_reward_ratio`).
2. **Zero-Lookahead HTF Target Filtering**:
   - Resampled HTF indicator DataFrames strictly filtered so that `pl.col("interval_end_exclusive_ns") <= signal_bar.interval_end_exclusive_ns`.
   - Forming, unclosed, or future HTF bars are never leaked to evaluate a signal bar. Verified with deterministic tests demonstrating identical candidate outputs regardless of future price shocks.
3. **Pre-Insert Risk:Reward Rejection Gate (`R:R >= 1.5`)**:
   - Evaluated in `ScannerWorker._execute_scan` prior to database transactions and candidate insertion.
   - Evaluates unrounded derived price ratio (`reward / risk >= Decimal("1.5")`) to prevent sub-threshold leakage (e.g. true 1.496 rounding to 1.50).
   - Candidate qualification happens **before** deduplication, deterministically ranking by `(instrument_id, timeframe, bar_close_inclusive_ns, -rr, id)` so higher R:R candidates win duplicate same-bar slots.
   - Rejections are logged with full details (reason, entry, stop, target, R:R).
4. **Automatic Trigger Timeframe Restriction**:
   - Automatic execution on `COMMITTED_BAR_EVENT` restricted strictly to `ALLOWED_AUTOMATIC_TIMEFRAMES = ("15m", "1h", "4h")`.
   - 1m bar streaming and ingestion are preserved for real-time exit monitoring, indicators, and resampling.
   - Built-in presets updated to allowed automatic timeframes (`bollinger-breakout` -> `["15m", "1h"]`, `scalp-multi-tf` -> `["15m", "1h", "4h"]`).
   - Prior D2 hashes registered in `PRESET_PREVIOUS_HASHES` so custom user specs are preserved without overwrite during operator upgrades.
5. **Phase D2/D3 Volume Reduction Gate (Empirical Replay)**:
   - Evaluated using reproducible read-only benchmark (`scripts/replay_d3_benchmark.py`) across all active presets and automatic timeframes:
     - **Oct 2–4 Baseline (3 days, 4,321 1m bars each)**:
       * **15m Combined (BTC+ETH)**: 228 (D1 state) -> 34 (D2 crossover) -> **6 (D3 structure)** = **38.0x reduction**.
       * **All-TF Combined (15m+1h+4h)**: 278 (D1 state) -> 37 (D2 crossover) -> **7 (D3 structure)** = **39.7x reduction** (4.6x vs D2).
     - **Oct 1–7 Extended Holdout (7 days, 10,079 1m bars each)**:
       * **15m Combined (BTC+ETH)**: 487 (D1 state) -> 73 (D2 crossover) -> **21 (D3 structure)** = **23.2x reduction**.
       * **All-TF Combined (15m+1h+4h)**: 624 (D1 state) -> 85 (D2 crossover) -> **26 (D3 structure)** = **24.0x reduction** (2.8x vs D2).
     - **Exit Gate Result**: Both the 15m scope (23.2x–38.0x) and the full All-TF scope (24.0x–39.7x) achieve the **10–50x exit gate**.

---

## 2. Component Changes

### 2.1 Indicators (`packages/scanner/cryptonite_scanner/indicators.py`)
- Added `calc_donchian_channel(high_col="high", low_col="low", period=20) -> tuple[pl.Expr, pl.Expr, pl.Expr]`:
  - `lower = pl.col(low_col).rolling_min(window_size=period)`.
  - `upper = pl.col(high_col).rolling_max(window_size=period)`.
  - `middle = (upper + lower) / 2.0`.

### 2.2 Models & Configurations (`packages/scanner/cryptonite_scanner/models.py`)
- Added constants:
  - `ALLOWED_AUTOMATIC_TIMEFRAMES = ("15m", "1h", "4h")`
  - `MIN_RISK_REWARD_RATIO = 1.5`
- Extended `RiskConfig`:
  - `direction: Literal["long", "short"] = "long"`
  - `stop_mode: Literal["atr", "structure"] = "structure"`
  - `donchian_lookback: int = 20`
  - `min_atr_mult: float = 0.5`
  - `max_atr_mult: float = 4.0`
  - `fallback_stop_atr_mult: float = 2.0`
  - `fallback_target_atr_mult: float = 3.0`
  - `min_risk_reward: float = 1.5`
- Extended `SignalCandidateRead`:
  - Added `risk_reward_ratio: Decimal | None = None` field.
  - Added `@model_validator(mode="after")` to calculate unrounded `reward / risk`.

### 2.3 Scanner Engine (`packages/scanner/cryptonite_scanner/engine.py`)
- In `enrich_indicators`:
  - Added `donchian_lower`, `donchian_middle`, and `donchian_upper` using `calc_donchian_channel(period=20)`.
- In `evaluate`:
  - Extended signature with `htf_df: pl.DataFrame | None = None` and `filter_min_rr: bool = False`.
  - Enforced finite positive ATR check: validates `atr > 0` and `math.isfinite(atr)`.
  - Structure-based stop calculation:
    - Long: uses `donchian_lower`. Bounded within `[min_atr_mult * atr, max_atr_mult * atr]`. Falls back to `entry_price - fallback_stop_atr_mult * atr`.
    - Short: uses `donchian_upper`. Bounded within `[min_atr_mult * atr, max_atr_mult * atr]`. Falls back to `entry_price + fallback_stop_atr_mult * atr`.
  - HTF resistance/support target calculation:
    - Long: checks HTF Donchian upper, then HTF Bollinger upper. Bounded within `[1.5 * atr, 6.0 * atr]`. Falls back to `entry_price + fallback_target_atr_mult * atr`.
    - Short: checks HTF Donchian lower, then HTF Bollinger lower. Bounded within `[1.5 * atr, 6.0 * atr]`. Falls back to `entry_price - fallback_target_atr_mult * atr`.
  - Quantized `entry_price`, `stop_loss_price`, and `take_profit_price` to `price_precision` (default 2 decimals).
  - Derived price ratio `derived_rr = (reward / risk)` unrounded, filtering when `filter_min_rr and derived_rr < min_rr`.
  - Stored provenance in `metrics`: `stop_type`, `target_type`, `stop_distance`, `target_distance`, `risk_reward_ratio`.

### 2.4 Presets & API Hash Registration
- In `packages/scanner/cryptonite_scanner/presets.py`:
  - Updated `PRESET_BOLLINGER_BREAKOUT` timeframes from `["5m", "15m", "1h"]` to `["15m", "1h"]`.
  - Updated `PRESET_SCALP_MULTI_TF` timeframes from `["5m", "15m", "1h", "4h"]` to `["15m", "1h", "4h"]`.
- In `services/api/cryptonite_api/routes/scanners.py`:
  - Registered prior D2 SHA-256 hashes in `PRESET_PREVIOUS_HASHES` to preserve customized user presets during operator upgrade transitions.

### 2.5 Scanner Worker (`services/scanner/cryptonite_scanner_service/worker.py`)
- Restricted automatic execution on `COMMITTED_BAR_EVENT`:
  - Filters universe timeframes strictly to `ALLOWED_AUTOMATIC_TIMEFRAMES = ("15m", "1h", "4h")`.
- HTF Bar Resampling & Lookahead Protection:
  - Resamples 1m bars into higher timeframe based on `HTF_MAP = {"15m": "1h", "1h": "4h", "4h": "1d"}`.
  - Strictly filters HTF rows to bars fully closed by the signal bar (`interval_end_exclusive_ns <= signal_bar_interval_end_ns`).
  - Passes enriched `htf_df` to `ScannerEngine.evaluate`.
- Pre-Insert Qualification Before Deduplication:
  - Rejects candidates failing unrounded `(reward / risk) >= Decimal("1.5")` before deduplication.
  - Sorts qualified candidates by `(instrument_id, timeframe, bar_close_inclusive_ns, -rr, id)` so highest R:R setup wins duplicate bar slots within that spec execution run.
  - Across concurrent runs for different specs, deduplication follows the transactional Postgres advisory lock (`pg_try_advisory_xact_lock`) / first-committer path, ensuring no duplicate candidates are committed concurrently across specs without claiming a global multi-spec highest-R:R auction.

---

## 3. Empirical Replay Results across All Active Presets & Timeframes

Replay benchmark executed using `scripts/replay_d3_benchmark.py` across all 5 built-in presets (`rsi-oversold-reversal`, `trend-pullback`, `bollinger-breakout`, `macd-momentum-shift`, `scalp-multi-tf`) on both assets:

### 3.1 Oct 2–4 Baseline Window (3 Days, 4,321 1m bars each)
- **Data Coverage**: BTCUSDT (4,321 1m, 287 15m, 71 1h, 17 4h); ETHUSDT (4,321 1m, 287 15m, 71 1h, 17 4h).
- **BTCUSDT**:
  - D1 State Baseline: 15m = 111 raw (99 deduped) | All TFs = 136 raw (119 deduped)
  - D2 Crossover Baseline: 15m = 13 | All TFs = 14 (deduped: 13)
  - D3 Generated: 14 raw | Filter rejections: `{'below_rr_threshold': 11}` (invalid ATR discarded at engine level) | Qualified: 3
  - D3 Accepted (deduped): **3** (15m=3, 1h=0, 4h=0)
  - **Reduction**: 15m = **37.0x raw** (**33.0x deduped like-for-like**) | All-TF = **45.3x raw** (**39.7x deduped like-for-like**, 4.3x vs D2)
  - Candidate retention rate: **23.1%** | Mean R:R = **1.50**
- **ETHUSDT**:
  - D1 State Baseline: 15m = 117 raw (106 deduped) | All TFs = 142 raw (128 deduped)
  - D2 Crossover Baseline: 15m = 21 | All TFs = 23 (deduped: 19)
  - D3 Generated: 23 raw | Filter rejections: `{'below_rr_threshold': 18}` | Qualified: 5
  - D3 Accepted (deduped): **4** (15m=3, 1h=1, 4h=0)
  - **Reduction**: 15m = **39.0x raw** (**35.3x deduped like-for-like**) | All-TF = **35.5x raw** (**32.0x deduped like-for-like**, 4.8x vs D2)
  - Candidate retention rate: **21.1%** | Mean R:R = **1.50**
- **Combined (BTC + ETH)**:
  - 15m D1 Baseline: 228 raw (205 deduped) -> D3 Accepted: **6** (**38.0x raw**, **34.2x deduped like-for-like**)
  - All-TF D1 Baseline: 278 raw (247 deduped) -> D3 Accepted: **7** (**39.7x raw**, **35.3x deduped like-for-like**, 4.6x vs D2)
  - Combined candidate retention rate: **21.9%**

### 3.2 Oct 1–7 Extended Holdout Window (7 Days, 10,079 1m bars each)
- **Data Coverage**: BTCUSDT (10,079 1m, 669 15m, 165 1h, 40 4h); ETHUSDT (10,078 1m, 668 15m, 164 1h, 40 4h).
- **BTCUSDT**:
  - D1 State Baseline: 15m = 240 raw (220 deduped) | All TFs = 313 raw (286 deduped)
  - D2 Crossover Baseline: 15m = 35 | All TFs = 39 (deduped: 34)
  - D3 Generated: 39 raw | Filter rejections: `{'below_rr_threshold': 27}` | Qualified: 12
  - D3 Accepted (deduped): **12** (15m=10, 1h=2, 4h=0)
  - **Reduction**: 15m = **24.0x raw** (**22.0x deduped like-for-like**) | All-TF = **26.1x raw** (**23.8x deduped like-for-like**, 2.8x vs D2)
  - Candidate retention rate: **35.3%** | Mean R:R = **2.16** (max 3.27)
  - Stop provenance: 6 atr_fallback, 6 donchian_low
  - Target provenance: 8 atr_fallback, 3 htf_donchian_high, 1 htf_bb_upper
- **ETHUSDT**:
  - D1 State Baseline: 15m = 247 raw (225 deduped) | All TFs = 311 raw (284 deduped)
  - D2 Crossover Baseline: 15m = 38 | All TFs = 46 (deduped: 40)
  - D3 Generated: 46 raw | Filter rejections: `{'below_rr_threshold': 31}` | Qualified: 15
  - D3 Accepted (deduped): **14** (15m=11, 1h=3, 4h=0)
  - **Reduction**: 15m = **22.5x raw** (**20.5x deduped like-for-like**) | All-TF = **22.2x raw** (**20.3x deduped like-for-like**, 2.9x vs D2)
  - Candidate retention rate: **35.0%** | Mean R:R = **1.83** (max 2.89)
  - Stop provenance: 8 atr_fallback, 6 donchian_low
  - Target provenance: 8 atr_fallback, 5 htf_donchian_high, 1 htf_bb_upper
- **Combined (BTC + ETH)**:
  - 15m D1 Baseline: 487 raw (445 deduped) -> D3 Accepted: **21** (**23.2x raw**, **21.2x deduped like-for-like**)
  - All-TF D1 Baseline: 624 raw (570 deduped) -> D3 Accepted: **26** (**24.0x raw**, **21.9x deduped like-for-like**, 2.8x vs D2)
  - Combined candidate retention rate: **35.1%**

### 3.3 Metric Reconciliation: Preliminary Draft (5+5, 22.9x) vs Final Multi-TF Benchmark (3+3, 38.0x)
- **Preliminary Exploration Draft**:
  - Evaluated only 15m bars across 4 presets prior to adding unrounded R:R filtering and qualification-before-dedupe ordering.
  - Resulted in 5 BTC + 5 ETH = 10 accepted candidates on 15m out of 229 D1 candidates (**22.9x reduction**).
- **Finalized Reproducible Multi-TF Benchmark (`scripts/replay_d3_benchmark.py`)**:
  - Covers all 5 active presets across all automatic timeframes (15m, 1h, 4h).
  - Strictly enforces unrounded derived price ratio (`reward / risk >= 1.50`) and worker-equivalent qualification-before-deduplication.
  - Result:
    * 15m Window: 3 BTC + 3 ETH = **6 accepted candidates** out of 228 raw (205 deduped) D1 candidates (**38.0x raw**, **34.2x deduped like-for-like**).
    * All-TF Window: 3 BTC + 4 ETH = **7 accepted candidates** out of 278 raw (247 deduped) D1 candidates (**39.7x raw**, **35.3x deduped like-for-like** vs D1; 4.6x vs D2 crossover).
    * 7-day Extended Holdout Window: 21 accepted on 15m (**23.2x raw**, **21.2x deduped like-for-like**), 26 accepted All-TF (**24.0x raw**, **21.9x deduped like-for-like**).
  - Both raw and like-for-like deduped metrics solidly satisfy the 10–50x exit gate (21.2x–35.3x deduped like-for-like; 23.2x–39.7x raw across all evaluated scopes).

---

## 4. Test Verification Summary

All tests executed and verified against clean isolated CI fixtures:
- `tests/test_scanner_structure_d3.py`: **12 / 12 passed**
  - Includes `test_unrounded_derived_price_ratio_boundary` (1.496 rejected, 1.500 accepted)
  - Includes `test_worker_qualification_before_dedupe_two_specs_same_bar` (same-bar higher R:R candidate prioritized)
  - Includes `test_deterministic_no_lookahead_htf` (future HTF bars cannot alter candidate)
  - Includes `test_fallback_exact_risk_reward_ratio` (exact 1.50 fallback)
  - Includes `test_automatic_trigger_timeframe_restriction` (automatic stream events restricted to 15m/1h/4h)
  - Includes `test_integration_stream_message_mixed_universe_5m_15m` (integration regression proving a 15m event on mixed `[5m, 15m]` evaluates/enqueues ONLY 15m, 0 5m candidates/outbox, and 5m events are ignored)
- Full Targeted Regression Suite: **31 / 31 passed**
  - `tests/test_benchmarks.py::test_polars_scanner_engine_p95_latency`: passed (5.04s)
  - `tests/test_scanner_fresh_enqueue.py`: 18 / 18 passed
  - `tests/test_scanner_structure_d3.py`: 12 / 12 passed
- GitHub Actions CI (PR #43): **6 / 6 checks passing green** (`ruff`, `web`, `pytest`, `e2e-smoke`, `verify`, `CodeRabbit`).
- Lint & Style: `ruff check` clean (0 errors), `git diff --check` clean.
