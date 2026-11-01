"""Rule triage (remediation R3.1): ``brief_data`` / ``hard_rejects`` in ``cryptonite_scanner.brief``.

The brief TEXT is pinned by a snapshot taken from the code before R3.1 (the certified triage model and
the eval sets read it); the structured twin carries the same numbers; the rules fire only when every
input they need is present.
"""

import json
from datetime import UTC, datetime

import polars as pl
import pytest
from cryptonite_scanner.brief import brief_data, build_brief, hard_rejects, timeframe_seconds


def frame(last_close, step, e20, e50, e200, vol, upper, lower, rsi=55.0, macd=1.0):
    closes = [last_close - step * (49 - i) for i in range(50)]
    n = 49
    return pl.DataFrame({
        "close": closes, "high": [c * 1.002 for c in closes], "low": [c * 0.998 for c in closes],
        "ema_20": [None] * n + [e20], "ema_50": [None] * n + [e50], "ema_200": [None] * n + [e200],
        "rsi": [None] * n + [rsi], "macd_hist": [None] * n + [macd], "bb_upper": [None] * n + [upper],
        "bb_lower": [None] * n + [lower], "atr": [None] * n + [last_close * 0.003],
        "volume_ratio": [None] * n + [vol],
    })


def frames():
    return {
        "15m": frame(65000.0, 10.0, 64900.0, 64800.0, 64000.0, 1.42, 65400.0, 64500.0),
        "1h": frame(65010.0, -20.0, 65100.0, 65300.0, 66000.0, 0.73, 65500.0, 64800.0, rsi=41.5, macd=-2.0),
    }


NOW = datetime(2026, 9, 15, 12, 7, tzinfo=UTC)
CLOSE_NS = int(datetime(2026, 9, 15, 12, 0, tzinfo=UTC).timestamp() * 1e9)
CANDIDATE = {"suggested_entry": "65000", "suggested_stop": "64700", "suggested_target": "65750",
             "timeframe": "15m", "matched_rules": ["ema_cross", "volume_spike"]}
DERIV = {"mark_price": 65012.5, "funding_rate": 0.0001, "open_interest": 81234.0}

# Rendered by the pre-R3.1 build_brief (scratch copy of the old module) on the inputs above
SNAPSHOT_FULL = (
    "MARKET BRIEF BTCUSDT.BINANCE (as of 2026-09-15 12:07 UTC)\n"
    "Data freshness: last closed bar 7 min ago\n"
    "15m: close 65,000.00, 0.76% over last 50 bars, trend up, RSI 55.0, MACD hist +, inside bands, "
    "ATR 0.30% of price, volume 1.42x avg, range 64,380.98-65,130.00\n"
    "1h: close 65,010.00, -1.49% over last 50 bars, trend down, RSI 41.5, MACD hist -, inside bands, "
    "ATR 0.30% of price, volume 0.73x avg, range 64,879.98-66,121.98\n"
    "Scanner candidate (15m): entry 65,000.00, stop 64,700.00, target 65,750.00, reward-to-risk 2.50; "
    "rules: ema_cross, volume_spike\n"
    "Direction: LONG · Setup grade: C (1 of 3 timeframes aligned; reward 2.5x the risk; regime, volatility, liquidity "
    "unknown: neutral, capped at B)\n"  # H4: unknown inputs are neutral; the brief reads the perp funding

    "Perp context: mark 65,012.50, funding 0.0100% per interval, open interest 81,234"
)
SNAPSHOT_BARE = (
    "MARKET BRIEF ETHUSDT.BINANCE (as of 2026-09-15 12:07 UTC)\n"
    "15m: close 65,000.00, 0.76% over last 50 bars, trend up, RSI 55.0, MACD hist +, inside bands, "
    "ATR 0.30% of price, volume 1.42x avg, range 64,380.98-65,130.00"
)


def test_build_brief_text_is_unchanged():
    assert build_brief("BTCUSDT.BINANCE", frames(), candidate=CANDIDATE, derivatives=DERIV,
                       last_bar_close_ns=CLOSE_NS, now=NOW) == SNAPSHOT_FULL
    assert build_brief("ETHUSDT.BINANCE", {"15m": frames()["15m"]}, now=NOW) == SNAPSHOT_BARE


def test_brief_data_is_the_structured_twin():
    data = brief_data("BTCUSDT.BINANCE", frames(), candidate=CANDIDATE, derivatives=DERIV,
                      last_bar_close_ns=CLOSE_NS, now=NOW)
    assert json.loads(json.dumps(data)) == data  # stored in Redis as JSON
    assert [s["timeframe"] for s in data["timeframes"]] == ["15m", "1h"]
    m15, h1 = data["timeframes"]
    assert (m15["trend"], m15["bands"], m15["volume_ratio"]) == ("up", "inside", 1.42)
    assert (h1["trend"], h1["bands"], h1["volume_ratio"]) == ("down", "inside", 0.73)
    assert data["candidate"]["reward_to_risk"] == pytest.approx(2.5)
    assert data["candidate"]["timeframe"] == "15m"
    assert data["derivatives"]["funding_rate"] == pytest.approx(0.0001)
    assert hard_rejects(data) == []


def state(tf, trend="up", volume=1.2, bands="above"):
    return {"timeframe": tf, "trend": trend, "volume_ratio": volume, "bands": bands}


def data(rr=2.5, states=None, candidate_tf="15m"):
    return {"timeframes": states if states is not None else [state("5m"), state("15m"), state("1h")],
            "candidate": {"timeframe": candidate_tf, "reward_to_risk": rr}}


@pytest.mark.parametrize("d, hits", [
    (data(rr=1.49), ["REWARD_TO_RISK"]),
    (data(rr=-0.5), ["REWARD_TO_RISK"]),  # stop on the wrong side
    (data(rr=1.5), []),
    (data(states=[state("5m", "down"), state("15m", "down"), state("1h", "down")]), ["TIMEFRAMES_AGAINST"]),
    # only the two HIGHEST timeframes count, whatever their order in the brief
    (data(states=[state("1h", "down"), state("1m", "up"), state("4h", "down"), state("15m", "up")]), ["TIMEFRAMES_AGAINST"]),
    (data(states=[state("5m", "down"), state("15m", "down"), state("1h", "mixed")]), []),
    (data(states=[state("5m"), state("15m", volume=0.8, bands="inside"), state("1h")]), ["CHOP"]),
    (data(states=[state("5m"), state("15m", volume=0.81, bands="inside"), state("1h")]), []),
    (data(states=[state("5m"), state("15m", volume=0.5, bands="above"), state("1h")]), []),
    (data(rr=1.0, states=[state("15m", "down", 0.5, "inside"), state("1h", "down")]),
     ["REWARD_TO_RISK", "TIMEFRAMES_AGAINST", "CHOP"]),
])
def test_hard_rejects(d, hits):
    assert hard_rejects(d) == hits


@pytest.mark.parametrize("d", [
    None,
    {},
    {"timeframes": "garbage", "candidate": 3},
    data(rr=None),  # no entry/stop/target: no reward-to-risk rule
    data(states=[state("15m", "down")]),  # one timeframe: no "two highest"
    data(states=[state("15m", "unknown"), state("1h", "down")]),  # EMAs missing on one of them
    data(states=[state("15m", "down"), state("weekly", "down")]),  # unparseable timeframe
    data(states=[state("15m", volume=None, bands="inside"), state("1h")]),  # no volume
    data(states=[state("15m", volume=0.5, bands=None), state("1h")]),  # no bands
    data(states=[state("5m", volume=0.5, bands="inside"), state("1h")]),  # candidate timeframe not shown
    {"timeframes": [state("15m", volume=0.5, bands="inside")], "candidate": None},
])
def test_missing_inputs_never_reject(d):
    assert hard_rejects(d) == []


def test_timeframe_seconds():
    assert [timeframe_seconds(t) for t in ("1m", "15m", "1h", "4h", "1d", "1w", "x", None)] == [
        60, 900, 3600, 14400, 86400, 604800, None, None]


def test_eval_set_labels_use_the_shared_rules():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_triage_eval_set.py"
    spec = importlib.util.spec_from_file_location("build_triage_eval_set", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.hard_rejects is hard_rejects
    assert mod.label(1.0, 5, "up", "up", 1.2, "above") == (False, "reward_to_risk_below_1.5")
    assert mod.label(2.4, 5, "down", "down", 1.2, "above") == (False, "both_timeframes_against")
    assert mod.label(2.4, 5, "up", "mixed", 0.6, "inside") == (False, "chop")
    assert mod.label(2.4, 45, "up", "up", 1.2, "above") == (False, "stale_data")
    assert mod.label(1.0, 45, "up", "up", 1.2, "above") == (False, "reward_to_risk_below_1.5+stale_data")
    assert mod.label(2.4, 5, "up", "up", 1.2, "above") == (True, "clean_setup")
    assert mod.label(2.4, 5, "up", "up", 0.9, "inside") == (None, "ambiguous")
