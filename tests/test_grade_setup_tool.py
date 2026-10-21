"""Stage 3.2 (leverage plan): the Brain tool ``grade_setup`` and the grade on every ``scan_swing_setups`` candidate.
Every Binance call is stubbed: spot klines, the USD-M answer and the futures tickers."""

import json

import cryptonite_agents.tools as tools
import pytest
from cryptonite_agents.brain.toolset import BRAIN_TOOLS
from cryptonite_agents.brain.widgets import SwingCandidateItem


def candles(n, start=100.0, step=0.4, wobble=0.6):
    """A steady uptrend with some noise, 1h apart."""
    out = []
    px = start
    for i in range(n):
        o = px
        c = px + step + (wobble if i % 3 == 0 else -wobble / 2)
        out.append({"open_time_ms": i * 3_600_000, "close_time_ms": (i + 1) * 3_600_000 - 1, "open": o,
                    "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": c, "volume": 1000.0})
        px = c
    return out


@pytest.fixture
def binance(monkeypatch):
    calls = []

    def klines(symbol, interval, limit, **kw):
        calls.append(("klines", symbol, interval))
        return {"candles": candles(limit)}, None

    def usdm(symbol):
        calls.append(("usdm", symbol))
        return {"mark": {"funding_rate": 0.0001}, "funding": [
            {"funding_time": "2026-10-20T08:00:00+00:00"}, {"funding_time": "2026-10-20T00:00:00+00:00"}]}

    def fapi(path, params):
        calls.append(("fapi", path, params.get("symbol")))
        if path == "/futures/data/openInterestHist":
            return [{"sumOpenInterest": "1000"}, {"sumOpenInterest": "1050"}]
        if path == "/fapi/v1/ticker/bookTicker":
            return {"bidPrice": "219.99", "askPrice": "220.01"}
        if path == "/fapi/v1/ticker/24hr":
            return {"quoteVolume": "800000000"}
        raise AssertionError(path)

    monkeypatch.setattr(tools, "_spot_klines", klines)
    monkeypatch.setattr(tools, "_usdm_cached", usdm)
    monkeypatch.setattr(tools, "_fapi_get", fapi)
    return calls


def test_grade_setup_reads_every_input_and_returns_the_grade(binance):
    out = json.loads(tools.grade_setup("SOLUSDT", "BUY", 220.0, 214.0, 235.0))
    assert out["symbol"] == "SOLUSDT" and out["side"] == "BUY" and out["version"] == "setup-grade-v1"
    assert out["grade"] in ("A", "B", "C", "NO_TRADE")
    assert out["rr_ratio"] == 2.5
    inputs = out["inputs"]
    assert inputs["trend_4h"] == inputs["trend_1h"] == inputs["trend_15m"] == "BULL"
    assert inputs["funding_rate_8h"] == pytest.approx(0.0001)
    assert inputs["oi_change_24h_pct"] == pytest.approx(5.0)
    assert inputs["spread_pct"] == pytest.approx(0.02 / 220.0 * 100)
    assert inputs["volume_24h_usd"] == 800_000_000
    assert inputs["adx"] is not None and inputs["atr_pct_q90"] is not None
    assert out["default_leverage_ceiling"] == {"A": 10, "B": 5, "C": 2, "NO_TRADE": 0}[out["grade"]]
    # deterministic: the same inputs give the same answer
    again = json.loads(tools.grade_setup("SOLUSDT", "BUY", 220.0, 214.0, 235.0))
    assert (again["grade"], again["reasons"]) == (out["grade"], out["reasons"])


def test_a_4h_funding_interval_is_scaled_to_8h(monkeypatch, binance):
    monkeypatch.setattr(tools, "_usdm_cached", lambda s: {"mark": {"funding_rate": 0.0001}, "funding": [
        {"funding_time": "2026-10-20T08:00:00+00:00"}, {"funding_time": "2026-10-20T04:00:00+00:00"}]})
    out = json.loads(tools.grade_setup("SOLUSDT", "BUY", 220.0, 214.0, 235.0))
    assert out["inputs"]["funding_rate_8h"] == pytest.approx(0.0002)


def test_unreadable_inputs_are_weak_never_an_error(monkeypatch):
    monkeypatch.setattr(tools, "_spot_klines", lambda *a, **k: (None, "Error: unavailable"))
    monkeypatch.setattr(tools, "_usdm_cached", lambda s: (_ for _ in ()).throw(RuntimeError("down")))

    def fapi(path, params):
        raise RuntimeError("down")

    monkeypatch.setattr(tools, "_fapi_get", fapi)
    out = json.loads(tools.grade_setup("SOLUSDT", "SELL", 220.0, 226.0, 205.0))
    assert out["grade"] == "C"  # every unknown input is a weak one: never A or B on no data
    assert "funding unknown" in out["reasons"] and "liquidity unknown" in out["reasons"]


def test_invalid_input_is_refused():
    assert "error" in json.loads(tools.grade_setup("bad symbol!", "BUY", 1, 0.5, 2))
    assert "error" in json.loads(tools.grade_setup("SOLUSDT", "BUY", "x", 0.5, 2))


def test_grade_setup_is_a_brain_tool():
    assert tools.GRADE_SETUP in BRAIN_TOOLS


def test_scan_grade_is_computed_from_the_scan_frames_and_never_above_b():
    from cryptonite_scanner.engine import ScannerEngine

    frames = {tf: tools._spot_candles_to_enriched_df(candles(n), ScannerEngine) for tf, n in (("4h", 120), ("1h", 300), ("15m", 120))}
    out = tools._scan_grade(frames, "LONG", 220.0, 214.0, 235.0)
    assert out["setup_grade"] in ("B", "C", "NO_TRADE")  # funding and liquidity are not read by a scan
    assert any("funding unknown" in r for r in out["grade_reasons"])
    item = SwingCandidateItem(instrument_id="SOLUSDT.BINANCE", preset_slug="p", timeframe="1h", score=80, entry=220,
                              stop=214, target=235, rr_ratio=2.5, **out)
    assert item.setup_grade == out["setup_grade"]
