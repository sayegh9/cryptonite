"""Unit tests for AI Brain Chat Swing Tools (WS-S3).

Verifies:
1. `scan_swing_setups` returning valid JSON with candidates, MTF trends (trend_4h, trend_1h, trend_15m),
   ATR-calibrated entry, stop, target, and rr_ratio >= 1.8 on synthetic trending bars.
2. `widget_for_tool("scan_swing_setups", ...)` producing a valid `SwingRadarWidget` (`kind == "swing_radar"`).
3. `show_price_chart` and `widget_for_tool("show_price_chart", ...)` with `entry`, `stop`, `target`
   populating `PriceChartWidget.entry`, `.stop`, `.target`.
4. Invariants on tool registration, RiskClass, and BRAIN_TOOLS ordering.
"""

import json
import time

import polars as pl
import pytest
from cryptonite_agents.brain.toolset import BRAIN_TOOLS, show_price_chart
from cryptonite_agents.brain.widgets import (
    PriceChartWidget,
    SwingRadarWidget,
    describe_widget,
    widget_for_tool,
)
from cryptonite_agents.gateway import RiskClass
from cryptonite_agents.tools import (
    SCAN_SWING_SETUPS,
    DuckDBLakeRepository,
    scan_swing_setups,
)
from cryptonite_scanner.presets import SWING_LIQUID_12


def make_synthetic_1m_bars(
    num_bars: int = 1500,
    start_price: float = 60000.0,
    drift: float = 10.0,
    end_ns: int | None = None,
) -> pl.DataFrame:
    """Generate contiguous, strictly UTC-aligned 1m bars for testing."""
    now_ns = end_ns or time.time_ns()
    minute_ns = 60 * 1_000_000_000
    now_ns = (now_ns // minute_ns) * minute_ns
    start_ns = now_ns - num_bars * minute_ns

    ts_event_ns = [start_ns + (i + 1) * minute_ns for i in range(num_bars)]
    bar_open_ns = [ts - minute_ns for ts in ts_event_ns]

    opens, highs, lows, closes, volumes = [], [], [], [], []
    price = start_price
    for i in range(num_bars):
        p_open = price
        p_close = price + drift
        p_high = max(p_open, p_close) + 5.0
        p_low = min(p_open, p_close) - 5.0
        p_vol = 100.0 + (i % 20)
        opens.append(p_open)
        highs.append(p_high)
        lows.append(p_low)
        closes.append(p_close)
        volumes.append(p_vol)
        price = p_close

    return pl.DataFrame({
        "bar_open_ns": bar_open_ns,
        "bar_close_inclusive_ns": [ts - 1_000_000 for ts in ts_event_ns],
        "interval_end_exclusive_ns": ts_event_ns,
        "ts_event_ns": ts_event_ns,
        "ts_init_ns": ts_event_ns,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "quote_volume": [v * c for v, c in zip(volumes, closes, strict=False)],
        "trades_count": [100] * num_bars,
    })


# -----------------------------------------------------------------------------
# 1. scan_swing_setups Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_swing_setups_returns_valid_json_and_candidates_on_synthetic_trending_bars(monkeypatch):
    """Verifies that scan_swing_setups produces ranked candidates with MTF trends,

    valid entry/stop/target geometry, and rr_ratio >= 1.8.
    """
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)

    def fake_load_bars(self, symbol: str, timeframe: str = "1m", start_ns: int | None = None, end_ns: int | None = None):
        if symbol == "BTCUSDT":
            return synthetic_bars
        return pl.DataFrame()

    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", fake_load_bars)

    raw_result = await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=5)
    data = json.loads(raw_result)

    assert data["preset_slug"] == "all"
    assert data["timeframe"] == "1h"
    assert data["scanned_count"] == len(SWING_LIQUID_12)
    assert "as_of" in data
    assert isinstance(data["candidates"], list)
    assert len(data["candidates"]) >= 1

    cand = data["candidates"][0]
    assert cand["instrument_id"] == "BTCUSDT.BINANCE"
    assert cand["timeframe"] == "1h"
    assert cand["direction"] == "LONG"
    assert cand["trend_4h"] == "BULL"
    assert cand["trend_1h"] == "BULL"
    assert cand["trend_15m"] == "BULL"
    assert cand["entry"] > 0
    assert cand["stop"] < cand["entry"]
    assert cand["target"] > cand["entry"]
    assert cand["rr_ratio"] >= 1.8
    assert 0 <= cand["score"] <= 100
    assert isinstance(cand["matched_rules"], list)
    assert len(cand["matched_rules"]) <= 4


@pytest.mark.asyncio
async def test_scan_swing_setups_preset_filter_and_clamping(monkeypatch):
    """Verifies preset filtering by slug and max_results clamping to [1, 10]."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)

    def fake_load_bars(self, symbol: str, timeframe: str = "1m", start_ns: int | None = None, end_ns: int | None = None):
        return synthetic_bars

    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", fake_load_bars)

    # 1. Specific preset
    raw = await scan_swing_setups(preset_slug="swing-trend-pullback-1h", timeframe="1h", max_results=2)
    data = json.loads(raw)
    assert data["preset_slug"] == "swing-trend-pullback-1h"
    assert len(data["candidates"]) <= 2

    # 2. Clamping max_results <= 0 -> 1
    raw_clamped_min = await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=0)
    data_min = json.loads(raw_clamped_min)
    assert len(data_min["candidates"]) <= 1

    # 3. Fallback when unknown preset_slug is passed
    raw_unknown = await scan_swing_setups(preset_slug="unknown-preset-xyz", timeframe="1h", max_results=3)
    data_unknown = json.loads(raw_unknown)
    assert "candidates" in data_unknown


@pytest.mark.asyncio
async def test_scan_swing_setups_empty_lake_fallback(monkeypatch):
    """Verifies graceful handling when the lake is completely empty."""
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, *args, **kwargs: pl.DataFrame())
    monkeypatch.setenv("DATABASE_URL", "")

    raw = await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=5)
    data = json.loads(raw)
    assert data["candidates"] == []
    assert data["scanned_count"] == len(SWING_LIQUID_12)


# -----------------------------------------------------------------------------
# 2. Swing Radar Widget Tests
# -----------------------------------------------------------------------------

def test_widget_for_tool_scan_swing_setups_success():
    """Verifies widget_for_tool produces a valid SwingRadarWidget."""
    payload = {
        "preset_slug": "all",
        "timeframe": "1h",
        "scanned_count": 12,
        "as_of": "2026-10-17T20:00:00+00:00",
        "candidates": [
            {
                "instrument_id": "BTCUSDT.BINANCE",
                "preset_slug": "swing-trend-pullback-1h",
                "timeframe": "1h",
                "direction": "LONG",
                "score": 95.0,
                "entry": 65000.0,
                "stop": 63500.0,
                "target": 68000.0,
                "rr_ratio": 2.0,
                "trend_4h": "BULL",
                "trend_1h": "BULL",
                "trend_15m": "BULL",
                "matched_rules": ["[TREND] close > ema_50", "[TRIGGER] rsi CROSS_ABOVE 40.0"],
            }
        ],
    }

    widget = widget_for_tool(
        tool="scan_swing_setups",
        args={"preset_slug": "all", "timeframe": "1h"},
        call_id="call_radar_1",
        content=json.dumps(payload),
    )

    assert widget is not None
    assert widget["kind"] == "swing_radar"
    assert widget["preset_slug"] == "all"
    assert widget["timeframe"] == "1h"
    assert widget["scanned_count"] == 12
    assert len(widget["candidates"]) == 1

    # Validate against pydantic schema
    radar_model = SwingRadarWidget.model_validate(widget)
    assert radar_model.kind == "swing_radar"
    assert len(radar_model.candidates) == 1
    assert radar_model.candidates[0].score == 95.0
    assert radar_model.candidates[0].rr_ratio == 2.0

    # Verify describe_widget format
    desc = describe_widget(widget)
    assert desc == "[widget:swing_radar preset=all candidates=1]"


def test_widget_for_tool_scan_swing_setups_invalid_content():
    """Verifies invalid or non-JSON content produces None."""
    assert widget_for_tool("scan_swing_setups", {}, call_id="c1", content="invalid json") is None
    assert widget_for_tool("scan_swing_setups", {}, call_id="c2", content="") is None
    assert widget_for_tool("scan_swing_setups", {}, call_id="c3", content='{"candidates": "not a list"}') is None


# -----------------------------------------------------------------------------
# 3. show_price_chart & Level Overlays Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_show_price_chart_with_geometry_levels():
    """Verifies show_price_chart accepts entry, stop, and target overlay levels."""
    res = await show_price_chart.func(
        symbol="BTCUSDT",
        timeframe="1h",
        bars=96,
        entry=65000.0,
        stop=63500.0,
        target=68000.0,
    )

    assert res["rendered"] == "price_chart"
    assert res["symbol"] == "BTCUSDT"
    assert res["timeframe"] == "1h"
    assert res["bars"] == 96
    assert res["entry"] == 65000.0
    assert res["stop"] == 63500.0
    assert res["target"] == 68000.0


def test_widget_for_tool_show_price_chart_with_levels():
    """Verifies widget_for_tool correctly parses entry, stop, target into PriceChartWidget."""
    args = {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "bars": 120,
        "entry": 65000.0,
        "stop": 63500.0,
        "target": 68000.0,
    }

    widget = widget_for_tool("show_price_chart", args, call_id="chart_call_1")
    assert widget is not None
    assert widget["kind"] == "price_chart"
    assert widget["symbol"] == "BTCUSDT"
    assert widget["timeframe"] == "4h"
    assert widget["bars"] == 120
    assert widget["entry"] == 65000.0
    assert widget["stop"] == 63500.0
    assert widget["target"] == 68000.0

    chart_model = PriceChartWidget.model_validate(widget)
    assert chart_model.entry == 65000.0

    desc = describe_widget(widget)
    assert desc.startswith("[widget:price_chart BTCUSDT 4h (120 bars")
    assert "entry=65000.0" in desc
    assert "stop=63500.0" in desc
    assert "target=68000.0" in desc


def test_describe_widget_price_chart_backward_compatibility():
    """Verifies legacy price_chart without levels maintains exact [price chart SYMBOL TF] format."""
    widget = {"kind": "price_chart", "symbol": "BTCUSDT", "timeframe": "1h", "bars": 96}
    assert describe_widget(widget) == "[price chart BTCUSDT 1h]"


# -----------------------------------------------------------------------------
# 4. Invariants & Registration Tests
# -----------------------------------------------------------------------------

def test_scan_swing_setups_is_read_tool_and_ordered_correctly():
    """Verifies critical existing test invariants:

    1. All tools in BRAIN_TOOLS except propose_order must have RiskClass.READ.
    2. SCAN_SWING_SETUPS must sit right after COMPARE_LIVE_VS_BACKTEST.
    3. show_price_chart docstring invariants.
    """
    assert SCAN_SWING_SETUPS.risk is RiskClass.READ
    assert SCAN_SWING_SETUPS.side_effects is False

    names = [t.name for t in BRAIN_TOOLS]
    idx_compare = names.index("compare_live_vs_backtest")
    idx_swing = names.index("scan_swing_setups")
    idx_chart = names.index("show_price_chart")

    assert idx_swing == idx_compare + 1
    assert idx_chart == idx_swing + 1

    doc = " ".join(show_price_chart.func.__doc__.split())
    assert "Only for symbols the ingest feed records (get_klines reports in_feed)." in doc
    assert "Use it whenever you discuss price action." not in doc
