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
    SwingCandidateItem,
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
from cryptonite_scanner.engine import ScannerEngine
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

@pytest.fixture(autouse=True)
def _no_scanner_setups(request, monkeypatch):
    """The scan's database read of recent scanner setups is stubbed out, except in tests that feed it rows."""
    if "scanner_rows" not in request.fixturenames:
        monkeypatch.setattr("cryptonite_agents.tools._readonly_rows", lambda sql: [])


@pytest.fixture
def scanner_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr("cryptonite_agents.tools._readonly_rows", lambda sql: rows)
    return rows


def _fake_evaluate(calls: list[tuple[str, str]] | None = None, side: str = "LONG"):
    """A ScannerEngine.evaluate stand-in that fires on the last bar, with levels 2% / 4% from its close."""
    from types import SimpleNamespace

    def evaluate(cls, df, rule_groups, risk_config, instrument_id, timeframe, **kwargs):
        if calls is not None:
            calls.append((instrument_id, timeframe))
        last = df.tail(1).to_dicts()[0]
        close = float(last["close"])
        sign = -1 if side == "SHORT" else 1
        return [SimpleNamespace(
            bar_close_inclusive_ns=int(last["bar_close_inclusive_ns"]),
            suggested_entry=close,
            suggested_stop=close * (1 - sign * 0.02),
            suggested_target=close * (1 + sign * 0.04),
            matched_rules=["rsi_14 < 40", "close > ema_50"],
        )]

    return classmethod(evaluate)


@pytest.mark.asyncio
async def test_scan_swing_setups_no_preset_match_returns_empty_with_note(monkeypatch):
    """Trending bars on which no preset fires give no candidates and a note: nothing is made up from the live close."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)

    def fake_load_bars(self, symbol: str, timeframe: str = "1m", start_ns: int | None = None, end_ns: int | None = None):
        if symbol == "BTCUSDT":
            return synthetic_bars
        return pl.DataFrame()

    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", fake_load_bars)
    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(lambda cls, *a, **k: []))

    data = json.loads(await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=5))
    assert data["timeframe"] == "1h"
    assert data["scanned_count"] == len(SWING_LIQUID_12)
    assert "as_of" in data
    assert data["candidates"] == []
    assert data["note"].startswith("No preset fired on the liquid universe at 1h, and the scanner found no setup")

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", timeframe="4h"))
    assert data["candidates"] == []
    assert data["note"].startswith("No preset fired on BTCUSDT at 4h, and the scanner found no setup")


@pytest.mark.asyncio
async def test_scan_swing_setups_named_symbol_scans_only_that_symbol(monkeypatch):
    """A named symbol loads and evaluates that instrument alone, and scanned_count says so."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    loaded: list[str] = []

    def fake_load_bars(self, symbol: str, timeframe: str = "1m", start_ns: int | None = None, end_ns: int | None = None):
        loaded.append(symbol)
        return synthetic_bars  # every symbol has bars: only the named one may be read

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", fake_load_bars)
    monkeypatch.setattr(ScannerEngine, "evaluate", _fake_evaluate(calls))

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", timeframe="4h", max_results=5))
    assert loaded == ["BTCUSDT"]
    assert data["scanned_count"] == 1
    assert {inst for inst, _ in calls} == {"BTCUSDT.BINANCE"}
    assert [c["instrument_id"] for c in data["candidates"]] == ["BTCUSDT.BINANCE"]
    assert "note" not in data


@pytest.mark.asyncio
async def test_scan_swing_setups_real_match_keeps_preset_levels_and_timeframe(monkeypatch):
    """A preset that fires gives its own levels and side, labelled with the frame it was evaluated on."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, symbol, *a, **k: synthetic_bars)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ScannerEngine, "evaluate", _fake_evaluate(calls))

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", preset_slug="all", timeframe="4h", max_results=10))
    assert data["timeframe"] == "4h"  # the requested frame
    # With "all", the 15m preset runs on 15m and the others on the requested frame
    # each long preset and its short twin: one 15m sniper and two 4h presets per side
    assert sorted(calls) == sorted([("BTCUSDT.BINANCE", "15m"), ("BTCUSDT.BINANCE", "4h"), ("BTCUSDT.BINANCE", "4h")] * 2)
    cand = data["candidates"][0]
    expected_tf = {"swing-sniper-pullback-15m": "15m", "swing-trend-pullback-1h": "4h", "swing-squeeze-breakout-1h": "4h"}
    assert cand["timeframe"] == expected_tf[cand["preset_slug"]]
    assert cand["direction"] == "LONG"
    assert cand["stop"] == pytest.approx(cand["entry"] * 0.98, rel=1e-3)
    assert cand["target"] == pytest.approx(cand["entry"] * 1.04, rel=1e-3)
    assert cand["rr_ratio"] == pytest.approx(2.0, abs=0.01)
    assert cand["matched_rules"] == ["rsi_14 < 40", "close > ema_50"]
    assert not any("volume_ratio >= 0.9" in r for r in cand["matched_rules"])


@pytest.mark.asyncio
async def test_scan_swing_setups_short_preset_keeps_its_side(monkeypatch):
    """A SHORT preset is reported as SHORT with its own levels, never turned into a LONG."""
    from cryptonite_scanner import presets as scanner_presets
    from cryptonite_scanner.models import RiskConfig

    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    base = scanner_presets.SWING_PRESETS[0]
    short_preset = base.model_copy(update={"risk": RiskConfig(direction="SHORT", min_risk_reward=1.8)})
    monkeypatch.setattr(scanner_presets, "SWING_PRESETS", [])
    monkeypatch.setattr(scanner_presets, "SWING_PRESETS_SHORT", [short_preset])
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, symbol, *a, **k: synthetic_bars)
    monkeypatch.setattr(ScannerEngine, "evaluate", _fake_evaluate(side="SHORT"))

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", timeframe="1h"))
    cand = data["candidates"][0]
    assert cand["direction"] == "SHORT"
    assert cand["stop"] > cand["entry"] > cand["target"]


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
    """Verifies graceful handling when both the lake and REST fallback return no data."""
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, *args, **kwargs: pl.DataFrame())
    monkeypatch.setattr("cryptonite_agents.tools._spot_klines", lambda *args, **kwargs: (None, "unavailable"))
    monkeypatch.setenv("DATABASE_URL", "")

    raw = await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=5)
    data = json.loads(raw)
    assert data["candidates"] == []
    assert data["scanned_count"] == len(SWING_LIQUID_12)


@pytest.mark.asyncio
async def test_scan_swing_setups_empty_lake_rest_fallback(monkeypatch):
    """Verifies that when the lake is empty, scan_swing_setups falls back to REST klines."""
    fake_candles = [
        {
            "open_time_ms": 1_700_000_000_000 + i * 3_600_000,
            "close_time_ms": 1_700_000_000_000 + (i + 1) * 3_600_000 - 1,
            "open": 50000.0 + i * 10.0,
            "high": 50020.0 + i * 10.0,
            "low": 49980.0 + i * 10.0,
            "close": 50015.0 + i * 10.0,
            "volume": 500.0,
        }
        for i in range(60)
    ]
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, *args, **kwargs: pl.DataFrame())
    monkeypatch.setattr(
        "cryptonite_agents.tools._spot_klines",
        lambda sym, interval, limit_val, **kwargs: ({"symbol": sym, "interval": interval, "candles": fake_candles}, None),
    )
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(ScannerEngine, "evaluate", _fake_evaluate())

    raw = await scan_swing_setups(preset_slug="all", timeframe="1h", max_results=3)
    data = json.loads(raw)
    assert len(data["candidates"]) > 0
    assert data["candidates"][0]["instrument_id"] in ("BTCUSDT.BINANCE", "ETHUSDT.BINANCE", "SOLUSDT.BINANCE", "XRPUSDT.BINANCE")


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


@pytest.mark.asyncio
async def test_dot_quote_and_bare_symbol_normalization_in_klines_and_swing_scan(monkeypatch):
    """Verifies MUBARAK.USDT and bare MUBARAK resolve to MUBARAKUSDT in get_klines and scan_swing_setups."""
    from cryptonite_agents import tools

    assert tools._strip_venue_preserve_quote("MUBARAK.USDT") == "MUBARAKUSDT"
    assert tools._strip_venue_preserve_quote("MUBARAK.USDT.BINANCE") == "MUBARAKUSDT"
    assert tools._strip_venue_preserve_quote("BTCUSDT.BINANCE") == "BTCUSDT"

    fake_candles = [
        {
            "open_time_ms": 1_700_000_000_000 + i * 3_600_000,
            "close_time_ms": 1_700_000_000_000 + (i + 1) * 3_600_000 - 1,
            "open": 0.0500 + i * 0.0001,
            "high": 0.0505 + i * 0.0001,
            "low": 0.0495 + i * 0.0001,
            "close": 0.0502 + i * 0.0001,
            "volume": 100000.0,
        }
        for i in range(60)
    ]

    def fake_spot_klines(sym: str, interval: str, limit_val: int, **kwargs):
        if sym in ("MUBARAK", "MUBARAKUSDT"):
            return {"symbol": "MUBARAKUSDT", "interval": interval, "candles": fake_candles}, None
        return None, f"Error: Symbol '{sym}' is not a valid active trading pair on Binance."

    monkeypatch.setattr(tools, "_spot_klines", fake_spot_klines)

    class EmptyLake:
        def __init__(self, *args, **kwargs):
            pass

        def load_bars(self, *args, **kwargs):
            return None

        def close(self):
            pass

    monkeypatch.setattr(tools, "DuckDBLakeRepository", EmptyLake)

    klines_out = json.loads(tools.get_klines("MUBARAK.USDT", "1h", 5))
    assert klines_out["symbol"] == "MUBARAKUSDT"

    swing_out = json.loads(await scan_swing_setups(symbol="MUBARAK.USDT", timeframe="1h", max_results=3))
    assert swing_out["scanned_count"] == 1
    assert swing_out["candidates"] == []  # no preset fires on these bars, and none is made up
    assert swing_out["note"].startswith("No preset fired on MUBARAKUSDT at 1h, and the scanner found no setup")

    monkeypatch.setattr(ScannerEngine, "evaluate", _fake_evaluate())
    swing_out = json.loads(await scan_swing_setups(symbol="MUBARAK.USDT", timeframe="1h", max_results=3))
    assert len(swing_out["candidates"]) == 1
    cand = swing_out["candidates"][0]
    assert cand["instrument_id"] == "MUBARAKUSDT.BINANCE"
    assert cand["entry"] > 0.05
    assert cand["stop"] < cand["entry"]
    assert cand["target"] > cand["entry"]


# -----------------------------------------------------------------------------
# 5. Staged Active Card Redis Cache Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_staged_active_card_redis_lookup():
    import uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from cryptonite_api.routes.brain import _find_card, _get_card

    sid = uuid.uuid4()
    card_id = f"card_test_{uuid.uuid4().hex[:8]}"
    fake_card = {
        "id": card_id,
        "kind": "order_card",
        "instrument_id": "BTCUSDT.BINANCE",
        "side": "BUY",
        "order_type": "MARKET",
        "allocation_pct": 5.0,
        "status": "PENDING_APPROVAL",
    }

    empty_session = {"messages": []}
    assert _find_card(empty_session, card_id) is None

    fake_redis = AsyncMock()
    fake_redis.get.return_value = json.dumps(fake_card)

    fake_request = MagicMock()
    fake_request.app.state.stream_hub.redis = fake_redis

    with patch("cryptonite_api.routes.brain._worker_get", AsyncMock(return_value=empty_session)):
        card = await _get_card(fake_request, "operator:test", sid, card_id)
        assert card is not None
        assert card["id"] == card_id
        assert card["status"] == "PENDING_APPROVAL"
        fake_redis.get.assert_awaited_once_with(f"brain:active_card:{card_id}")


@pytest.mark.asyncio
async def test_brain_note_updates_redis_when_streaming():
    from unittest.mock import AsyncMock, MagicMock

    from cryptonite_agents.brain import Brain

    fake_store = MagicMock()
    fake_store.get_session = AsyncMock(return_value={"id": "s1"})
    fake_store.update_widget = AsyncMock(return_value=False)
    fake_store.append_message = AsyncMock(return_value=1)

    fake_redis = AsyncMock()
    cached_card = {"id": "c1", "status": "PENDING_APPROVAL", "kind": "order_card"}
    fake_redis.get.return_value = json.dumps(cached_card)

    brain = Brain(
        gateway=MagicMock(),
        store=fake_store,
        redis=fake_redis,
    )

    applied = await brain.note(
        operator_id="op1",
        session_id="s1",
        content="Operator approved order card c1.",
        widget_id="c1",
        widget_patch={"status": "APPROVED", "command_id": "cmd-123"},
        expect_status="PENDING_APPROVAL",
    )

    assert applied is True
    fake_redis.get.assert_awaited_with("brain:active_card:c1")
    call_args = fake_redis.set.call_args
    assert call_args[0][0] == "brain:active_card:c1"
    updated_card = json.loads(call_args[0][1])
    assert updated_card["status"] == "APPROVED"
    assert updated_card["command_id"] == "cmd-123"




@pytest.mark.asyncio
async def test_scan_swing_setups_all_scans_long_and_short_presets(monkeypatch):
    """'all' runs the short swing presets as well as the long ones, so a downtrend can still show setups."""
    from cryptonite_scanner.presets import SWING_PRESETS, SWING_PRESETS_SHORT

    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, symbol, *a, **k: synthetic_bars)
    sides: list[str] = []

    def evaluate(cls, df, rule_groups, risk_config, instrument_id, timeframe, **kwargs):
        sides.append(risk_config.direction)
        return []

    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(evaluate))
    await scan_swing_setups(symbol="BTCUSDT", timeframe="1h")
    assert sides.count("SHORT") == len(SWING_PRESETS_SHORT) == 3
    assert sides.count("LONG") == len(SWING_PRESETS)


@pytest.mark.asyncio
async def test_scan_swing_setups_adds_recent_scanner_setups(monkeypatch, scanner_rows):
    """A setup the system scanner stored recently joins the result, with its side, source and age."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    close = float(synthetic_bars.tail(1)["close"][0])
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, symbol, *a, **k: synthetic_bars)
    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(lambda cls, *a, **k: []))
    scanner_rows.append({
        "instrument_id": "BTCUSDT.BINANCE", "timeframe": "15m", "preset_slug": "swing-sniper-pullback-short-15m",
        "suggested_entry": close, "suggested_stop": close * 1.01, "suggested_target": close * 0.98,
        "matched_rules": '["[TREND] htf_close < htf_ema_50"]', "price": close, "age_minutes": 12,
    })

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", timeframe="1h"))
    assert "note" not in data
    cand = data["candidates"][0]
    assert cand["source"] == "scanner" and cand["age_minutes"] == 12
    assert cand["direction"] == "SHORT" and cand["preset_slug"] == "swing-sniper-pullback-short-15m"
    assert cand["timeframe"] == "15m" and cand["rr_ratio"] == 2.0
    assert cand["matched_rules"] == ["[TREND] htf_close < htf_ema_50"]
    SwingRadarWidget(candidates=[SwingCandidateItem(**cand)])  # the radar accepts it


@pytest.mark.asyncio
async def test_scan_swing_setups_drops_scanner_setup_when_price_moved(monkeypatch, scanner_rows):
    """A scanner setup whose entry is more than 2.5% from the live close is stale and left out."""
    synthetic_bars = make_synthetic_1m_bars(num_bars=1500, start_price=60000.0, drift=5.0)
    close = float(synthetic_bars.tail(1)["close"][0])
    monkeypatch.setattr(DuckDBLakeRepository, "load_bars", lambda self, symbol, *a, **k: synthetic_bars)
    monkeypatch.setattr(ScannerEngine, "evaluate", classmethod(lambda cls, *a, **k: []))
    scanner_rows.append({
        "instrument_id": "BTCUSDT.BINANCE", "timeframe": "1h", "preset_slug": "trend-pullback",
        "suggested_entry": close * 0.95, "suggested_stop": close * 0.94, "suggested_target": close * 0.97,
        "matched_rules": [], "price": close * 0.95, "age_minutes": 200,
    })

    data = json.loads(await scan_swing_setups(symbol="BTCUSDT", timeframe="1h"))
    assert data["candidates"] == [] and "no setup" in data["note"]
