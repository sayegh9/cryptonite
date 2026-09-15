"""Test post-subscription watermark persistence and closed gate protection on DB failure."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptonite_execution.engine import PaperTradingEngine
from nautilus_trader.model import Bar, BarType, Price, Quantity


def make_test_bar(bar_type: BarType, ts_event: int, close_val: str) -> Bar:
    p = Price.from_str(close_val)
    q = Quantity.from_str("1.0")
    return Bar(
        bar_type=bar_type,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=q,
        ts_event=ts_event,
        ts_init=ts_event,
    )


@pytest.mark.asyncio
async def test_processed_high_water_mark_persistence_success():
    """Verify that the actual greatest routed bar timestamp (max_processed_bar_ns)
    is persisted to DB after routing, and gate opens upon successful commit.
    """
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    base_ts = 1_700_000_000_000_000_000
    step_ns = 60 * 1_000_000_000

    # Historical bars 1..5, C is bar 5
    hist_bars = [
        make_test_bar(bar_type, base_ts + i * step_ns, f"{50000 + i * 10}.00")
        for i in range(1, 6)
    ]
    watermark_c = hist_bars[-1].ts_event

    # Buffered bars: bar 5 (duplicate <= C), bar 6 (> C), bar 7 (> C)
    buffered_bar_5 = hist_bars[-1]
    buffered_bar_6 = make_test_bar(bar_type, base_ts + 6 * step_ns, "50060.00")
    buffered_bar_7 = make_test_bar(bar_type, base_ts + 7 * step_ns, "50070.00")

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001",
        trader_id="TRADER-TEST-001",
        engine_run_id="RUN-001",
        pg_pool=mock_pool,
        redis_client=MagicMock(),
    )

    # Ingest health mock: always fresh
    engine.ingress_health.is_fresh = MagicMock(return_value=True)

    # Buffer the bars while gate is closed
    engine.on_live_bar(buffered_bar_5)
    engine.on_live_bar(buffered_bar_6)
    engine.on_live_bar(buffered_bar_7)

    assert not engine.is_gate_open

    # Run startup handoff protocol
    success = await engine.execute_startup_handoff(
        symbol="BTCUSDT",
        bar_type_str=str(bar_type),
        catalog_watermark_c_ns=watermark_c,
        historical_warmup_bars=hist_bars,
    )

    assert success is True
    assert engine.is_gate_open is True

    # Assert greatest processed timestamp is bar 7 (not merely watermark C)
    expected_max_ts = buffered_bar_7.ts_event
    assert engine.router.max_processed_bar_ns == expected_max_ts

    # Verify DB update executed with the actual max_processed_bar_ns
    mock_conn.execute.assert_called_once()
    sql_args = mock_conn.execute.call_args[0]
    assert "UPDATE paper_strategy_instances" in sql_args[0]
    assert sql_args[1] == "PAPER-ACCOUNT-001"
    assert sql_args[2] == expected_max_ts  # persisted greatest routed bar!


@pytest.mark.asyncio
async def test_watermark_persistence_failure_keeps_gate_closed():
    """Verify that if database persistence fails, the live gate remains closed."""
    bar_type = BarType.from_str("BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL")
    base_ts = 1_700_000_000_000_000_000
    step_ns = 60 * 1_000_000_000

    hist_bars = [make_test_bar(bar_type, base_ts + step_ns, "50010.00")]
    watermark_c = hist_bars[0].ts_event

    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = RuntimeError("Database connection severed during checkpoint commit")
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001",
        trader_id="TRADER-TEST-002",
        engine_run_id="RUN-002",
        pg_pool=mock_pool,
        redis_client=MagicMock(),
    )

    # Ingress health is fresh
    engine.ingress_health.is_fresh = MagicMock(return_value=True)

    success = await engine.execute_startup_handoff(
        symbol="BTCUSDT",
        bar_type_str=str(bar_type),
        catalog_watermark_c_ns=watermark_c,
        historical_warmup_bars=hist_bars,
    )

    # Handshake must fail and gate must remain CLOSED
    assert success is False
    assert engine.is_gate_open is False
