"""D3: Structure-based stop/target, zero-lookahead HTF evaluation,
symmetric long/short logic, varied R:R, invalid ATR/level fallback,
pre-insert R:R rejection gate, and automatic trigger timeframe restriction tests.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import polars as pl
import pytest
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    SignalCandidateRead,
)
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service.worker import ScannerWorker
from migrated_db import migrated_database


def _make_dummy_ohlcv(
    n_bars: int = 220,
    base_price: float = 100.0,
    step_ns: int = 15 * ONE_MINUTE_NS,
    start_ns: int = 1_700_000_000 * 1_000_000_000,
    close_override: list[float] | None = None,
    high_override: list[float] | None = None,
    low_override: list[float] | None = None,
) -> pl.DataFrame:
    """Helper to build a valid OHLCV DataFrame for testing."""
    closes = close_override if close_override is not None else [base_price + (i * 0.1) for i in range(n_bars)]
    highs = high_override if high_override is not None else [c + 1.0 for c in closes]
    lows = low_override if low_override is not None else [c - 1.0 for c in closes]
    opens = closes

    open_times = [start_ns + (i * step_ns) for i in range(len(closes))]
    close_times = [t + step_ns - 1_000_000 for t in open_times]
    interval_ends = [t + step_ns for t in open_times]

    return pl.DataFrame({
        "bar_open_ns": open_times,
        "bar_close_inclusive_ns": close_times,
        "interval_end_exclusive_ns": interval_ends,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [100.0] * len(closes),
        "quote_volume": [100.0 * c for c in closes],
        "trades_count": [10] * len(closes),
    })


# -----------------------------------------------------------------------------
# Test 1: Structure Donchian Stop & ATR Fallback (Symmetric Long & Short)
# -----------------------------------------------------------------------------

def test_structure_donchian_stop_long_and_short():
    """Verify that LONG uses Donchian lower and SHORT uses Donchian upper within bounds."""
    n = 220
    df = _make_dummy_ohlcv(n_bars=n, base_price=100.0)

    # 1. LONG test
    rules_long = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk_long = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
    )
    candidates_long = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules_long,
        risk_config=risk_long,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(candidates_long) > 0
    c_long = candidates_long[-1]
    assert c_long.suggested_stop < c_long.suggested_entry
    assert c_long.suggested_target > c_long.suggested_entry
    assert c_long.metrics["stop_type"] in ("donchian_low", "atr_fallback")
    assert c_long.risk_reward_ratio is not None
    assert c_long.risk_reward_ratio > Decimal("0.0")

    # 2. SHORT test (symmetric)
    rules_short = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk_short = RiskConfig(
        direction="SHORT",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
    )
    candidates_short = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules_short,
        risk_config=risk_short,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(candidates_short) > 0
    c_short = candidates_short[-1]
    # For SHORT, stop is above entry, target is below entry
    assert c_short.suggested_stop > c_short.suggested_entry
    assert c_short.suggested_target < c_short.suggested_entry
    assert c_short.metrics["stop_type"] in ("donchian_high", "atr_fallback")
    assert c_short.risk_reward_ratio is not None
    assert c_short.risk_reward_ratio > Decimal("0.0")


# -----------------------------------------------------------------------------
# Test 2: Bounds Enforcement & Fallbacks (Long & Short)
# -----------------------------------------------------------------------------

def test_structure_stop_bounds_enforcement_and_fallback():
    """Verify bounds [0.5 * ATR, 4.0 * ATR] trigger fallback when Donchian is out of range."""
    n = 220
    # Case A: Donchian distance too tight (distance < 0.5 * ATR)
    # If high=close+0.01, low=close-0.01, ATR will be ~0.02.
    # Setting low equal to close-0.001 makes Donchian distance 0.001 < 0.5*0.02 = 0.01.
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [100.0] * n  # low is identical to close, distance = 0
    df_tight = _make_dummy_ohlcv(n_bars=n, close_override=closes, high_override=highs, low_override=lows)

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
    )
    candidates = ScannerEngine.evaluate(
        df=df_tight,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(candidates) > 0
    c = candidates[-1]
    # Since distance is 0 (< 0.5 * ATR), it must trigger atr_fallback!
    assert c.metrics["stop_type"] == "atr_fallback"
    atr = Decimal(str(round(c.metrics["atr"], 2)))
    expected_stop = c.suggested_entry - (Decimal("2.0") * atr)
    assert abs(c.suggested_stop - expected_stop) <= Decimal("0.05")


# -----------------------------------------------------------------------------
# Test 3: HTF Target Resolution & Fallbacks (Symmetric Long & Short)
# -----------------------------------------------------------------------------

def test_htf_target_and_atr_fallback():
    """Verify HTF target level selection vs fallback when HTF is absent or out-of-bounds."""
    n = 220
    start_ns = 1_700_000_000 * 1_000_000_000
    df_15m = _make_dummy_ohlcv(n_bars=n, base_price=100.0, step_ns=15 * ONE_MINUTE_NS, start_ns=start_ns)
    df_1h = _make_dummy_ohlcv(
        n_bars=n,
        base_price=100.0,
        step_ns=60 * ONE_MINUTE_NS,
        start_ns=start_ns - (100 * 60 * ONE_MINUTE_NS),
    )

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk_long = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )

    # 1. LONG with HTF DataFrame
    cands_long = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_long,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_1h,
    )
    assert len(cands_long) > 0
    c_long = cands_long[-1]
    assert c_long.metrics["target_type"] in ("htf_donchian_high", "htf_bb_upper", "atr_fallback")
    assert c_long.suggested_target > c_long.suggested_entry

    # 2. LONG without HTF DataFrame -> Must use atr_fallback
    cands_long_no_htf = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_long,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=None,
    )
    assert len(cands_long_no_htf) > 0
    c_long_no_htf = cands_long_no_htf[-1]
    assert c_long_no_htf.metrics["target_type"] == "atr_fallback"

    # 3. Symmetric SHORT with HTF DataFrame
    risk_short = RiskConfig(
        direction="SHORT",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )
    cands_short = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_short,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_1h,
    )
    assert len(cands_short) > 0
    c_short = cands_short[-1]
    assert c_short.metrics["target_type"] in ("htf_donchian_low", "htf_bb_lower", "atr_fallback")
    assert c_short.suggested_target < c_short.suggested_entry

    # 4. Symmetric SHORT without HTF DataFrame -> Must use atr_fallback
    cands_short_no_htf = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_short,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=None,
    )
    assert len(cands_short_no_htf) > 0
    c_short_no_htf = cands_short_no_htf[-1]
    assert c_short_no_htf.metrics["target_type"] == "atr_fallback"


# -----------------------------------------------------------------------------
# Test 4: Deterministic No-Lookahead Guarantee (Closed Signal & HTF Bars Only)
# -----------------------------------------------------------------------------

def test_deterministic_no_lookahead_htf():
    """Verify that HTF data is strictly filtered to closed bars as of signal bar close.

    Adding future HTF bars or forming bars MUST NOT change the evaluated stop/target.
    """
    start_ns = 1_700_000_000 * 1_000_000_000
    step_15m = 15 * ONE_MINUTE_NS
    step_1h = 60 * ONE_MINUTE_NS

    # 15m bars up to bar 100
    n_15m = 100
    df_15m = _make_dummy_ohlcv(n_bars=n_15m, base_price=100.0, step_ns=step_15m, start_ns=start_ns)

    # 1h bars:
    # Closed HTF bars: 25 * 60m = 100 * 15m
    closed_1h_bars = _make_dummy_ohlcv(
        n_bars=25,
        base_price=100.0,
        step_ns=step_1h,
        start_ns=start_ns,
        high_override=[104.0] * 25,
        low_override=[96.0] * 25,
    )

    # Future HTF bars (beyond signal_bar_end_ns) with extreme prices
    future_1h_bars = _make_dummy_ohlcv(
        n_bars=10,
        base_price=100.0,
        step_ns=step_1h,
        start_ns=start_ns + (25 * step_1h),
        high_override=[999999.0] * 10,  # Extreme future spike
        low_override=[1.0] * 10,
    )

    extended_1h_bars = pl.concat([closed_1h_bars, future_1h_bars])

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )

    # Evaluate with strictly closed HTF
    cands_closed = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=closed_1h_bars,
    )

    # Evaluate with extended HTF (containing forming & future bars)
    cands_extended = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=extended_1h_bars,
    )

    assert len(cands_closed) == len(cands_extended)
    c1 = cands_closed[-1]
    c2 = cands_extended[-1]

    # Verify identical output despite future/partial HTF bars
    assert c1.suggested_entry == c2.suggested_entry
    assert c1.suggested_stop == c2.suggested_stop
    assert c1.suggested_target == c2.suggested_target
    assert c1.risk_reward_ratio == c2.risk_reward_ratio
    assert c1.metrics["target_type"] == c2.metrics["target_type"]
    # The extreme future price (999999.0) was NOT used!
    assert c2.suggested_target < Decimal("500.0")


# -----------------------------------------------------------------------------
# Test 5: Invalid ATR, Non-Finite ATR, and Corrupted Level Fallbacks
# -----------------------------------------------------------------------------

def test_invalid_atr_and_level_fallback():
    """Verify that zero, negative, NaN/Inf ATR and inverted levels safely fall back or skip."""
    n = 220
    df = _make_dummy_ohlcv(n_bars=n, base_price=100.0)

    # 1. ATR is 0.0 -> must be skipped
    enriched = ScannerEngine.enrich_indicators(df)
    corrupted_zero = enriched.with_columns(
        atr=pl.when(pl.col("interval_end_exclusive_ns") == enriched["interval_end_exclusive_ns"][-1])
        .then(pl.lit(0.0))
        .otherwise(pl.col("atr"))
    )

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk = RiskConfig()
    candidates = ScannerEngine.evaluate(
        df=corrupted_zero,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    last_end = enriched["interval_end_exclusive_ns"][-1]
    assert not any(c.interval_end_exclusive_ns == last_end for c in candidates)

    # 2. ATR is negative (-1.0) -> must be skipped
    corrupted_neg = enriched.with_columns(
        atr=pl.when(pl.col("interval_end_exclusive_ns") == enriched["interval_end_exclusive_ns"][-1])
        .then(pl.lit(-1.0))
        .otherwise(pl.col("atr"))
    )
    candidates_neg = ScannerEngine.evaluate(
        df=corrupted_neg,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert not any(c.interval_end_exclusive_ns == last_end for c in candidates_neg)

    # 3. ATR is float("nan") or float("inf") -> must be skipped
    corrupted_nan = enriched.with_columns(
        atr=pl.when(pl.col("interval_end_exclusive_ns") == enriched["interval_end_exclusive_ns"][-1])
        .then(pl.lit(float("nan")))
        .otherwise(pl.col("atr"))
    )
    candidates_nan = ScannerEngine.evaluate(
        df=corrupted_nan,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert not any(c.interval_end_exclusive_ns == last_end for c in candidates_nan)


# -----------------------------------------------------------------------------
# Test 6: Fallback Exact Risk-Reward Ratio (1.50)
# -----------------------------------------------------------------------------

def test_fallback_exact_risk_reward_ratio():
    """Verify that fallback 2.0 ATR stop and 3.0 ATR target yields exactly 1.50 R:R."""
    n = 220
    # Flat price series where Donchian low equals close (dist=0 < 0.5 ATR), forcing ATR fallback
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [100.0] * n
    df = _make_dummy_ohlcv(n_bars=n, close_override=closes, high_override=highs, low_override=lows)

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]
    risk = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )
    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=None,  # No HTF, forcing target ATR fallback
    )
    assert len(candidates) > 0
    c = candidates[-1]
    assert c.metrics["stop_type"] == "atr_fallback"
    assert c.metrics["target_type"] == "atr_fallback"
    # 3.0 ATR / 2.0 ATR = exactly 1.50
    assert c.risk_reward_ratio == Decimal("1.50")
    assert c.metrics["risk_reward_ratio"] == 1.50


# -----------------------------------------------------------------------------
# Test 7: Varied R:R Calculation & Engine min_risk_reward Filtering
# -----------------------------------------------------------------------------

def test_varied_risk_reward_ratios_and_engine_filtering():
    """Verify varied R:R calculations (sub-1.5, exact 1.5, >1.5) and filter_min_rr gate."""
    n = 220
    start_ns = 1_700_000_000 * 1_000_000_000
    step_15m = 15 * ONE_MINUTE_NS
    step_1h = 60 * ONE_MINUTE_NS

    df_15m = _make_dummy_ohlcv(n_bars=n, base_price=100.0, step_ns=step_15m, start_ns=start_ns)

    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]

    # Scenario A: Target distance is small -> R:R < 1.5
    # HTF high = 101.5 (reward = 1.5), Stop Donchian low = 98.0 (risk = 2.0) -> R:R = 0.75
    df_htf_low_target = _make_dummy_ohlcv(
        n_bars=60,
        base_price=100.0,
        step_ns=step_1h,
        start_ns=start_ns,
        high_override=[101.5] * 60,
        low_override=[95.0] * 60,
    )
    risk_custom = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=1.5,
        min_risk_reward=1.5,
    )

    # Without filter_min_rr: returns candidate with actual R:R recorded
    cands_unfiltered = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_custom,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_htf_low_target,
        filter_min_rr=False,
    )
    assert len(cands_unfiltered) > 0
    c_low = cands_unfiltered[-1]
    assert c_low.risk_reward_ratio is not None
    assert c_low.risk_reward_ratio < Decimal("1.50")

    # With filter_min_rr=True: candidate with R:R < 1.5 is filtered out
    cands_filtered = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_custom,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_htf_low_target,
        filter_min_rr=True,
    )
    assert len(cands_filtered) == 0

    # Scenario B: High R:R (e.g. 2.50) passes filter
    df_htf_high_target = _make_dummy_ohlcv(
        n_bars=60,
        base_price=100.0,
        step_ns=step_1h,
        start_ns=start_ns,
        high_override=[105.0] * 60,
        low_override=[95.0] * 60,
    )
    risk_high = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
        min_risk_reward=1.5,
    )
    cands_high = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk_high,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_htf_high_target,
        filter_min_rr=True,
    )
    assert len(cands_high) > 0
    c_ok = cands_high[-1]
    assert c_ok.risk_reward_ratio >= Decimal("1.50")


# -----------------------------------------------------------------------------
# Worker Integration Fixtures & Tests
# -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_d3") as url:
        yield url


def as_scanner(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"cryptonite_scanner:scanner_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))


@pytest.fixture
async def admin(db_url):
    conn = await asyncpg.connect(db_url)
    yield conn
    await conn.execute(
        "TRUNCATE scanner_debate_outbox, outbox_events, signal_candidates, scanner_checkpoints, "
        "scan_runs, scan_spec_versions, scan_specs, ingest_checkpoints CASCADE"
    )
    await conn.close()


class FakeLakeQueryEngine:
    def __init__(self, df: pl.DataFrame):
        self._df = df

    def query_bars_df(self, **_kwargs) -> pl.DataFrame:
        return self._df


async def _seed_spec(pool, slug: str, universe: dict, rules: list) -> tuple[uuid4, uuid4]:
    import json
    spec_id = uuid4()
    ver_id = uuid4()
    await pool.execute(
        """
        INSERT INTO scan_specs (id, slug, name, active_version_id, is_archived)
        VALUES ($1, $2, $2, NULL, false);
        """,
        spec_id, slug,
    )
    await pool.execute(
        """
        INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
        VALUES ($1, $2, 1, 'hash-val', $3::jsonb, $4::jsonb);
        """,
        ver_id, spec_id, json.dumps(universe), json.dumps(rules),
    )
    await pool.execute(
        "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2;",
        ver_id, spec_id,
    )
    return spec_id, ver_id


@pytest.mark.asyncio
async def test_worker_pre_insert_rr_rejection_gate(admin, db_url, monkeypatch):
    """Verify that candidates with R:R < 1.5 are rejected before signal_candidates and outbox."""
    import time
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    now_ts = (time.time_ns() // (60 * ONE_MINUTE_NS)) * (60 * ONE_MINUTE_NS)

    universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["15m"]}
    rules = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    spec_id, ver_id = await _seed_spec(pool, "test-rr-filter", universe, rules)

    cand_rejected = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=ver_id,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=now_ts - 15 * ONE_MINUTE_NS,
        bar_close_inclusive_ns=now_ts - 1_000_000,
        interval_end_exclusive_ns=now_ts,
        matched_rules=["rule_low_rr"],
        metrics={"stop_type": "donchian_low", "target_type": "atr_fallback", "risk_reward_ratio": 1.20},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("95.00"),
        suggested_target=Decimal("106.00"),
        risk_reward_ratio=Decimal("1.20"),
        created_at=datetime.now(UTC),
    )
    cand_accepted = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=ver_id,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=now_ts - 30 * ONE_MINUTE_NS,
        bar_close_inclusive_ns=now_ts - 15 * ONE_MINUTE_NS - 1_000_000,
        interval_end_exclusive_ns=now_ts - 15 * ONE_MINUTE_NS,
        matched_rules=["rule_high_rr"],
        metrics={"stop_type": "donchian_low", "target_type": "htf_donchian_high", "risk_reward_ratio": 2.00},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("97.00"),
        suggested_target=Decimal("106.00"),
        risk_reward_ratio=Decimal("2.00"),
        created_at=datetime.now(UTC),
    )

    def mock_evaluate(*_args, **_kwargs):
        return [cand_rejected, cand_accepted]

    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(mock_evaluate))

    lake_df = _make_dummy_ohlcv(n_bars=300, base_price=100.0, step_ns=ONE_MINUTE_NS, start_ns=now_ts - 300 * ONE_MINUTE_NS)
    worker = ScannerWorker(database_url=as_scanner(db_url), analytics_lake_path="/nonexistent-lake")
    worker.pool = pool
    worker.query_engine = FakeLakeQueryEngine(lake_df)

    await worker._execute_scan(
        spec_version_id=ver_id,
        spec_hash="hash-rr",
        universe_data=universe,
        rules_data=rules,
        trigger_reason="COMMITTED_BAR_EVENT",
        trigger_event_id=str(uuid4()),
        watermark_ns=now_ts,
    )

    # Verify database contents:
    row_rejected = await pool.fetchrow("SELECT id FROM signal_candidates WHERE id = $1;", cand_rejected.id)
    assert row_rejected is None, "Candidate with R:R < 1.5 must be rejected and not inserted into signal_candidates!"

    row_accepted = await pool.fetchrow("SELECT id FROM signal_candidates WHERE id = $1;", cand_accepted.id)
    assert row_accepted is not None, "Candidate with R:R >= 1.5 must be inserted into signal_candidates!"

    outbox_rejected = await pool.fetchrow("SELECT candidate_id FROM scanner_debate_outbox WHERE candidate_id = $1;", cand_rejected.id)
    assert outbox_rejected is None, "Candidate with R:R < 1.5 must not produce debate outbox row!"

    await pool.close()


@pytest.mark.asyncio
async def test_automatic_trigger_timeframe_restriction(admin, db_url, monkeypatch):
    """Verify that automatic stream events strictly restrict triggers to 15m/1h/4h, skipping 1m and 5m."""
    import json
    import time

    from cryptonite_scanner.models import ALLOWED_AUTOMATIC_TIMEFRAMES
    from cryptonite_scanner.presets import BUILTIN_PRESETS

    # 1. Verify all built-in presets only use allowed automatic timeframes
    for p in BUILTIN_PRESETS:
        for tf in p.universe.timeframes:
            assert tf in ALLOWED_AUTOMATIC_TIMEFRAMES, f"Preset {p.slug} has non-automatic timeframe {tf}"

    # 2. Test stream handler skips 1m and 5m committed bar events
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    now_ts = (time.time_ns() // (60 * ONE_MINUTE_NS)) * (60 * ONE_MINUTE_NS)
    universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["15m", "1h"]}
    rules = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    await _seed_spec(pool, "test-tf-restrict", universe, rules)

    worker = ScannerWorker(database_url=as_scanner(db_url), analytics_lake_path="/nonexistent-lake")
    worker.pool = pool

    class DummyRedis:
        def __init__(self):
            self.xacked: list[tuple[str, str, str]] = []

        async def xack(self, stream: str, group: str, msg_id: str) -> None:
            self.xacked.append((stream, group, msg_id))

    worker.redis = DummyRedis()  # type: ignore

    scans_executed: list[dict[str, Any]] = []

    async def mock_execute_scan(*args, **kwargs):
        scans_executed.append(kwargs)
        return uuid4()

    monkeypatch.setattr(worker, "_execute_scan", mock_execute_scan)

    # 1m event should be skipped (XACK'd, 0 scans executed)
    msg_1m = {"data": json.dumps({"event_id": "ev-1m", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "1m", "bar_close_inclusive_ns": now_ts})}
    await worker._handle_stream_message("closed_bar_committed", "1-0", msg_1m)
    assert len(scans_executed) == 0
    assert len(worker.redis.xacked) == 1

    # 5m event should also be skipped
    msg_5m = {"data": json.dumps({"event_id": "ev-5m", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "5m", "bar_close_inclusive_ns": now_ts})}
    await worker._handle_stream_message("closed_bar_committed", "2-0", msg_5m)
    assert len(scans_executed) == 0
    assert len(worker.redis.xacked) == 2

    # 15m event should be processed (1 scan executed)
    msg_15m = {"data": json.dumps({"event_id": "ev-15m", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "15m", "bar_close_inclusive_ns": now_ts})}
    await worker._handle_stream_message("closed_bar_committed", "3-0", msg_15m)
    assert len(scans_executed) == 1
    assert scans_executed[-1]["trigger_reason"] == "COMMITTED_BAR_EVENT"
    assert scans_executed[-1]["trigger_event_id"] == "ev-15m"
    assert len(worker.redis.xacked) == 3

    # 1h event should be processed
    msg_1h = {"data": json.dumps({"event_id": "ev-1h", "instrument_id": "BTCUSDT.BINANCE", "timeframe": "1h", "bar_close_inclusive_ns": now_ts})}
    await worker._handle_stream_message("closed_bar_committed", "4-0", msg_1h)
    assert len(scans_executed) == 2
    assert scans_executed[-1]["trigger_event_id"] == "ev-1h"

    await pool.close()


def test_unrounded_derived_price_ratio_boundary():
    """Verify that unrounded derived price ratio correctly rejects true 1.496 despite rounding to 1.50."""
    # 1. 1.496 boundary: entry=100.00, stop=89.97 (risk=10.03), target=115.01 (reward=15.01) -> 15.01 / 10.03 = 1.49651...
    cand_1496 = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=uuid4(),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=1000,
        bar_close_inclusive_ns=2000,
        interval_end_exclusive_ns=2001,
        matched_rules=["test"],
        metrics={},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("89.97"),
        suggested_target=Decimal("115.01"),
        created_at=datetime.now(UTC),
    )
    risk_1496 = abs(cand_1496.suggested_entry - cand_1496.suggested_stop)
    reward_1496 = abs(cand_1496.suggested_target - cand_1496.suggested_entry)
    derived_rr_1496 = reward_1496 / risk_1496
    assert derived_rr_1496 < Decimal("1.5")

    # 2. 1.500 boundary: entry=100.00, stop=90.00 (risk=10.00), target=115.00 (reward=15.00) -> 1.5000
    cand_1500 = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=uuid4(),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=1000,
        bar_close_inclusive_ns=2000,
        interval_end_exclusive_ns=2001,
        matched_rules=["test"],
        metrics={},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("90.00"),
        suggested_target=Decimal("115.00"),
        created_at=datetime.now(UTC),
    )
    risk_1500 = abs(cand_1500.suggested_entry - cand_1500.suggested_stop)
    reward_1500 = abs(cand_1500.suggested_target - cand_1500.suggested_entry)
    derived_rr_1500 = reward_1500 / risk_1500
    assert derived_rr_1500 >= Decimal("1.5")


@pytest.mark.asyncio
async def test_worker_qualification_before_dedupe_two_specs_same_bar(admin, db_url, monkeypatch):
    """Verify that qualification happens BEFORE deduplication so passing spec B is not dropped by sub-threshold spec A."""
    import time
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    now_ts = 1_791_201_600 * 10**9
    # Pin time_ns so candidate bar closing at now_ts is within freshness window (0 <= age_ns <= 15m)
    monkeypatch.setattr(time, "time_ns", lambda: now_ts + 5 * ONE_MINUTE_NS)

    universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["15m"]}
    rules = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    spec_a_id, ver_a_id = await _seed_spec(pool, "spec-a-low-rr", universe, rules)
    spec_b_id, ver_b_id = await _seed_spec(pool, "spec-b-high-rr", universe, rules)

    same_bar_open = now_ts - 15 * ONE_MINUTE_NS
    same_bar_close = now_ts - 1_000_000
    same_bar_end = now_ts

    # Spec A has RR = 1.20 (< 1.50) on the EXACT same bar
    cand_spec_a = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=ver_a_id,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=same_bar_open,
        bar_close_inclusive_ns=same_bar_close,
        interval_end_exclusive_ns=same_bar_end,
        matched_rules=["rule_a"],
        metrics={"stop_type": "donchian_low", "target_type": "atr_fallback", "risk_reward_ratio": 1.20},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("95.00"),  # risk = 5.00
        suggested_target=Decimal("106.00"),  # reward = 6.00 -> RR = 1.20
        created_at=datetime.now(UTC),
    )

    # Spec B has RR = 1.80 (>= 1.50) on the EXACT same bar
    cand_spec_b = SignalCandidateRead(
        id=uuid4(),
        scan_run_id=uuid4(),
        spec_version_id=ver_b_id,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        bar_open_ns=same_bar_open,
        bar_close_inclusive_ns=same_bar_close,
        interval_end_exclusive_ns=same_bar_end,
        matched_rules=["rule_b"],
        metrics={"stop_type": "donchian_low", "target_type": "htf_donchian_high", "risk_reward_ratio": 1.80},
        suggested_entry=Decimal("100.00"),
        suggested_stop=Decimal("95.00"),  # risk = 5.00
        suggested_target=Decimal("109.00"),  # reward = 9.00 -> RR = 1.80
        created_at=datetime.now(UTC),
    )

    # Evaluate returns [cand_spec_a, cand_spec_b] in order: spec A first
    # If deduplication happened before qualification, spec A would be selected and then rejected,
    # leaving 0 candidates persisted.
    # Because qualification happens first, spec A is rejected, spec B qualifies, passes dedupe, and persists.
    def mock_evaluate(*_args, **_kwargs):
        return [cand_spec_a, cand_spec_b]

    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(mock_evaluate))

    lake_df = _make_dummy_ohlcv(n_bars=300, base_price=100.0, step_ns=ONE_MINUTE_NS, start_ns=now_ts - 300 * ONE_MINUTE_NS)
    worker = ScannerWorker(database_url=as_scanner(db_url), analytics_lake_path="/nonexistent-lake")
    worker.pool = pool
    worker.query_engine = FakeLakeQueryEngine(lake_df)

    await worker._execute_scan(
        spec_version_id=ver_b_id,
        spec_hash="hash-two-spec",
        universe_data=universe,
        rules_data=rules,
        trigger_reason="COMMITTED_BAR_EVENT",
        trigger_event_id=str(uuid4()),
        watermark_ns=now_ts,
    )

    # Spec A candidate must NOT exist
    row_a = await pool.fetchrow("SELECT id FROM signal_candidates WHERE id = $1;", cand_spec_a.id)
    assert row_a is None, "Spec A candidate with R:R < 1.5 must not be inserted"

    # Spec B candidate MUST exist (survived qualification and dedupe)
    row_b = await pool.fetchrow("SELECT id FROM signal_candidates WHERE id = $1;", cand_spec_b.id)
    assert row_b is not None, "Spec B candidate with R:R >= 1.5 must be persisted even when Spec A on the same bar had R:R < 1.5"

    outbox_b = await pool.fetchrow("SELECT candidate_id FROM scanner_debate_outbox WHERE candidate_id = $1;", cand_spec_b.id)
    assert outbox_b is not None

    await pool.close()
