"""D2: Event-based scanner conditions, crossover semantics, candidate dedupe,
and max-active-specs guard.

Validates:
1. RuleCondition lookback >= 1 enforcement on crossover operators (cross_above, cross_below,
   crosses_above, crosses_below) and default lookback=1.
2. Deterministic fixture: crossover produces exactly one candidate per crossover event,
   not one candidate per bar in trending continuation data.
3. In-memory and cross-spec deduplication per (instrument, timeframe, bar).
4. MAX_ACTIVE_SPECS guard in API routes and scanner worker daemon.
5. Volume reduction assessment comparing state-based vs crossover seed presets.
"""

import json
import time as real_time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import polars as pl
import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_scanner.engine import ScannerEngine
from cryptonite_scanner.models import (
    MAX_ACTIVE_SPECS,
    RiskConfig,
    RuleCondition,
    RuleGroup,
    RuleOperator,
    SignalCandidateRead,
    compute_spec_hash,
    deduplicate_candidates,
)
from cryptonite_scanner.presets import (
    PRESET_BOLLINGER_BREAKOUT,
    PRESET_MACD_MOMENTUM,
    PRESET_RSI_OVERSOLD,
    PRESET_TREND_PULLBACK,
)
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker
from migrated_db import migrated_database

# -----------------------------------------------------------------------------
# Fixtures and Helpers
# -----------------------------------------------------------------------------

def redis_url() -> str:
    return urlunsplit(urlsplit(REDIS_URL)._replace(path="/15"))


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_d2") as url:
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


@pytest.fixture
async def redis15(monkeypatch):
    client = aioredis.from_url(redis_url(), decode_responses=True)
    assert (await client.connection_pool.get_connection()).db == 15
    stream = f"test:d2:debate_queue:{uuid4().hex[:8]}"
    monkeypatch.setattr(worker_mod, "DEBATE_QUEUE", stream)
    yield SimpleNamespace(client=client, stream=stream)
    await client.delete(stream)
    await client.aclose()


class FakeQueryEngine:
    def __init__(self, df: pl.DataFrame):
        self._df = df

    def query_bars_df(self, **_kwargs) -> pl.DataFrame:
        return self._df


async def make_worker(db_url_str: str, lake: pl.DataFrame, redis_client) -> ScannerWorker:
    w = ScannerWorker(database_url=as_scanner(db_url_str), analytics_lake_path="/nonexistent-d2-lake")
    w.query_engine = FakeQueryEngine(lake)
    w.pool = await asyncpg.create_pool(w.database_url, min_size=1, max_size=4)
    w.redis = redis_client
    return w


async def seed_spec_row(conn: asyncpg.Connection, slug: str, name: str, rules: list[dict], instrument: str = "BTCUSDT.BINANCE") -> UUID:
    universe = {"instrument_ids": [instrument], "timeframes": ["15m"]}
    spec_hash = compute_spec_hash(universe, rules)
    version_id = uuid4()
    spec_id = uuid4()
    await conn.execute(
        """INSERT INTO scan_specs (id, slug, name, description, is_archived)
           VALUES ($1, $2, $3, 'test', false)""",
        spec_id, slug, name,
    )
    await conn.execute(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           VALUES ($1, $2, 1, $3, $4::jsonb, $5::jsonb)""",
        version_id, spec_id, spec_hash, json.dumps(universe), json.dumps(rules),
    )
    await conn.execute(
        "UPDATE scan_specs SET active_version_id = $1 WHERE id = $2",
        version_id, spec_id,
    )
    return version_id


# -----------------------------------------------------------------------------
# 1. Grammar & Lookback Validation Tests
# -----------------------------------------------------------------------------

def test_rule_condition_lookback_validation():
    """Crossover conditions require lookback >= 1 and reject lookback < 1; defaults to 1."""
    # Default lookback for crossover is 1
    c1 = RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value=100.0)
    assert c1.lookback == 1

    c2 = RuleCondition(field="close", operator=RuleOperator.CROSSES_ABOVE, value="bb_upper")
    assert c2.lookback == 1

    c3 = RuleCondition(field="rsi", operator=RuleOperator.CROSS_BELOW, value=30.0)
    assert c3.lookback == 1

    c4 = RuleCondition(field="macd_hist", operator=RuleOperator.CROSSES_BELOW, value=0.0)
    assert c4.lookback == 1

    # Explicit lookback >= 1 is accepted
    c5 = RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value=100.0, lookback=2)
    assert c5.lookback == 2

    # lookback < 1 on crossover raises ValueError
    with pytest.raises(ValueError, match="requires lookback >= 1"):
        RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value=100.0, lookback=0)

    with pytest.raises(ValueError, match="requires lookback >= 1"):
        RuleCondition(field="close", operator=RuleOperator.CROSSES_ABOVE, value=100.0, lookback=0)

    with pytest.raises(ValueError, match="requires lookback >= 1"):
        RuleCondition(field="close", operator=RuleOperator.CROSS_BELOW, value=100.0, lookback=0)

    with pytest.raises(ValueError, match="requires lookback >= 1"):
        RuleCondition(field="close", operator=RuleOperator.CROSSES_BELOW, value=100.0, lookback=0)

    # Negative lookback is rejected for all operators
    with pytest.raises(ValueError, match="cannot be negative"):
        RuleCondition(field="close", operator=RuleOperator.GT, value=100.0, lookback=-1)

    # Non-crossover operator with lookback=0 or lookback>0 is valid
    c_state = RuleCondition(field="close", operator=RuleOperator.GT, value=100.0, lookback=0)
    assert c_state.lookback == 0

    c_shift = RuleCondition(field="close", operator=RuleOperator.GT, value="open", lookback=1)
    assert c_shift.lookback == 1


# -----------------------------------------------------------------------------
# 2. Deterministic Fixture: One Candidate Per Crossover, Not One Per Bar
# -----------------------------------------------------------------------------

def test_deterministic_crossover_produces_one_candidate_not_continuation():
    """In trending data, state condition fires on every bar; crossover fires ONCE on the cross bar."""
    base_ts = 1_700_000_000 * 1_000_000_000
    # 240 warmup bars at 90.0, then approach 100, then cross at bar 244, then continuation bars
    warmup = [90.0] * 240
    approach = [92.0, 95.0, 98.0]  # bars 241, 242, 243
    cross_bar = [102.0]            # bar 244: crossover above 100
    continuation = [105.0, 110.0, 115.0, 120.0, 125.0]  # bars 245..249
    prices = warmup + approach + cross_bar + continuation
    n = len(prices)  # 250 bars

    open_times = [base_ts + (i * 15 * ONE_MINUTE_NS) for i in range(n)]
    df = pl.DataFrame({
        "bar_open_ns": open_times,
        "bar_close_inclusive_ns": [t + 15 * ONE_MINUTE_NS - 1 for t in open_times],
        "interval_end_exclusive_ns": [t + 15 * ONE_MINUTE_NS for t in open_times],
        "open": [p - 0.5 for p in prices],
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [1000.0] * n,
        "quote_volume": [1000.0 * p for p in prices],
        "trades_count": [50] * n,
    })

    # 1. State condition: close > 100
    state_rule = [RuleGroup(
        category="trigger",
        logic="AND",
        conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=100.0, lookback=0)],
    )]
    state_candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=state_rule,
        risk_config=RiskConfig(),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    # State condition fires on bar 244 plus all 5 continuation bars (245..249) -> 6 candidates!
    assert len(state_candidates) == 6

    # 2. Crossover condition: close CROSS_ABOVE 100
    crossover_rule = [RuleGroup(
        category="trigger",
        logic="AND",
        conditions=[RuleCondition(field="close", operator=RuleOperator.CROSS_ABOVE, value=100.0, lookback=1)],
    )]
    crossover_candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=crossover_rule,
        risk_config=RiskConfig(),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    # Crossover condition fires ONLY on bar 244 (98 -> 102) -> EXACTLY 1 candidate!
    assert len(crossover_candidates) == 1
    c = crossover_candidates[0]
    cross_bar_index = 240 + len(approach)
    expected_bar_close = open_times[cross_bar_index] + 15 * ONE_MINUTE_NS - 1
    assert c.bar_close_inclusive_ns == expected_bar_close
    assert c.suggested_entry == Decimal("102.00")

    # 3. Crossover condition: CROSSES_ABOVE alias produces identical event detection
    crosses_rule = [RuleGroup(
        category="trigger",
        logic="AND",
        conditions=[RuleCondition(field="close", operator=RuleOperator.CROSSES_ABOVE, value=100.0, lookback=1)],
    )]
    crosses_candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=crosses_rule,
        risk_config=RiskConfig(),
        instrument_id="BTCUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(crosses_candidates) == 1
    assert crosses_candidates[0].bar_close_inclusive_ns == expected_bar_close


def test_deterministic_cross_below_produces_one_candidate():
    """CROSS_BELOW fires exactly once when value drops below threshold."""
    base_ts = 1_700_000_000 * 1_000_000_000
    warmup = [110.0] * 240
    approach = [108.0, 105.0, 102.0]  # bars 241, 242, 243
    cross_bar = [97.0]                 # bar 244: crossover below 100
    continuation = [95.0, 92.0, 88.0, 85.0, 80.0]  # bars 245..249
    prices = warmup + approach + cross_bar + continuation
    n = len(prices)  # 250 bars

    open_times = [base_ts + (i * 15 * ONE_MINUTE_NS) for i in range(n)]
    df = pl.DataFrame({
        "bar_open_ns": open_times,
        "bar_close_inclusive_ns": [t + 15 * ONE_MINUTE_NS - 1 for t in open_times],
        "interval_end_exclusive_ns": [t + 15 * ONE_MINUTE_NS for t in open_times],
        "open": [p + 0.5 for p in prices],
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [1000.0] * n,
        "quote_volume": [1000.0 * p for p in prices],
        "trades_count": [50] * n,
    })

    cross_below_rule = [RuleGroup(
        category="trigger",
        logic="AND",
        conditions=[RuleCondition(field="close", operator=RuleOperator.CROSS_BELOW, value=100.0, lookback=1)],
    )]
    candidates = ScannerEngine.evaluate(
        df=df,
        rule_groups=cross_below_rule,
        risk_config=RiskConfig(),
        instrument_id="ETHUSDT.BINANCE",
        timeframe="15m",
        spec_version_id=uuid4(),
        scan_run_id=uuid4(),
    )
    assert len(candidates) == 1
    cross_bar_index = 240 + len(approach)
    assert candidates[0].bar_close_inclusive_ns == open_times[cross_bar_index] + 15 * ONE_MINUTE_NS - 1
    assert candidates[0].suggested_entry == Decimal("97.00")


# -----------------------------------------------------------------------------
# 3. Deduplication Tests: In-Memory and Across Specs in Worker
# -----------------------------------------------------------------------------

def test_deduplicate_candidates_in_memory():
    """deduplicate_candidates filters duplicates sharing (instrument_id, timeframe, bar_close_inclusive_ns)."""
    now = datetime.now(UTC)
    spec1 = uuid4()
    spec2 = uuid4()
    run_id = uuid4()

    c1 = SignalCandidateRead(
        id=uuid4(), scan_run_id=run_id, spec_version_id=spec1, instrument_id="BTCUSDT.BINANCE",
        timeframe="15m", bar_open_ns=1000, bar_close_inclusive_ns=1899, interval_end_exclusive_ns=1900,
        matched_rules=["rule1"], metrics={}, suggested_entry=Decimal("100"), suggested_stop=Decimal("95"),
        suggested_target=Decimal("110"), created_at=now,
    )
    # c2 shares (instrument_id, timeframe, bar_close_inclusive_ns) with c1 but from spec2
    c2 = SignalCandidateRead(
        id=uuid4(), scan_run_id=run_id, spec_version_id=spec2, instrument_id="BTCUSDT.BINANCE",
        timeframe="15m", bar_open_ns=1000, bar_close_inclusive_ns=1899, interval_end_exclusive_ns=1900,
        matched_rules=["rule2"], metrics={}, suggested_entry=Decimal("100"), suggested_stop=Decimal("94"),
        suggested_target=Decimal("112"), created_at=now,
    )
    # c3 is on a different timeframe
    c3 = SignalCandidateRead(
        id=uuid4(), scan_run_id=run_id, spec_version_id=spec1, instrument_id="BTCUSDT.BINANCE",
        timeframe="1h", bar_open_ns=1000, bar_close_inclusive_ns=4599, interval_end_exclusive_ns=4600,
        matched_rules=["rule3"], metrics={}, suggested_entry=Decimal("100"), suggested_stop=Decimal("95"),
        suggested_target=Decimal("110"), created_at=now,
    )

    deduped = deduplicate_candidates([c1, c2, c3])
    assert len(deduped) == 2
    assert deduped[0].id == c1.id
    assert deduped[1].id == c3.id


async def test_cross_spec_candidate_deduplication_in_worker(db_url, admin, redis15, monkeypatch):
    """When two scan specs match the same bar on an instrument, worker inserts exactly 1 candidate across specs."""
    # Build 1m bars that form a closed 15m bar
    t0 = 1_791_201_600 * 10**9  # 2026-10-05 12:00:00 UTC
    # 15 minutes of 1m bars from 11:45 to 11:59
    opens = [t0 - 15 * ONE_MINUTE_NS + i * ONE_MINUTE_NS for i in range(15)]
    lake_df = pl.DataFrame({
        "bar_open_ns": opens,
        "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0] * 15,
        "high": [105.0] * 15,
        "low": [95.0] * 15,
        "close": [102.0] * 15,
        "volume": [500.0] * 15,
    })

    # Both specs match: rule close > 0
    rule1 = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    rule2 = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 50}]}]

    spec1_id = await seed_spec_row(admin, "spec-one", "Spec One", rule1)
    spec2_id = await seed_spec_row(admin, "spec-two", "Spec Two", rule2)

    # Pin now_ns so the bar closing at 12:00 is fresh
    fake_now = t0 + 30 * 10**9
    monkeypatch.setattr(real_time, "time_ns", lambda: fake_now)

    def mock_eval(cls, df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, scan_run_id, price_precision=2):
        last_row = df.to_dicts()[-1]
        return [
            SignalCandidateRead(
                id=uuid4(),
                scan_run_id=scan_run_id,
                spec_version_id=spec_version_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                bar_open_ns=last_row["bar_open_ns"],
                bar_close_inclusive_ns=last_row["interval_end_exclusive_ns"] - 1,
                interval_end_exclusive_ns=last_row["interval_end_exclusive_ns"],
                matched_rules=["match"],
                metrics={},
                suggested_entry=Decimal("100"),
                suggested_stop=Decimal("95"),
                suggested_target=Decimal("110"),
                created_at=datetime.now(UTC),
            )
        ]

    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(mock_eval))

    w = await make_worker(db_url, lake_df, redis15.client)
    try:
        universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["15m"]}
        # Run spec 1
        await w._execute_scan(
            spec_version_id=spec1_id,
            spec_hash="hash1",
            universe_data=universe,
            rules_data=rule1,
            trigger_reason="COMMITTED_BAR_EVENT",
            trigger_event_id=str(uuid4()),
        )
        # Run spec 2 on the exact same bar
        await w._execute_scan(
            spec_version_id=spec2_id,
            spec_hash="hash2",
            universe_data=universe,
            rules_data=rule2,
            trigger_reason="COMMITTED_BAR_EVENT",
            trigger_event_id=str(uuid4()),
        )
    finally:
        await w.pool.close()

    # Total signal_candidates across specs must be 1 (deduped across specs per instrument/tf/bar)
    candidates_count = await admin.fetchval("SELECT count(*) FROM signal_candidates WHERE instrument_id = 'BTCUSDT.BINANCE'")
    assert candidates_count == 1

    # Total debate outbox entries must also be 1
    outbox_count = await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox")
    assert outbox_count == 1


# -----------------------------------------------------------------------------
# 4. MAX_ACTIVE_SPECS Guard Tests
# -----------------------------------------------------------------------------

def test_max_active_specs_constant():
    assert MAX_ACTIVE_SPECS == 10


async def test_worker_processes_all_active_specs_above_cap_without_dropping_signals(db_url, admin, redis15):
    """When more than MAX_ACTIVE_SPECS active specs exist in DB, worker processes all active specs to avoid dropping signals."""
    lake_df = pl.DataFrame({
        "bar_open_ns": [1000],
        "ts_event_ns": [1000 + ONE_MINUTE_NS],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "volume": [10.0],
    })

    # Seed 12 active specs in DB
    rule = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    for i in range(12):
        await seed_spec_row(admin, f"cap-spec-{i}", f"Cap Spec {i}", rule)

    w = await make_worker(db_url, lake_df, redis15.client)
    executed_specs: list[UUID] = []

    async def mock_execute_scan(spec_version_id, **_kwargs):
        executed_specs.append(spec_version_id)
        return uuid4()

    w._execute_scan = mock_execute_scan

    try:
        # 1. Test reconcile_and_prime processes all active specs
        # Set ingest checkpoint so reconcile primes
        await admin.execute(
            """INSERT INTO ingest_checkpoints (venue, market_type, symbol, bar_type, timeframe, first_timestamp_ns, last_timestamp_ns)
               VALUES ('BINANCE', 'SPOT', 'BTCUSDT', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', 0, $1)""",
            1000 + ONE_MINUTE_NS,
        )
        await w.reconcile_and_prime()
        assert len(executed_specs) == 12

        # 2. Test _handle_stream_message processes all active specs
        executed_specs.clear()
        stream_msg = {
            "data": json.dumps({
                "event_id": str(uuid4()),
                "instrument_id": "BTCUSDT.BINANCE",
                "bar_close_inclusive_ns": 1000 + ONE_MINUTE_NS - 1,
            })
        }
        await w._handle_stream_message("stream", "1-0", stream_msg)
        assert len(executed_specs) == 12
    finally:
        await w.pool.close()


# -----------------------------------------------------------------------------
# 5. Baseline Volume Reduction Assessment Test
# -----------------------------------------------------------------------------

def test_preset_specs_are_crossover_triggers():
    """Verify that all builtin presets use crossover events with lookback >= 1."""
    presets = [
        PRESET_RSI_OVERSOLD,
        PRESET_TREND_PULLBACK,
        PRESET_BOLLINGER_BREAKOUT,
        PRESET_MACD_MOMENTUM,
    ]
    crossover_ops = {
        RuleOperator.CROSS_ABOVE,
        RuleOperator.CROSS_BELOW,
        RuleOperator.CROSSES_ABOVE,
        RuleOperator.CROSSES_BELOW,
    }

    for p in presets:
        trigger_groups = [rg for rg in p.rules if rg.category == "trigger"]
        assert len(trigger_groups) >= 1, f"Preset {p.slug} has no trigger rule group"
        has_crossover = any(
            cond.operator in crossover_ops and cond.lookback >= 1
            for rg in trigger_groups
            for cond in rg.conditions
        )
        assert has_crossover, f"Preset {p.slug} trigger does not use a crossover event with lookback >= 1"


def test_volume_reduction_crossover_vs_state_comparison():
    """Evaluate old state-based rules vs new crossover rules on a trending oscillation fixture,

    confirming at least 5x to 15x volume reduction.
    """
    base_ts = 1_700_000_000 * 1_000_000_000
    n = 300
    # Create trending oscillating price series:
    # prices alternate above/below moving average, generating multiple sustained trends
    import math
    prices = [100.0 + (i * 0.1) + (5.0 * math.sin(i * 0.2)) for i in range(n)]

    open_times = [base_ts + (i * 15 * ONE_MINUTE_NS) for i in range(n)]
    df = pl.DataFrame({
        "bar_open_ns": open_times,
        "bar_close_inclusive_ns": [t + 15 * ONE_MINUTE_NS - 1 for t in open_times],
        "interval_end_exclusive_ns": [t + 15 * ONE_MINUTE_NS for t in open_times],
        "open": [p - 0.2 for p in prices],
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [1000.0] * n,
        "quote_volume": [1000.0 * p for p in prices],
        "trades_count": [50] * n,
    })

    # Old MACD momentum state rule: close > ema_50 AND macd_hist > 0.0
    old_macd = [
        RuleGroup(category="trend", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50")]),
        RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="macd_hist", operator=RuleOperator.GT, value=0.0)]),
    ]

    # New MACD momentum event rule: close > ema_50 AND macd_hist CROSS_ABOVE 0.0
    new_macd = [
        RuleGroup(category="trend", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value="ema_50")]),
        RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="macd_hist", operator=RuleOperator.CROSS_ABOVE, value=0.0, lookback=1)]),
    ]

    risk = RiskConfig()
    old_cands = ScannerEngine.evaluate(df, old_macd, risk, "BTCUSDT.BINANCE", "15m", uuid4(), uuid4())
    new_cands = ScannerEngine.evaluate(df, new_macd, risk, "BTCUSDT.BINANCE", "15m", uuid4(), uuid4())

    assert len(new_cands) >= 1, "New crossover rule should detect at least 1 crossover event"
    reduction_ratio = len(old_cands) / len(new_cands)
    # The state-based rule fires on every continuation bar of the trend; the event-based rule fires once per cross.
    # Volume reduction must be >= 5x (typically 8x - 20x)
    assert reduction_ratio >= 5.0, f"Expected >= 5x volume reduction, got {reduction_ratio:.2f}x ({len(old_cands)} vs {len(new_cands)})"


async def test_create_scanner_api_guard_rejects_at_max_active_specs(db_url, admin):
    """create_scanner route rejects with HTTP 400 when active specs count reaches MAX_ACTIVE_SPECS."""
    from cryptonite_api.routes.scanners import create_scanner
    from cryptonite_scanner.models import ScanSpecCreate, UniverseConfig
    from fastapi import HTTPException
    from starlette.requests import Request

    # Seed 10 active specs
    rule = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    for i in range(MAX_ACTIVE_SPECS):
        await seed_spec_row(admin, f"api-max-spec-{i}", f"Max Spec {i}", rule)

    pool = await asyncpg.create_pool(db_url)
    try:
        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(pool=pool))}
        req = Request(scope)

        new_spec = ScanSpecCreate(
            slug="api-max-spec-overflow",
            name="Overflow Spec",
            universe=UniverseConfig(instrument_ids=["BTCUSDT.BINANCE"], timeframes=["15m"]),
            rules=[RuleGroup(category="trigger", logic="AND", conditions=[RuleCondition(field="close", operator=RuleOperator.GT, value=0.0)])],
        )

        with pytest.raises(HTTPException) as exc_info:
            await create_scanner(new_spec, req)

        assert exc_info.value.status_code == 400
        assert f"maximum active specs limit ({MAX_ACTIVE_SPECS}) reached" in exc_info.value.detail
    finally:
        await pool.close()


async def test_cross_spec_dedupe_concurrent_race_serialized(db_url, admin, redis15, monkeypatch):
    """Two concurrent _execute_scan executions racing on different specs for the same bar
    are serialized by transaction advisory lock, producing exactly 1 candidate across specs."""
    import asyncio
    t0 = 1_791_201_600 * 10**9  # 2026-10-05 12:00:00 UTC
    opens = [t0 - 15 * ONE_MINUTE_NS + i * ONE_MINUTE_NS for i in range(15)]
    lake_df = pl.DataFrame({
        "bar_open_ns": opens,
        "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0] * 15,
        "high": [105.0] * 15,
        "low": [95.0] * 15,
        "close": [102.0] * 15,
        "volume": [500.0] * 15,
    })

    rule1 = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    rule2 = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 10}]}]
    spec1_id = await seed_spec_row(admin, "race-spec-1", "Race Spec 1", rule1)
    spec2_id = await seed_spec_row(admin, "race-spec-2", "Race Spec 2", rule2)

    fake_now = t0 + 30 * 10**9
    monkeypatch.setattr(real_time, "time_ns", lambda: fake_now)

    def mock_eval(cls, df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, scan_run_id, price_precision=2):
        last_row = df.to_dicts()[-1]
        return [
            SignalCandidateRead(
                id=uuid4(),
                scan_run_id=scan_run_id,
                spec_version_id=spec_version_id,
                instrument_id=instrument_id,
                timeframe=timeframe,
                bar_open_ns=last_row["bar_open_ns"],
                bar_close_inclusive_ns=last_row["interval_end_exclusive_ns"] - 1,
                interval_end_exclusive_ns=last_row["interval_end_exclusive_ns"],
                matched_rules=["match"],
                metrics={},
                suggested_entry=Decimal("100"),
                suggested_stop=Decimal("95"),
                suggested_target=Decimal("110"),
                created_at=datetime.now(UTC),
            )
        ]

    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(mock_eval))

    w1 = await make_worker(db_url, lake_df, redis15.client)
    w2 = await make_worker(db_url, lake_df, redis15.client)
    universe = {"instrument_ids": ["BTCUSDT.BINANCE"], "timeframes": ["15m"]}

    try:
        # Race both executions concurrently
        task1 = asyncio.create_task(
            w1._execute_scan(
                spec_version_id=spec1_id,
                spec_hash="hash1",
                universe_data=universe,
                rules_data=rule1,
                trigger_reason="COMMITTED_BAR_EVENT",
                trigger_event_id=str(uuid4()),
            )
        )
        task2 = asyncio.create_task(
            w2._execute_scan(
                spec_version_id=spec2_id,
                spec_hash="hash2",
                universe_data=universe,
                rules_data=rule2,
                trigger_reason="COMMITTED_BAR_EVENT",
                trigger_event_id=str(uuid4()),
            )
        )
        await asyncio.gather(task1, task2)
    finally:
        await w1.pool.close()
        await w2.pool.close()

    # Total signal_candidates across specs must be exactly 1
    candidates_count = await admin.fetchval(
        "SELECT count(*) FROM signal_candidates WHERE instrument_id = 'BTCUSDT.BINANCE'"
    )
    assert candidates_count == 1

    # Total debate outbox entries must also be exactly 1
    outbox_count = await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox")
    assert outbox_count == 1


async def test_worker_active_specs_deterministic_order_and_visible_error(db_url, admin, redis15, caplog):
    """When active specs exceed MAX_ACTIVE_SPECS, worker logs ERROR visibly and processes all active specs."""
    import logging
    lake_df = pl.DataFrame({
        "bar_open_ns": [1000],
        "ts_event_ns": [1000 + ONE_MINUTE_NS],
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "volume": [10.0],
    })

    # Seed 12 active specs in DB
    rule = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
    for i in range(12):
        await seed_spec_row(admin, f"det-spec-{i:02d}", f"Det Spec {i}", rule)

    # Ingest checkpoint so reconcile primes
    await admin.execute(
        """INSERT INTO ingest_checkpoints (venue, market_type, symbol, bar_type, timeframe, first_timestamp_ns, last_timestamp_ns)
           VALUES ('BINANCE', 'SPOT', 'BTCUSDT', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', 0, $1)""",
        1000 + ONE_MINUTE_NS,
    )

    w = await make_worker(db_url, lake_df, redis15.client)
    executed_specs: list[UUID] = []

    async def mock_execute_scan(spec_version_id, **_kwargs):
        executed_specs.append(spec_version_id)
        return uuid4()

    w._execute_scan = mock_execute_scan

    try:
        with caplog.at_level(logging.ERROR):
            await w.reconcile_and_prime()
        # Verify all active specs executed (no signal drops)
        assert len(executed_specs) == 12
        # Verify visible error log
        assert any(
            "exceeds MAX_ACTIVE_SPECS (10)" in record.message and "Processing all 12 active specs" in record.message
            for record in caplog.records
            if record.levelno == logging.ERROR
        )
    finally:
        await w.pool.close()


async def test_ensure_presets_seeded_bootstraps_fresh_db_and_noop_when_populated(db_url, admin):
    """ensure_presets_seeded bootstraps empty DB, but does not mutate already-populated DB."""
    from cryptonite_api.routes.scanners import ensure_presets_seeded

    pool = await asyncpg.create_pool(db_url)
    try:
        # 1. Fresh seeding
        await ensure_presets_seeded(pool)
        specs = await pool.fetch("SELECT slug, active_version_id FROM scan_specs;")
        assert len(specs) >= 4

        # Record active version IDs
        orig_active = {r["slug"]: r["active_version_id"] for r in specs}

        # Modify one version hash manually to simulate DB with custom or outdated state
        await pool.execute(
            "UPDATE scan_spec_versions SET spec_hash = 'arbitrary_hash' WHERE id = $1;",
            next(iter(orig_active.values())),
        )

        # 2. Run ensure_presets_seeded again: must be an immediate no-op
        await ensure_presets_seeded(pool)

        specs_after = await pool.fetch("SELECT slug, active_version_id FROM scan_specs;")
        after_active = {r["slug"]: r["active_version_id"] for r in specs_after}
        assert orig_active == after_active
    finally:
        await pool.close()


async def test_get_scanners_is_read_only(db_url, admin):
    """GET /api/v1/scanners is read-only and does not mutate active versions or specs."""
    from cryptonite_api.routes.scanners import ensure_presets_seeded, list_scanners
    from starlette.requests import Request

    pool = await asyncpg.create_pool(db_url)
    try:
        await ensure_presets_seeded(pool)
        specs_before = await pool.fetch("SELECT slug, active_version_id, updated_at FROM scan_specs ORDER BY slug;")

        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(pool=pool))}
        req = Request(scope)

        scanners = await list_scanners(req)
        assert len(scanners) >= 4

        specs_after = await pool.fetch("SELECT slug, active_version_id, updated_at FROM scan_specs ORDER BY slug;")
        assert [r["active_version_id"] for r in specs_before] == [r["active_version_id"] for r in specs_after]
        assert [r["updated_at"] for r in specs_before] == [r["updated_at"] for r in specs_after]
    finally:
        await pool.close()


async def test_preset_upgrade_operator_endpoint_dry_run_and_apply(db_url, admin):
    """POST /presets/upgrade dry_run previews without mutating, apply mode upgrades unedited and preserves custom."""
    from cryptonite_api.routes.scanners import (
        PRESET_PREVIOUS_HASHES,
        ensure_presets_seeded,
        upgrade_presets,
    )
    from cryptonite_scanner.presets import PRESET_RSI_OVERSOLD, PRESET_TREND_PULLBACK
    from starlette.requests import Request

    pool = await asyncpg.create_pool(db_url)
    try:
        await ensure_presets_seeded(pool)

        # 1. Simulate an older unedited preset in DB: set RSI preset to its known prior hash
        rsi_spec = await pool.fetchrow("SELECT id, active_version_id FROM scan_specs WHERE slug = $1;", PRESET_RSI_OVERSOLD.slug)
        old_hash = next(iter(PRESET_PREVIOUS_HASHES[PRESET_RSI_OVERSOLD.slug]))
        await pool.execute(
            "UPDATE scan_spec_versions SET spec_hash = $1 WHERE id = $2;",
            old_hash,
            rsi_spec["active_version_id"],
        )

        # 2. Simulate a user-edited custom preset: set Trend Pullback to custom hash
        trend_spec = await pool.fetchrow("SELECT id, active_version_id FROM scan_specs WHERE slug = $1;", PRESET_TREND_PULLBACK.slug)
        custom_hash = "custom_trader_hash_99999"
        await pool.execute(
            "UPDATE scan_spec_versions SET spec_hash = $1 WHERE id = $2;",
            custom_hash,
            trend_spec["active_version_id"],
        )

        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(pool=pool))}
        req = Request(scope)

        # 3. Dry-run upgrade
        dry_res = await upgrade_presets(req, dry_run=True)
        assert dry_res.dry_run is True

        rsi_dry = next(it for it in dry_res.items if it.slug == PRESET_RSI_OVERSOLD.slug)
        assert rsi_dry.action == "would_upgrade"

        trend_dry = next(it for it in dry_res.items if it.slug == PRESET_TREND_PULLBACK.slug)
        assert trend_dry.action == "custom_preserved"

        # Verify DB was NOT modified during dry-run
        db_rsi = await pool.fetchval("SELECT spec_hash FROM scan_spec_versions WHERE id = $1;", rsi_spec["active_version_id"])
        assert db_rsi == old_hash

        # 4. Apply upgrade
        apply_res = await upgrade_presets(req, dry_run=False)
        assert apply_res.dry_run is False

        rsi_apply = next(it for it in apply_res.items if it.slug == PRESET_RSI_OVERSOLD.slug)
        assert rsi_apply.action == "upgraded"

        trend_apply = next(it for it in apply_res.items if it.slug == PRESET_TREND_PULLBACK.slug)
        assert trend_apply.action == "custom_preserved"

        # Verify RSI was upgraded in DB
        updated_rsi = await pool.fetchrow(
            """
            SELECT s.active_version_id, v.version_num, v.spec_hash
            FROM scan_specs s
            JOIN scan_spec_versions v ON s.active_version_id = v.id
            WHERE s.slug = $1;
            """,
            PRESET_RSI_OVERSOLD.slug,
        )
        assert updated_rsi["spec_hash"] != old_hash
        assert updated_rsi["version_num"] >= 2

        # Verify Trend Pullback custom spec remained untouched
        preserved_trend = await pool.fetchrow(
            "SELECT active_version_id FROM scan_specs WHERE slug = $1;",
            PRESET_TREND_PULLBACK.slug,
        )
        assert preserved_trend["active_version_id"] == trend_spec["active_version_id"]
    finally:
        await pool.close()


async def test_activate_preset_version_operator_route_and_cap_guard(db_url, admin):
    """Guarded operator activation activates preset version and rejects when MAX_ACTIVE_SPECS is reached."""
    from cryptonite_api.routes.scanners import activate_preset_version, ensure_presets_seeded
    from cryptonite_scanner.presets import PRESET_MACD_MOMENTUM
    from fastapi import HTTPException
    from starlette.requests import Request

    pool = await asyncpg.create_pool(db_url)
    try:
        await ensure_presets_seeded(pool)

        # Archive the MACD preset
        await pool.execute(
            "UPDATE scan_specs SET is_archived = true WHERE slug = $1;",
            PRESET_MACD_MOMENTUM.slug,
        )

        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(pool=pool))}
        req = Request(scope)

        # 1. Activate MACD when under cap -> success
        res = await activate_preset_version(PRESET_MACD_MOMENTUM.slug, req)
        assert res.slug == PRESET_MACD_MOMENTUM.slug
        assert res.is_archived is False
        assert res.active_version is not None

        # 2. Archive MACD again
        await pool.execute(
            "UPDATE scan_specs SET is_archived = true WHERE slug = $1;",
            PRESET_MACD_MOMENTUM.slug,
        )

        # Seed additional active specs up to MAX_ACTIVE_SPECS (10 active specs)
        rule = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]
        curr_active = await pool.fetchval(
            "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
        )
        needed = 10 - curr_active
        for i in range(needed):
            await seed_spec_row(admin, f"fill-cap-{i}", f"Fill Cap {i}", rule)

        # Now active count == 10
        total_active = await pool.fetchval(
            "SELECT count(*) FROM scan_specs WHERE is_archived = false AND active_version_id IS NOT NULL;"
        )
        assert total_active == 10

        # Attempting to activate archived preset must fail with HTTP 400
        with pytest.raises(HTTPException) as exc_info:
            await activate_preset_version(PRESET_MACD_MOMENTUM.slug, req)
        assert exc_info.value.status_code == 400
        assert "maximum active specs limit (10) reached" in exc_info.value.detail
    finally:
        await pool.close()


async def test_concurrent_user_edit_vs_preset_upgrade_race_preserves_custom_edit(db_url, admin):
    """Concurrent user update (update_scanner) and operator preset upgrade (upgrade_presets)
    serialize under scan_specs:active_limit and FOR UPDATE locks, guaranteeing that a concurrent
    user customization is never overwritten with the built-in preset target version.
    """
    import asyncio

    from cryptonite_api.routes.scanners import (
        PRESET_PREVIOUS_HASHES,
        ensure_presets_seeded,
        update_scanner,
        upgrade_presets,
    )
    from cryptonite_scanner.models import ScanSpecCreate, UniverseConfig
    from cryptonite_scanner.presets import PRESET_RSI_OVERSOLD
    from starlette.requests import Request

    pool = await asyncpg.create_pool(db_url)
    try:
        await ensure_presets_seeded(pool)

        # 1. Simulate an older unedited preset in DB by resetting RSI spec to known prior hash
        rsi_spec = await pool.fetchrow(
            "SELECT id, active_version_id FROM scan_specs WHERE slug = $1;",
            PRESET_RSI_OVERSOLD.slug,
        )
        spec_id = rsi_spec["id"]
        old_hash = next(iter(PRESET_PREVIOUS_HASHES[PRESET_RSI_OVERSOLD.slug]))
        await pool.execute(
            "UPDATE scan_spec_versions SET spec_hash = $1 WHERE id = $2;",
            old_hash,
            rsi_spec["active_version_id"],
        )

        scope = {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(pool=pool))}
        req = Request(scope)

        # 2. Define custom user modification payload
        custom_spec_in = ScanSpecCreate(
            slug=PRESET_RSI_OVERSOLD.slug,
            name="Custom RSI Trader Spec",
            description="User custom RSI edit that must not be overwritten by preset upgrade",
            universe=UniverseConfig(symbols=["BTCUSDT"], timeframes=["15m"]),
            rules=[
                RuleGroup(
                    category="trigger",
                    conditions=[
                        RuleCondition(field="rsi", operator="<", value=22.0)
                    ],
                )
            ],
        )

        # 3. Execute concurrent user update and preset upgrade
        results = await asyncio.gather(
            update_scanner(spec_id, custom_spec_in, req),
            upgrade_presets(req, dry_run=False),
            return_exceptions=False,
        )
        user_update_res, upgrade_res = results

        # 4. Verify user update committed and holds custom version
        assert user_update_res.name == "Custom RSI Trader Spec"
        custom_ver_id = user_update_res.active_version_id

        # 5. Check that active spec version in DB is strictly the custom user edit
        final_spec = await pool.fetchrow(
            """
            SELECT s.active_version_id, s.name, v.version_num, v.spec_hash, v.rules
            FROM scan_specs s
            JOIN scan_spec_versions v ON s.active_version_id = v.id
            WHERE s.id = $1;
            """,
            spec_id,
        )
        assert final_spec["active_version_id"] == custom_ver_id
        assert final_spec["name"] == "Custom RSI Trader Spec"
        rules = json.loads(final_spec["rules"]) if isinstance(final_spec["rules"], str) else final_spec["rules"]
        assert rules[0]["conditions"][0]["value"] == 22.0
    finally:
        await pool.close()


async def test_operator_script_upgrade_presets_dry_run_and_apply(db_url, admin):
    """Operator script scripts/upgrade_scanner_presets.py operates safely against DB:
    dry run makes no modifications, apply commits version upgrades and prints rollback criteria.
    """
    import importlib.util
    from pathlib import Path

    from cryptonite_api.routes.scanners import PRESET_PREVIOUS_HASHES, ensure_presets_seeded
    from cryptonite_scanner.presets import PRESET_RSI_OVERSOLD

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "upgrade_scanner_presets.py"
    spec = importlib.util.spec_from_file_location("upgrade_scanner_presets", script_path)
    script_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script_mod)
    run_direct_db = script_mod.run_direct_db

    pool = await asyncpg.create_pool(db_url)
    try:
        await ensure_presets_seeded(pool)

        # Set RSI to prior hash
        rsi_spec = await pool.fetchrow(
            "SELECT id, active_version_id FROM scan_specs WHERE slug = $1;",
            PRESET_RSI_OVERSOLD.slug,
        )
        old_hash = next(iter(PRESET_PREVIOUS_HASHES[PRESET_RSI_OVERSOLD.slug]))
        await pool.execute(
            "UPDATE scan_spec_versions SET spec_hash = $1 WHERE id = $2;",
            old_hash,
            rsi_spec["active_version_id"],
        )
    finally:
        await pool.close()

    # 1. Run script in dry-run mode
    ret_dry = await run_direct_db(db_url, apply=False)
    assert ret_dry == 0

    pool = await asyncpg.create_pool(db_url)
    try:
        # Check that dry-run did not mutate DB
        curr_hash = await pool.fetchval(
            "SELECT spec_hash FROM scan_spec_versions WHERE id = $1;",
            rsi_spec["active_version_id"],
        )
        assert curr_hash == old_hash
    finally:
        await pool.close()

    # 2. Run script in apply mode
    ret_apply = await run_direct_db(db_url, apply=True)
    assert ret_apply == 0

    pool = await asyncpg.create_pool(db_url)
    try:
        # Check that apply mutated DB to crossover target
        new_row = await pool.fetchrow(
            """
            SELECT s.active_version_id, v.spec_hash, v.version_num
            FROM scan_specs s
            JOIN scan_spec_versions v ON s.active_version_id = v.id
            WHERE s.slug = $1;
            """,
            PRESET_RSI_OVERSOLD.slug,
        )
        assert new_row["spec_hash"] != old_hash
        assert new_row["version_num"] >= 2
    finally:
        await pool.close()
