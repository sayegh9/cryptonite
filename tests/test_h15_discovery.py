"""H15 PR A: more setups found, on real rules. Nothing here can create an order."""

import asyncio
import json
import time

import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_agents.brain.widgets import widget_for_tool
from cryptonite_scanner import brief as brief_mod
from cryptonite_scanner.broad import LOOKBACK_MINUTES, CoinScan, closed_frames, scan_coin
from cryptonite_scanner.conditional import capped, conditional_item, expires_at, required_bars
from cryptonite_scanner.models import RuleCondition, RuleGroup, RuleOperator
from cryptonite_scanner.presets import preset_by_slug
from cryptonite_scanner.setup_grade import funding_carry, settlement_interval_hours
from cryptonite_schemas.profile import PRESET_CATALOG

MIN_NS = 60_000_000_000
HOUR_NS = 60 * MIN_NS
SCOPES = [{"name": "both", "instruments": ["WLDUSDT.BINANCE"], "markets": ["PERP"], "direction": "BOTH", "max_leverage": 2}]


def bars_1m(minutes: int, now_ns: int, *, start: float = 100.0, step: float = 0.0) -> pl.DataFrame:
    end = now_ns - now_ns % MIN_NS
    opens = [end - (minutes - i) * MIN_NS for i in range(minutes)]
    closes = [start + step * i + ((i // 7) % 5 - 2) * 0.01 for i in range(minutes)]
    return pl.DataFrame({
        "ts_event_ns": [t + MIN_NS for t in opens], "open": closes, "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes], "close": closes, "volume": [10.0] * minutes,
    })


def cond(field, op, value, lookback=0):
    return RuleCondition(field=field, operator=op, value=value, lookback=lookback)


def frame(**columns):
    return pl.DataFrame({k: v if isinstance(v, list) else [v, v] for k, v in columns.items()})


TREND = RuleGroup(category="trend", logic="AND", conditions=[cond("close", RuleOperator.GT, "ema_200")])


# --- A3 watchlist -----------------------------------------------------------------------------------------------------

def test_a_cross_one_bar_away_is_conditional_with_its_trigger_and_no_levels():
    rules = [TREND, RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.CROSS_ABOVE, 40.0, 1)]),
             RuleGroup(category="confirmation", logic="AND", conditions=[cond("volume_ratio", RuleOperator.GTE, 0.9)])]
    item = conditional_item(frame(close=10.0, ema_200=9.0, rsi=[38.0, 39.0], volume_ratio=0.5), rules)
    assert item["trigger"]["field"] == "rsi" and item["trigger"]["field_value"] == 39.0
    assert item["also_required"][0]["field"] == "volume_ratio"
    assert item["invalidation"][0]["compared_to"] == "ema_200"
    assert not {"entry", "stop", "target"} & set(item)


def test_a_disarmed_cross_gives_no_item():
    rules = [TREND, RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.CROSS_ABOVE, 40.0, 1)])]
    assert conditional_item(frame(close=10.0, ema_200=9.0, rsi=[45.0, 46.0]), rules) is None


def test_a_level_trigger_is_conditional():
    rules = [TREND, RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.GT, 50.0)])]
    assert conditional_item(frame(close=10.0, ema_200=9.0, rsi=40.0), rules)["trigger"]["compared_value"] == 50.0


def test_two_unmet_triggers_or_a_broken_trend_or_a_fired_trigger_give_no_item():
    two = RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.GT, 50.0), cond("macd", RuleOperator.GT, 1.0)])
    assert conditional_item(frame(close=10.0, ema_200=9.0, rsi=40.0, macd=0.0), [TREND, two]) is None
    one = RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.GT, 50.0)])
    assert conditional_item(frame(close=8.0, ema_200=9.0, rsi=40.0), [TREND, one]) is None
    assert conditional_item(frame(close=10.0, ema_200=9.0, rsi=60.0), [TREND, one]) is None


def test_a_preset_without_a_trend_group_gives_no_item():
    only_trigger = [RuleGroup(category="trigger", logic="AND", conditions=[cond("rsi", RuleOperator.GT, 50.0)])]
    assert conditional_item(frame(close=10.0, rsi=40.0), only_trigger) is None


def test_expiry_is_four_bars_after_the_close():
    assert expires_at(0, 900 * 10**9) == "1970-01-01T01:00:00+00:00"


def test_the_watchlist_is_capped_in_a_stable_order():
    items = [{"instrument_id": "X", "direction": "LONG", "timeframe": tf, "preset_slug": s}
             for tf in ("4h", "1h", "15m") for s in ("b", "a")]
    assert [(i["timeframe"], i["preset_slug"]) for i in capped(items)] == [("15m", "a"), ("15m", "b"), ("1h", "a")]


# --- A2 broad scan ----------------------------------------------------------------------------------------------------

def test_warm_up_reads_the_preset_and_its_parent():
    assert required_bars(preset_by_slug("trend-breakout-4h").rules) == (200, 0)
    assert required_bars(preset_by_slug("swing-trend-pullback-1h").rules)[1] == 50


def test_too_little_history_reports_unavailable_and_never_a_verdict():
    now = time.time_ns()
    scan = scan_coin("WLDUSDT.BINANCE", bars_1m(3 * 1440, now), now)  # REST's window is about this short
    assert {u["reason"] for u in scan.unavailable} == {"UNAVAILABLE: INSUFFICIENT_WARMUP"}
    blocked = {(u["preset_slug"], u["timeframe"]) for u in scan.unavailable}
    assert not blocked & {(m["preset_slug"], m["timeframe"]) for m in scan.matches + scan.conditional}
    four_hour = {u["preset_slug"] for u in scan.unavailable if u["timeframe"] == "4h"}
    assert {"trend-breakout-4h", "trend-breakdown-short-4h"} <= four_hour


def test_full_history_evaluates_every_catalog_scope_including_4h():
    now = time.time_ns()
    scan = scan_coin("WLDUSDT.BINANCE", bars_1m(LOOKBACK_MINUTES, now), now)
    judged = {(r["preset_slug"], r["timeframe"]) for r in scan.rejections} | {
        (m["preset_slug"], m["timeframe"]) for m in scan.matches}
    wanted = {(slug, tf) for slug, meta in PRESET_CATALOG.items() for tf in meta.timeframes}
    assert judged == wanted and not scan.unavailable


def test_a_stale_feed_is_unavailable():
    now = time.time_ns()
    scan = scan_coin("WLDUSDT.BINANCE", bars_1m(LOOKBACK_MINUTES, now - 3 * 24 * HOUR_NS), now)
    assert {u["reason"] for u in scan.unavailable if u["timeframe"] == "15m"} == {"UNAVAILABLE: STALE_DATA"}


def test_a_bar_still_open_is_never_read():
    now = (time.time_ns() // (4 * HOUR_NS)) * (4 * HOUR_NS) + 90 * MIN_NS  # mid 4h bar, mid 1h bar
    frames = closed_frames(bars_1m(2000, now), now)
    for tf, df in frames.items():
        assert int(df["interval_end_exclusive_ns"].max()) <= now, tf


class NoLake:
    def __init__(self, **_):
        pass

    def load_bars(self, **_):
        return pl.DataFrame({"ts_event_ns": [1]})

    def close(self):
        pass


@pytest.fixture
def broad(monkeypatch):
    """The broad scan with its data stubbed. Any gateway or order call would have nothing to reach."""
    monkeypatch.setattr(tools, "_active_scopes", lambda: SCOPES)
    monkeypatch.setattr(tools, "_fed_symbols", lambda: {"WLDUSDT"})
    monkeypatch.setattr(tools, "DuckDBLakeRepository", NoLake)
    monkeypatch.setattr(tools, "_recent_scanner_setups", lambda *_: [])
    monkeypatch.setattr(tools, "_stored_funding_8h", lambda *_: None)
    monkeypatch.setattr(tools, "annotate_with_debates", lambda *_: None)
    monkeypatch.setattr(tools, "_monitored_scopes", lambda: {("WLDUSDT.BINANCE", "trend-pullback", "1h")})

    def run(scan: CoinScan, grade="B", reasons=()):
        monkeypatch.setattr(tools, "_scan_grade", lambda *_: {"setup_grade": grade, "grade_reasons": list(reasons)})
        monkeypatch.setattr("cryptonite_scanner.broad.scan_coin", lambda *_: scan)
        return json.loads(asyncio.run(tools.scan_swing_setups(preset_slug="broad")))
    return run


def match(slug, tf, side, entry=100.0, stop=None, target=None):
    stop = stop if stop is not None else (98.0 if side == "LONG" else 102.0)
    target = target if target is not None else (104.0 if side == "LONG" else 96.0)
    return {"instrument_id": "WLDUSDT.BINANCE", "preset_slug": slug, "timeframe": tf, "direction": side, "entry": entry,
            "stop": stop, "target": target, "rr_ratio": round(abs(target - entry) / abs(entry - stop), 2),
            "matched_rules": [], "source": "live_scan", "bar_close": "2026-11-05T11:15:00+00:00"}


def test_broad_keeps_both_sides_and_both_timeframes_apart(broad):
    out = broad(CoinScan(matches=[match("trend-pullback", "1h", "LONG"), match("trend-pullback-short", "1h", "SHORT"),
                                  match("trend-breakdown-short-4h", "4h", "SHORT")]))
    keys = {(c["direction"], c["timeframe"]) for c in out["candidates"]}
    assert keys == {("LONG", "1h"), ("SHORT", "1h"), ("SHORT", "4h")}
    assert all(c["status"] == "CANDIDATE" for c in out["candidates"])


def test_several_presets_on_one_key_are_all_listed(broad):
    out = broad(CoinScan(matches=[match("trend-pullback-short", "1h", "SHORT"), match("swing-trend-pullback-short-1h", "1h", "SHORT", entry=100.5)]))
    assert len(out["candidates"]) == 1
    assert {p["preset_slug"] for p in out["candidates"][0]["contributing_presets"]} == {
        "trend-pullback-short", "swing-trend-pullback-short-1h"}


def test_a_no_trade_grade_is_rejected_with_its_reason_and_is_no_candidate(broad):
    out = broad(CoinScan(matches=[match("trend-pullback-short", "1h", "SHORT")]), grade="NO_TRADE", reasons=["4h and 1h oppose"])
    assert out["candidates"] == []
    assert out["untradeable_setups"][0]["status"] == "REJECTED"
    assert "4h and 1h oppose" in out["untradeable_setups"][0]["reason"]


def test_a_failed_scope_makes_the_result_partial(broad):
    scan = CoinScan(unavailable=[{"instrument_id": "WLDUSDT.BINANCE", "preset_slug": "trend-breakout-4h", "timeframe": "4h",
                                  "status": "UNAVAILABLE", "reason": "UNAVAILABLE: INSUFFICIENT_WARMUP", "detail": "1 of 200"}],
                    matches=[match("trend-pullback", "1h", "LONG")])
    out = broad(scan)
    assert out["partial"] is True and "partial" in out["scope_note"] and out["candidates"]


def test_an_unowned_coin_is_research_only_and_a_route_uses_the_profiles_leverage(monkeypatch):
    assert tools._setup_route("WLDUSDT.BINANCE", "LONG", [{**SCOPES[0], "markets": ["SPOT"], "max_leverage": 2}])[0] is None
    assert tools._setup_route("WLDUSDT.BINANCE", "LONG", [{**SCOPES[0], "markets": ["SPOT"], "max_leverage": 1}])[0]["market"] == "SPOT"


def test_watchlist_items_say_whether_a_profile_monitors_them_and_reach_the_widget(broad):
    item = {"instrument_id": "WLDUSDT.BINANCE", "direction": "LONG", "timeframe": "1h", "preset_slug": "trend-pullback",
            "preset_version": "abc", "status": "CONDITIONAL", "as_of": "t", "bar_close": "t", "expires_at": "t",
            "trigger": {"field": "rsi"}, "also_required": [], "invalidation": []}
    out = broad(CoinScan(conditional=[item, {**item, "preset_slug": "macd-momentum-shift"}]))
    assert {i["preset_slug"]: i["monitored"] for i in out["conditional"]} == {"trend-pullback": True, "macd-momentum-shift": False}
    widget = widget_for_tool("scan_swing_setups", {"preset_slug": "broad"}, content=json.dumps(out), call_id="c1")
    assert len(widget["conditional"]) == 2 and widget["preset_slug"] == "broad"


def test_the_radar_widget_keeps_unavailable_scopes():
    data = {"preset_slug": "broad", "timeframe": "15m,1h,4h", "candidates": [], "partial": True,
            "unavailable": [{"instrument_id": "X.BINANCE", "preset_slug": "*", "timeframe": "*", "status": "UNAVAILABLE",
                             "reason": "UNAVAILABLE: INSUFFICIENT_WARMUP", "detail": "not fed"}]}
    widget = widget_for_tool("scan_swing_setups", {}, content=json.dumps(data), call_id="c1")
    assert widget["partial"] is True and widget["unavailable"][0]["detail"] == "not fed"


# --- A5 funding interval -----------------------------------------------------------------------------------------------

def _grade(rate, interval):
    cand = {"entry": 100.0, "stop": 98.0, "target": 104.0}
    return brief_mod._setup_grade("LONG", cand, {"trend_4h": "BULL", "trend_1h": "BULL", "trend_15m": "BULL"},
                                  {"funding_rate": rate, "funding_interval_hours": interval})


def test_a_4h_rate_is_doubled_and_an_unknown_interval_reads_as_8h():
    crowded = "crowded (funding against the trade or open interest falling): one step down"
    assert crowded in _grade(0.0002, 4.0)["reasons"]
    assert crowded not in _grade(0.0002, None)["reasons"]


def test_the_settlement_interval_comes_from_the_stored_value_else_the_gap():
    assert settlement_interval_hours([{"funding_interval_hours": 4, "funding_time_ns": 9}]) == 4.0
    gap = [{"funding_interval_hours": None, "funding_time_ns": 8 * HOUR_NS}, {"funding_interval_hours": None, "funding_time_ns": 4 * HOUR_NS}]
    assert settlement_interval_hours(gap) == 4.0
    assert settlement_interval_hours([]) is None


# --- A6 funding comparison ---------------------------------------------------------------------------------------------

def test_carry_is_adverse_only_and_in_r():
    paid = funding_carry("SHORT", 100.0, 102.0, -0.0001, 8.0, "1h")  # 9 settlements over 72 h, 2 units of risk
    assert paid["settlements"] == 9 and paid["carry_r"] == pytest.approx(0.045) and paid["crowded"] is False
    assert funding_carry("LONG", 100.0, 98.0, -0.0001, 8.0, "1h")["carry_r"] == 0.0
    assert funding_carry("LONG", 100.0, 99.0, 0.0005, 4.0, "4h")["crowded"] is True


@pytest.mark.parametrize(("rate", "interval", "tf"), [(None, 8.0, "1h"), (0.0001, None, "1h"), (0.0001, 8.0, "1d")])
def test_missing_carry_data_is_unknown(rate, interval, tf):
    assert funding_carry("LONG", 100.0, 98.0, rate, interval, tf)["carry_status"] == "UNKNOWN"


def test_the_comparison_keeps_both_grades_and_no_alternative_when_unknown():
    cand = {"entry": 100.0, "stop": 98.0, "target": 104.0}
    data = {"direction": "LONG", "candidate": cand, "grade_context": {"trend_4h": "BULL", "trend_1h": "BULL", "trend_15m": "BULL"},
            "setup_grade": {"grade": "B"}, "derivatives": {"funding_rate": 0.0005, "funding_interval_hours": 8.0}}
    known = brief_mod.funding_comparison(data, "1h")
    assert known["version"] == "funding-carry-v1" and known["actual_grade"] == "B" and known["alternative_grade"] in ("A", "B", "C")
    unknown = brief_mod.funding_comparison({**data, "derivatives": {}}, "1h")
    assert unknown["carry_status"] == "UNKNOWN" and unknown["alternative_grade"] is None
    assert brief_mod.funding_comparison({**data, "setup_grade": None}, "1h") is None


def test_brain_is_told_never_to_propose_from_a_watchlist_item():
    from cryptonite_agents.brain import agent

    source = open(agent.__file__).read()
    assert "NEVER propose an order from it" in source
    assert '"broad"' in source


def test_monitored_scopes_read_the_profile_picks_view(monkeypatch):
    rows = [{"instruments": ["WLDUSDT.BINANCE", "WLDUSDT-PERP.BINANCE"], "direction": "BOTH", "style": "SWING",
             "strategies": ["trend-breakdown-short-4h"], "timeframes": ["4h"]}]
    seen = []
    monkeypatch.setattr(tools, "_readonly_rows", lambda sql, *_: seen.append(sql) or rows)
    assert ("WLDUSDT.BINANCE", "trend-breakdown-short-4h", "4h") in tools._monitored_scopes()
    assert "agent_safe_profile_picks" in seen[0]
