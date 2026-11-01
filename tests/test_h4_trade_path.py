"""H4 (master plan 2026-11-01, Stage 1): regressions for the trade-path blockers, one per verified failure."""

import datetime as dt
import json
from importlib import resources

import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_api.routes.setups import _routeable
from cryptonite_scanner.brief import _setup_grade
from cryptonite_scanner.setup_grade import GradeInputs, grade_setup, volume_24h_usd
from cryptonite_schemas.profile import effective_timeframes, preset_timeframes
from test_debate_gate import load


@pytest.fixture(scope="module")
def gate():
    return load("debate_worker_gate_h4", "gate.py")


def brief(direction, trend, levels):
    entry, stop, target = levels
    data = {"candidate": {"entry": entry, "stop": stop, "target": target, "reward_to_risk": 2, "timeframe": "1h"},
            "timeframes": [{"timeframe": "4h", "trend": trend}, {"timeframe": "1h", "trend": trend}]}
    if direction is not None:
        data["direction"] = direction
    return data


LONG_LEVELS, SHORT_LEVELS = (100, 95, 110), (100, 105, 90)


# 1. The running worker rejected an aligned short (HTF down) and passed an opposing one (HTF up): the gate flipped the
# trends and hard_rejects flipped them again for direction SHORT.
@pytest.mark.parametrize("direction, levels, trend, expected", [
    ("SHORT", SHORT_LEVELS, "down", None),  # aligned short: was TRIAGE_RULE_TIMEFRAMES_AGAINST
    ("SHORT", SHORT_LEVELS, "up", "TRIAGE_RULE_TIMEFRAMES_AGAINST"),  # opposing short: was allowed
    ("LONG", LONG_LEVELS, "up", None),
    ("LONG", LONG_LEVELS, "down", "TRIAGE_RULE_TIMEFRAMES_AGAINST"),
    (None, SHORT_LEVELS, "down", None),  # no direction: valid short levels make it a short
    (None, SHORT_LEVELS, "up", "TRIAGE_RULE_TIMEFRAMES_AGAINST"),
    (None, LONG_LEVELS, "up", None),  # no direction, long levels: the LONG rules
    (None, LONG_LEVELS, "down", "TRIAGE_RULE_TIMEFRAMES_AGAINST"),
])
def test_short_trend_gate_judges_trends_once(gate, direction, levels, trend, expected):
    assert gate._rule_rejection(brief(direction, trend, levels)) == expected


# 3. A SWING profile runs swing-sniper-pullback-15m, but its timeframes were 1h/4h: 15m candidates were refused
# TIMEFRAME_NOT_IN_STYLE.
def test_a_swing_profile_runs_its_15m_preset_on_15m_only():
    assert "15m" in effective_timeframes("SWING", "LONG", [], [])
    runs = preset_timeframes("SWING", "BOTH", [], [])
    assert "15m" in runs["swing-sniper-pullback-15m"] and "15m" in runs["swing-sniper-pullback-short-15m"]
    assert runs["swing-trend-pullback-1h"] == ("1h", "4h")  # the other presets keep the style's timeframes
    assert effective_timeframes("SWING", "LONG", ["swing-trend-pullback-1h"], []) == ("1h", "4h")


# 4. Every unknown input (regime, volatility, funding, liquidity) was a weakness, so scan grades landed at C.
ALIGNED = dict(side="LONG", entry=100.0, stop=95.0, target=112.0, trend_4h="BULL", trend_1h="BULL", trend_15m="BULL",
               adx=30.0, plus_di=30.0, minus_di=10.0, atr_pct=1.0, atr_pct_q90=2.0)


def test_unknown_grade_inputs_are_neutral_and_cap_at_b():
    known = grade_setup(GradeInputs(**ALIGNED, funding_rate_8h=0.0001, spread_pct=0.01, volume_24h_usd=1e9))
    assert known.grade == "A"
    scan = grade_setup(GradeInputs(**ALIGNED))  # funding and liquidity unknown: was C
    assert scan.grade == "B"
    assert "funding, liquidity unknown: neutral, capped at B" in scan.reasons
    bare = grade_setup(GradeInputs(side="LONG", entry=100.0, stop=95.0, target=112.0, trend_4h="BULL",
                                   trend_1h="BULL", trend_15m="BULL"))
    assert bare.grade == "B"  # four unknown inputs: still not a weakness each
    thin = grade_setup(GradeInputs(**ALIGNED, funding_rate_8h=0.0, volume_24h_usd=1e6))  # spread unknown, volume known
    assert thin.grade == "C"  # the known thin volume still caps


def test_the_24h_volume_comes_from_the_stored_1h_bars():
    bars = pl.DataFrame({"close": [100.0] * 30, "volume": [30_000.0] * 30})
    assert volume_24h_usd(bars) == pytest.approx(24 * 100 * 30_000)
    assert volume_24h_usd(bars.head(10)) is None
    assert volume_24h_usd(None) is None


def test_the_scanner_brief_grade_reads_the_stored_funding():
    ctx = {k: ALIGNED[k] for k in ("trend_4h", "trend_1h", "trend_15m", "adx", "plus_di", "minus_di", "atr_pct",
                                   "atr_pct_q90")} | {"volume_24h_usd": 1e9}
    cand = {"entry": 100.0, "stop": 95.0, "target": 112.0}
    graded = _setup_grade("LONG", cand, ctx, {"funding_rate": 0.0001})
    assert graded["grade"] == "B" and not any("funding" in r for r in graded["reasons"])  # only the spread unknown
    crowded = _setup_grade("LONG", cand, ctx, {"funding_rate": 0.001})
    assert crowded["grade"] == "B" and any(r.startswith("crowded") for r in crowded["reasons"])  # A, one step down


# 6. The Brain's swing scan offered setups on coins no mandate could trade.
SCOPES = [{"name": "BTC perp", "instruments": ["BTCUSDT-PERP.BINANCE"], "markets": ["PERP"], "direction": "BOTH"},
          {"name": "ETH spot", "instruments": ["ETHUSDT.BINANCE"], "markets": ["SPOT"], "direction": "LONG"}]


def test_a_scan_setup_routes_only_to_a_mandate_side_and_market():
    route, why = tools._setup_route("BTCUSDT.BINANCE", "SHORT", SCOPES)
    assert why is None and route == {"mandate": "BTC perp", "market": "PERP", "instrument_id": "BTCUSDT-PERP.BINANCE"}
    route, why = tools._setup_route("ETHUSDT.BINANCE", "LONG", SCOPES)
    assert route["market"] == "SPOT" and route["instrument_id"] == "ETHUSDT.BINANCE"
    route, why = tools._setup_route("ETHUSDT.BINANCE", "SHORT", SCOPES)
    assert route is None and why.startswith("no ACTIVE mandate allows a short on ETHUSDT")
    route, why = tools._setup_route("SOLUSDT.BINANCE", "LONG", SCOPES)
    assert route is None and why == "no ACTIVE mandate covers SOLUSDT"


async def test_the_swing_scan_lists_unfed_or_uncovered_coins_as_not_tradeable(monkeypatch):
    class Lake:
        def __init__(self, **_):
            pass

        def load_bars(self, **_):
            raise AssertionError("an unfed or uncovered coin is never scanned")

        def close(self):
            pass

    monkeypatch.setattr(tools, "_active_scopes", lambda: SCOPES)
    monkeypatch.setattr(tools, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT", "XRPUSDT"}))
    monkeypatch.setattr(tools, "DuckDBLakeRepository", Lake)
    monkeypatch.setattr(tools, "_recent_scanner_setups", lambda *_: [])
    monkeypatch.setattr(tools, "_spot_klines", lambda *a, **k: (None, "offline"))  # H5: discovery is over REST only
    out = json.loads(await tools.scan_swing_setups(symbol="SOLUSDT"))
    assert out["candidates"] == [] and out["scanned_count"] == 0 and out["untradeable_setups"] == []
    assert out["uncovered_coins"] == [{"coin": "SOLUSDT",
                                       "reason": "SOLUSDT is not in the live feed and no ACTIVE mandate covers it"}]
    out = json.loads(await tools.scan_swing_setups(symbol="XRPUSDT"))
    assert out["uncovered_coins"] == [{"coin": "XRPUSDT", "reason": "no ACTIVE mandate covers XRPUSDT"}]


# 7. The Setups inbox showed the row's age, not the bar close's, and kept offering candidates the gate refuses.
def test_the_inbox_marks_a_stale_candidate_no_longer_routeable():
    now = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)
    close_ns = lambda minutes: int((now.timestamp() - minutes * 60) * 1e9)  # noqa: E731
    ok = {"eligible": True, "reason": None}
    assert _routeable(close_ns(20), "15m", ok, now) == {"bar_age_seconds": 1200, "routeable": True,
                                                         "routeable_reason": None}
    assert _routeable(close_ns(31), "15m", ok, now)["routeable_reason"] == "STALE_CANDIDATE"
    assert _routeable(close_ns(100), "1h", ok, now)["routeable"] is True  # two 1h bars
    refused = _routeable(close_ns(5), "1h", {"eligible": False, "reason": "NO_OWNER"}, now)
    assert (refused["routeable"], refused["routeable_reason"]) == (False, "NO_OWNER")


# 8. The installed preset numbers are the cost-aware walk-forward of 1 Nov.
def test_the_installed_preset_replay_is_the_cost_aware_walk_forward():
    doc = json.loads(resources.files("cryptonite_scanner").joinpath("data/preset_replay.json").read_text())
    assert doc["assumptions"]["cost_model"] == "spot-v1 for LONG, perp-v1 for SHORT"
    assert doc["assumptions"]["window"]["test_start"].startswith("2026-08-26")
