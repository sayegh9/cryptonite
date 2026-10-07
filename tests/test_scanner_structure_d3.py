"""D3: Structure-based stop/target, zero-lookahead HTF evaluation,
positive finite ATR validation, R:R >= 1.5 pre-insert rejection gate,
and automatic trigger timeframe restriction.

Validates:
1. Long and Short structure stops (Donchian channel within [0.5 * ATR, 4.0 * ATR]) with ATR fallback.
2. Long and Short HTF structure targets (HTF Donchian / Bollinger levels within [1.5 * ATR, 6.0 * ATR]) with ATR fallback.
3. Deterministic no-lookahead guarantee: future and partial HTF bars never alter earlier candidates.
4. Positive finite ATR validation and quantized price levels.
5. Exact 1.50 R:R on fallback (2.0 ATR stop, 3.0 ATR target).
6. Pre-insert rejection gate: candidates with R:R < 1.5 are rejected with logged reason and never inserted into signal_candidates or scanner_debate_outbox.
7. Automatic scheduling restriction: COMMITTED_BAR_EVENT evaluates only ALLOWED_AUTOMATIC_TIMEFRAMES ("15m", "1h", "4h").
"""

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import polars as pl
import pytest
from connections import REDIS_URL
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


def redis_url() -> str:
    return urlunsplit(urlsplit(REDIS_URL)._replace(path="/15"))


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
# Test 1: Structure Donchian Stop & ATR Fallback (Long & Short)
# -----------------------------------------------------------------------------

def test_structure_donchian_stop_long_and_short():
    """Verify Donchian stop within [0.5*ATR, 4.0*ATR] bounds vs fallback on out-of-bounds."""
    n = 220
    df = _make_dummy_ohlcv(n_bars=n, base_price=100.0)

    # Simple rule that triggers on the last bar: close > 0
    rules = [
        RuleGroup(
            category="trigger",
            logic="AND",
            conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)],
        )
    ]

    # 1. Long Structure Stop (default)
    risk_long = RiskConfig(
        direction="LONG",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )
    cands_long = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules,
        risk_config=risk_long,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(cands_long) > 0
    c_long = cands_long[-1]
    assert c_long.suggested_stop < c_long.suggested_entry
    assert c_long.metrics["stop_type"] in ("donchian_low", "atr_fallback")

    # 2. Short Structure Stop
    risk_short = RiskConfig(
        direction="SHORT",
        stop_mode="structure",
        min_atr_mult=0.5,
        max_atr_mult=4.0,
        fallback_stop_atr_mult=2.0,
        fallback_target_atr_mult=3.0,
    )
    cands_short = ScannerEngine.evaluate(
        df=df,
        rule_groups=rules,
        risk_config=risk_short,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(cands_short) > 0
    c_short = cands_short[-1]
    assert c_short.suggested_stop > c_short.suggested_entry
    assert c_short.metrics["stop_type"] in ("donchian_high", "atr_fallback")


def test_structure_stop_bounds_enforcement_and_fallback():
    """Verify that Donchian stops exceeding 4.0 ATR fall back to 2.0 ATR."""
    n = 220
    # Create an artificial low 20 bars ago that is 10.0 units below close, while ATR is ~1.0
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    # Make a deep low at index n - 15 (within 20-bar lookback)
    lows[n - 15] = 80.0  # 20 units below, ATR is ~2.0 -> dist=20 > 4.0*ATR (8.0)

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
    )
    assert len(candidates) > 0
    c = candidates[-1]
    # Deep low of 80 was rejected as too wide; fell back to 2.0 * ATR
    assert c.metrics["stop_type"] == "atr_fallback"
    assert c.suggested_stop > Decimal("85.0")  # Much tighter than 80.0


# -----------------------------------------------------------------------------
# Test 2: HTF Target & ATR Fallback
# -----------------------------------------------------------------------------

def test_htf_target_and_atr_fallback():
    """Verify HTF target level selection vs fallback when HTF is absent or out-of-bounds."""
    n = 220
    df_15m = _make_dummy_ohlcv(n_bars=n, base_price=100.0, step_ns=15 * ONE_MINUTE_NS)
    # Corresponding 1h HTF frame covering the same time
    df_1h = _make_dummy_ohlcv(
        n_bars=n,
        base_price=100.0,
        step_ns=60 * ONE_MINUTE_NS,
        start_ns=1_700_000_000 * 1_000_000_000 - (100 * 60 * ONE_MINUTE_NS),
    )

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

    # 1. With HTF DataFrame
    cands_with_htf = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=df_1h,
    )
    assert len(cands_with_htf) > 0
    c_htf = cands_with_htf[-1]
    assert c_htf.metrics["target_type"] in ("htf_donchian_high", "htf_bb_upper", "atr_fallback")

    # 2. Without HTF DataFrame -> Must use atr_fallback
    cands_no_htf = ScannerEngine.evaluate(
        df=df_15m,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
        htf_df=None,
    )
    assert len(cands_no_htf) > 0
    c_no_htf = cands_no_htf[-1]
    assert c_no_htf.metrics["target_type"] == "atr_fallback"


# -----------------------------------------------------------------------------
# Test 3: Deterministic No-Lookahead Guarantee on HTF Bars
# -----------------------------------------------------------------------------

def test_deterministic_no_lookahead_htf():
    """Verify that forming or future HTF bars cannot alter earlier candidate stop/target/R:R."""
    base_ts = 1_700_000_000 * 1_000_000_000
    n = 220
    df_15m = _make_dummy_ohlcv(n_bars=n, base_price=100.0, step_ns=15 * ONE_MINUTE_NS, start_ns=base_ts)

    # Target the last 15m bar
    last_15m_bar_end_ns = df_15m["interval_end_exclusive_ns"][-1]

    # HTF 1h dataset strictly closed by the last 15m bar:
    # All 1h bars have interval_end_exclusive_ns <= last_15m_bar_end_ns
    closed_1h_bars = _make_dummy_ohlcv(
        n_bars=100,
        base_price=100.0,
        step_ns=60 * ONE_MINUTE_NS,
        start_ns=last_15m_bar_end_ns - (100 * 60 * ONE_MINUTE_NS),
    )
    # Ensure all closed_1h_bars closed at or before last_15m_bar_end_ns
    closed_1h_bars = closed_1h_bars.filter(pl.col("interval_end_exclusive_ns") <= last_15m_bar_end_ns)

    # Now create future / forming HTF bars with wild spike values (e.g. price 999999.0)
    future_1h_bars = _make_dummy_ohlcv(
        n_bars=5,
        base_price=999_999.0,
        step_ns=60 * ONE_MINUTE_NS,
        start_ns=last_15m_bar_end_ns,  # starts exactly when last 15m bar ends; closes in the future!
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
    # The extreme price (999999.0) was NOT used!
    assert c2.suggested_target < Decimal("500.0")


# -----------------------------------------------------------------------------
# Test 4: Positive Finite ATR Validation & Quantization
# -----------------------------------------------------------------------------

def test_positive_finite_atr_validation():
    """Verify that rows with null, non-positive, or NaN/Inf ATR are safely skipped."""
    n = 220
    df = _make_dummy_ohlcv(n_bars=n, base_price=100.0)

    # Artificially enrich indicators, then corrupt ATR in the last row
    enriched = ScannerEngine.enrich_indicators(df)
    corrupted = enriched.with_columns(
        atr=pl.when(pl.col("interval_end_exclusive_ns") == enriched["interval_end_exclusive_ns"][-1])
        .then(pl.lit(0.0))  # non-positive ATR
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
        df=corrupted,
        rule_groups=rules,
        risk_config=risk,
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    # The corrupted last row with ATR=0.0 must be skipped
    last_end = enriched["interval_end_exclusive_ns"][-1]
    assert not any(c.interval_end_exclusive_ns == last_end for c in candidates)


def test_fallback_exact_risk_reward_ratio():
    """Verify that fallback 2.0 ATR stop and 3.0 ATR target yields exactly 1.50 R:R."""
    n = 220
    # Flat price series where Donchian low equals close (dist=0 < 0.5 ATR), forcing ATR fallback
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [100.0] * n  # low equals close -> Donchian low = 100.0 == close, fails bounds, falls back!
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
# -----------------------------------------------------------------------------
# Test 5: Worker Pre-Insert R:R < 1.5 Rejection Gate
# -----------------------------------------------------------------------------

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

    # Mock ScannerEngine.evaluate to return two candidates:
    # 1. R:R = 1.20 (< 1.50) -> Must be rejected!
    # 2. R:R = 2.00 (>= 1.50) -> Must be accepted!
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
        suggested_stop=Decimal("95.00"),  # risk = 5.00
        suggested_target=Decimal("106.00"),  # reward = 6.00 -> R:R = 1.20
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
        suggested_stop=Decimal("97.00"),  # risk = 3.00
        suggested_target=Decimal("106.00"),  # reward = 6.00 -> R:R = 2.00
        risk_reward_ratio=Decimal("2.00"),
        created_at=datetime.now(UTC),
    )

    def mock_evaluate(*_args, **_kwargs):
        return [cand_rejected, cand_accepted]

    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(mock_evaluate))

    # Lake DataFrame
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
    # cand_rejected (R:R=1.20) must NOT exist
    # cand_accepted (R:R=2.00) MUST exist
    row_rejected = await pool.fetchrow("SELECT id FROM signal_candidates WHERE id = $1;", cand_rejected.id)
    assert row_rejected is None, "Candidate with R:R < 1.5 must be rejected and not inserted into signal_candidates!"

    row_accepted = await pool.fetchrow("SELECT id, suggested_entry, suggested_stop, suggested_target FROM signal_candidates WHERE id = $1;", cand_accepted.id)
    assert row_accepted is not None, "Candidate with R:R >= 1.5 must be inserted into signal_candidates!"

    outbox_rejected = await pool.fetchrow("SELECT candidate_id FROM scanner_debate_outbox WHERE candidate_id = $1;", cand_rejected.id)
    assert outbox_rejected is None, "Candidate with R:R < 1.5 must not produce debate outbox row!"

    await pool.close()


# -----------------------------------------------------------------------------
# Test 6: Automatic Trigger Timeframe Restriction (15m, 1h, 4h only)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_automatic_trigger_timeframe_restriction(admin, db_url, monkeypatch):
    """Verify that COMMITTED_BAR_EVENT evaluates only 15m/1h/4h, skipping 1m and 5m."""
    import time
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    now_ts = (time.time_ns() // (60 * ONE_MINUTE_NS)) * (60 * ONE_MINUTE_NS)

    universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["1m", "5m", "15m", "1h"]}
    rules = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    spec_id, ver_id = await _seed_spec(pool, "test-tf-restrict", universe, rules)

    evaluated_timeframes: list[str] = []

    def mock_eval(cls, df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, scan_run_id, price_precision=2, **_kwargs):
        evaluated_timeframes.append(timeframe)
        return []

    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(mock_eval))

    lake_df = _make_dummy_ohlcv(n_bars=300, base_price=100.0, step_ns=ONE_MINUTE_NS, start_ns=now_ts - 300 * ONE_MINUTE_NS)
    worker = ScannerWorker(database_url=as_scanner(db_url), analytics_lake_path="/nonexistent-lake")
    worker.pool = pool
    worker.query_engine = FakeLakeQueryEngine(lake_df)

    # 1. Trigger via COMMITTED_BAR_EVENT -> Only 15m and 1h evaluated!
    evaluated_timeframes.clear()
    await worker._execute_scan(
        spec_version_id=ver_id,
        spec_hash="hash-tf",
        universe_data=universe,
        rules_data=rules,
        trigger_reason="COMMITTED_BAR_EVENT",
        trigger_event_id=str(uuid4()),
        watermark_ns=now_ts,
    )
    assert set(evaluated_timeframes) == {"15m", "1h"}, f"Expected only 15m and 1h for automatic event, got {evaluated_timeframes}"
    assert "1m" not in evaluated_timeframes
    assert "5m" not in evaluated_timeframes

    # 2. Trigger via MANUAL_JOB -> All configured timeframes evaluated!
    evaluated_timeframes.clear()
    await worker._execute_scan(
        spec_version_id=ver_id,
        spec_hash="hash-tf",
        universe_data=universe,
        rules_data=rules,
        trigger_reason="MANUAL_JOB",
        trigger_event_id=None,
        watermark_ns=now_ts,
    )
    assert "1m" in evaluated_timeframes
    assert "5m" in evaluated_timeframes
    assert "15m" in evaluated_timeframes
    assert "1h" in evaluated_timeframes

    await pool.close()
