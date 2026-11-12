"""The Brain's ranked trade ideas: always an answer, levels from closed bars only, the idea kept apart from whether it
can be executed now.

No model, no database and no network: bars, the feed universe and the profiles are injected.
"""

import asyncio
import datetime
import json

import pytest
from cryptonite_agents import trade_ideas as ti
from cryptonite_agents.brain.toolset import BRAIN_TOOLS
from cryptonite_agents.brain.widgets import describe_widget, widget_for_tool
from cryptonite_agents.gateway import RiskClass

T0 = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)


def bars_from_closes(closes: list[float], wick: float = 0.2) -> list[dict]:
    """Closed bars in the shape ``recent_bars`` returns, one hour apart, ending at T0."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        t = T0 - datetime.timedelta(hours=len(closes) - 1 - i)
        out.append({"close_time": t.isoformat(timespec="seconds"), "open": prev, "high": max(prev, c) + wick,
                    "low": min(prev, c) - wick, "close": c, "volume": 100.0})
        prev = c
    return out


def payload(closes: list[float], *, age: float = 5.0, missing: int = 0, timeframe: str = "1h") -> dict:
    bars = bars_from_closes(closes)
    return {"instrument_id": "BTCUSDT.BINANCE", "timeframe": timeframe, "expected": len(bars) + missing,
            "returned": len(bars), "missing": missing, "complete": missing == 0,
            "newest_bar_close": bars[-1]["close_time"], "newest_bar_age_minutes": age, "bars": bars}


UP = [100 + i * 0.5 for i in range(60)]
DOWN = [130 - i * 0.5 for i in range(60)]
RANGE_LOW = [100 + (i % 10) for i in range(55)] + [104, 103, 102, 101, 100.5]  # ends near the range low
RANGE_HIGH = [100 + (i % 10) for i in range(55)] + [106, 107, 108, 108.5, 109]  # ends near the range high


def active(name="BTC Perps", coins=("BTCUSDT.BINANCE",), markets=("PERP",), direction="BOTH", **extra) -> dict:
    return {"name": name, "status": "ACTIVE", "paused_reason": None, "halted_reason": None,
            "instruments": list(coins), "markets": list(markets), "direction": direction, "max_leverage": 2, **extra}


# ---- the pure analysis ------------------------------------------------------------------------------------------


def test_uptrend_gives_a_long_with_levels_from_the_bars():
    ideas = ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(UP))
    longs = [i for i in ideas if i["direction"] == "LONG"]
    assert longs, ideas
    idea = longs[0]
    lows = [b["low"] for b in bars_from_closes(UP)]
    assert idea["stop"] < idea["entry"] < idea["target"]
    assert idea["stop"] < min(lows[-ti.SWING_BARS:])  # the stop sits under the recent swing low, not a fixed percent
    assert idea["rr_ratio"] == pytest.approx((idea["target"] - idea["entry"]) / (idea["entry"] - idea["stop"]), abs=0.01)
    assert idea["bar_close"] == T0.isoformat(timespec="seconds")
    assert idea["basis"] and idea["invalidation"]


def test_downtrend_gives_a_short():
    ideas = ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(DOWN))
    shorts = [i for i in ideas if i["direction"] == "SHORT"]
    assert shorts and shorts[0]["target"] < shorts[0]["entry"] < shorts[0]["stop"]


def test_range_gives_the_side_nearest_its_edge():
    low = ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(RANGE_LOW))
    high = ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(RANGE_HIGH))
    assert [i["direction"] for i in low if i["kind"] == "range_reversion"] == ["LONG"]
    assert [i["direction"] for i in high if i["kind"] == "range_reversion"] == ["SHORT"]
    long_idea = next(i for i in low if i["kind"] == "range_reversion")
    assert long_idea["stop"] < min(b["low"] for b in bars_from_closes(RANGE_LOW)[-ti.RANGE_BARS:])


def test_too_few_bars_gives_no_idea_and_never_invents_levels():
    assert ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(UP[:10])) == []
    assert ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", {"bars": [], "reason": "lake bars could not be read"}) == []


def test_every_readable_frame_gives_at_least_one_idea():
    mid = [100 + (i % 10) for i in range(55)] + [104, 105, 104.5, 105, 104.5]  # mid-range: only break triggers
    for closes in (UP, DOWN, RANGE_LOW, RANGE_HIGH, mid):
        ideas = ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(closes))
        assert ideas
        for i in ideas:
            assert i["entry"] > 0 and i["stop"] > 0 and i["target"] > 0 and i["rr_ratio"] > 0


# ---- can it be executed now -------------------------------------------------------------------------------------


def idea(direction="LONG", rr=2.0, age=5.0, complete=True, entry_type="AT_MARKET") -> dict:
    return {"instrument_id": "BTCUSDT.BINANCE", "coin": "BTCUSDT", "timeframe": "1h", "direction": direction,
            "rr_ratio": rr, "newest_bar_age_minutes": age, "data_complete": complete, "entry_type": entry_type}


def test_active_profile_makes_an_idea_potential_never_executable():
    s = ti.executability(idea(), [active()])
    assert s["status"] == "POTENTIAL" and s["card_possible"] and s["route"]["mandate"] == "BTC Perps" and s["reasons"] == []


def test_stale_data_pause_is_named_as_temporary_not_as_no_mandate():
    paused = active(status="PAUSED", paused_reason="BREAKER_STALE_DATA: BTCUSDT ingest older than 10 minutes")
    s = ti.executability(idea(), [paused])
    assert s["status"] == "BLOCKED"
    assert s["reasons"][0]["code"] == "PROFILE_PAUSED_STALE_DATA"
    assert "temporarily paused" in s["reasons"][0]["text"] and "BTC Perps" in s["reasons"][0]["text"]


def test_halted_and_manually_paused_profiles_block_with_their_reason():
    halted = ti.executability(idea(), [active(status="HALTED", halted_reason="max drawdown")])
    manual = ti.executability(idea(), [active(status="PAUSED", paused_reason="operator")])
    assert halted["reasons"][0]["code"] == "PROFILE_HALTED" and "max drawdown" in halted["reasons"][0]["text"]
    assert manual["reasons"][0]["code"] == "PROFILE_PAUSED"


def test_no_profile_is_a_block_the_card_can_fix_for_a_long_only():
    long_s = ti.executability(idea("LONG"), [])
    short_s = ti.executability(idea("SHORT"), [])
    assert long_s["status"] == "BLOCKED" and long_s["reasons"][0]["code"] == "NO_PROFILE" and long_s["card_possible"]
    assert short_s["status"] == "BLOCKED" and not short_s["card_possible"]


def test_side_the_profile_does_not_allow_blocks():
    s = ti.executability(idea("SHORT"), [active(markets=("SPOT",), direction="LONG")])
    assert s["status"] == "BLOCKED" and s["reasons"][0]["code"] == "SIDE_NOT_ALLOWED"


def test_low_reward_and_stale_bars_block_and_all_reasons_are_listed():
    s = ti.executability(idea(rr=1.2, age=500.0), [active()])
    assert s["status"] == "BLOCKED"
    assert {r["code"] for r in s["reasons"]} == {"RR_BELOW_MIN", "BARS_STALE"}


def test_a_stop_wider_than_the_hard_stop_blocks_and_ranks_below_a_tighter_idea():
    wide = {**idea(), "risk_pct": 9.3, "entry_type": "ON_BREAK", "data_complete": True}
    tight = {**wide, "risk_pct": 1.2}
    s = ti.executability(wide, [active()])
    assert s["status"] == "BLOCKED" and s["reasons"][0]["code"] == "STOP_BEYOND_HARD_STOP"
    assert ti.executability(tight, [active()])["status"] == "POTENTIAL"
    assert ti.idea_score(tight, {}) > ti.idea_score(wide, {})


def test_unreadable_profiles_are_unknown_never_executable_and_never_no_mandate():
    s = ti.executability(idea(), None)
    assert s["status"] == "UNKNOWN" and s["reasons"][0]["code"] == "PROFILES_UNREADABLE" and not s["card_possible"]


# ---- the tool ---------------------------------------------------------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    """BTC trends up, ETH trends down, SOL has no readable bars."""
    series = {"BTCUSDT.BINANCE": UP, "ETHUSDT.BINANCE": DOWN}

    def fake_bars(instrument_id, timeframe="1h", n=48, now_ns=None):
        if instrument_id not in series:
            return {"bars": [], "complete": False, "reason": "lake bars could not be read"}
        return {**payload(series[instrument_id], timeframe=timeframe), "instrument_id": instrument_id}

    state = {"scopes": [active(), active(name="ETH Margin", coins=("ETHUSDT.BINANCE",), markets=("SPOT_MARGIN",))]}
    monkeypatch.setattr(ti, "recent_bars", fake_bars)
    monkeypatch.setattr(ti, "_fed_symbols", lambda: frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"}))
    monkeypatch.setattr(ti, "all_scopes", lambda: state["scopes"])
    return state


def run(**kw) -> dict:
    return json.loads(asyncio.run(ti.get_trade_ideas(**kw)))


def test_tool_returns_a_ranked_shortlist_with_status_and_scope(world):
    out = run()
    assert 1 <= len(out["ideas"]) <= 3
    assert [i["rank"] for i in out["ideas"]] == list(range(1, len(out["ideas"]) + 1))
    assert out["ideas"] == sorted(out["ideas"], key=lambda i: -i["score"])
    assert all(i["status"] in ("POTENTIAL", "BLOCKED", "UNKNOWN") for i in out["ideas"])
    assert out["scanned_timeframes"] == ["15m", "1h", "4h"]
    assert set(out["scanned_coins"]) == {"BTCUSDT", "ETHUSDT"}
    assert out["unavailable"] == [{"coin": "SOLUSDT", "reason": "lake bars could not be read"}]
    assert out["as_of"]


def test_tool_shows_blocked_ideas_instead_of_hiding_them(world):
    world["scopes"] = [active(status="PAUSED", paused_reason="BREAKER_STALE_DATA: BTCUSDT ingest older than 10 minutes"),
                       active(name="ETH Margin", coins=("ETHUSDT.BINANCE",), markets=("SPOT_MARGIN",), status="PAUSED",
                              paused_reason="BREAKER_STALE_DATA: ETHUSDT ingest older than 10 minutes")]
    out = run()
    assert out["ideas"] and all(i["status"] == "BLOCKED" for i in out["ideas"])
    assert out["profiles"]["paused_stale_data"] == ["BTC Perps", "ETH Margin"]
    assert "temporarily paused" in out["note"]


def test_tool_with_unreadable_profiles_still_gives_ideas_marked_unknown(world, monkeypatch):
    monkeypatch.setattr(ti, "all_scopes", lambda: None)
    out = run()
    assert out["ideas"] and all(i["status"] == "UNKNOWN" for i in out["ideas"])
    assert "profiles" in out["missing_evidence"]


def test_tool_limits_to_one_symbol_and_one_idea_per_coin_and_side(world):
    out = run(symbol="btc", max_results=10)
    assert {i["coin"] for i in out["ideas"]} == {"BTCUSDT"}
    keys = [(i["coin"], i["direction"]) for i in out["ideas"]]
    assert len(keys) == len(set(keys))


def test_tool_with_no_bars_at_all_says_missing_data_not_no_trade(world, monkeypatch):
    monkeypatch.setattr(ti, "recent_bars", lambda *a, **k: {"bars": [], "reason": "lake bars could not be read"})
    out = run()
    assert out["ideas"] == [] and "missing data" in out["note"] and "bars" in out["missing_evidence"]


def test_tool_is_a_read_tool_the_brain_has():
    assert ti.TRADE_IDEAS.risk is RiskClass.READ
    assert ti.TRADE_IDEAS in BRAIN_TOOLS


# ---- the widget, which is also the saved record -----------------------------------------------------------------


def test_widget_keeps_every_idea_with_its_status_for_later_scoring(world):
    content = json.dumps(run())
    w = widget_for_tool("get_trade_ideas", {}, call_id="c1", content=content)
    assert w["kind"] == "trade_ideas" and w["id"] == "c1" and w["as_of"]
    first = w["ideas"][0]
    for key in ("instrument_id", "timeframe", "direction", "entry", "stop", "target", "rr_ratio", "status", "bar_close"):
        assert first[key] is not None
    assert "trade_ideas" in describe_widget(w) and str(len(w["ideas"])) in describe_widget(w)


def test_widget_ignores_fields_it_does_not_know():
    content = json.dumps({"ideas": [{"rank": 1, "instrument_id": "BTCUSDT.BINANCE", "coin": "BTCUSDT",
                                     "timeframe": "1h", "kind": "trend_continuation", "direction": "LONG",
                                     "entry_type": "ON_BREAK", "entry": 1.0, "stop": 0.9, "target": 1.2,
                                     "rr_ratio": 2.0, "status": "EXECUTABLE", "a_new_field": 1}],
                          "as_of": "2026-11-10T12:00:00+00:00", "another_new_field": True})
    assert widget_for_tool("get_trade_ideas", {}, call_id="c2", content=content)["ideas"][0]["entry"] == 1.0


# ---- scoring a saved idea afterwards ----------------------------------------------------------------------------


def m1(prices: list[float]) -> list[dict]:
    """1m bars that step from one price to the next."""
    return [{"open": a, "high": max(a, b), "low": min(a, b), "close": b}
            for a, b in zip(prices[:-1], prices[1:], strict=True)]


def saved(direction="LONG", entry_type="ON_BREAK", entry=100.0, stop=98.0, target=104.0) -> dict:
    return {"direction": direction, "entry_type": entry_type, "entry": entry, "stop": stop, "target": target,
            "timeframe": "15m"}


def test_score_a_break_that_triggers_and_then_stops_out_loses_about_one_r():
    path = [99.0, 99.5, 100.2, 100.1] + [100.1 - 0.3 * i for i in range(1, 12)]
    s = ti.score_idea(saved(), m1(path))
    assert s["triggered"] and s["outcome"] == "STRUCTURAL_STOP"
    assert -1.3 < s["net_r"] < -0.9 and s["net_r"] < s["gross_r"]  # costs make the net worse than the gross


def test_score_an_idea_that_never_triggers_took_no_trade():
    flat = m1([99.0, 99.2] * (ti.IDEA_TRIGGER_BARS * 15))
    s = ti.score_idea(saved(), flat)
    assert s == {**s, "outcome": "NOT_TRIGGERED", "triggered": False, "net_r": None}
    assert ti.score_idea(saved(), flat[:20])["outcome"] == "PENDING"  # too early to say


def test_score_a_stop_traded_before_the_trigger_invalidates_the_idea():
    assert ti.score_idea(saved(), m1([99.0, 97.5, 101.0]))["outcome"] == "INVALIDATED_BEFORE_TRIGGER"


def test_score_a_short_at_market_that_falls_wins():
    path = [100.0 - 0.2 * i for i in range(60)]
    s = ti.score_idea(saved("SHORT", "AT_MARKET", entry=100.0, stop=102.0, target=96.0), m1(path) + m1([88.2, 103.0]))
    assert s["triggered"] and s["net_r"] is not None and s["net_r"] > 0


def test_score_without_bars_is_no_data_not_a_loss():
    assert ti.score_idea(saved(), [])["outcome"] == "NO_DATA"


def test_scorecard_counts_a_repeated_idea_once_and_says_when_the_sample_is_too_small():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "score_brain_ideas", Path(__file__).resolve().parents[1] / "scripts" / "score_brain_ideas.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    one = {"instrument_id": "BTCUSDT.BINANCE", "coin": "BTCUSDT", "direction": "LONG", "timeframe": "15m",
           "kind": "range_break", "entry_type": "ON_BREAK", "entry": 100.0, "stop": 98.0, "target": 104.0,
           "status": "BLOCKED"}
    rows = [{"created_at": T0, "widget": {"kind": "trade_ideas", "ideas": [one]}},
            {"created_at": T0 + datetime.timedelta(minutes=5), "widget": json.dumps({"ideas": [one]})}]
    ideas = mod.saved_ideas(rows)
    assert len(ideas) == 1 and ideas[0]["shown_at"] == T0
    path = [99.0, 99.5, 100.2, 100.1] + [100.1 - 0.3 * i for i in range(1, 12)]
    scored = [{**i, **ti.score_idea(i, m1(path))} for i in ideas]
    text = mod.render(scored, T0, T0 + datetime.timedelta(days=1))
    assert "Ideas: 1 distinct" in text and "| BLOCKED | 1 |" in text and "STRUCTURAL_STOP" in text
    assert "too few to judge an edge" in text and "Not realized P&L" in text


# ---- an untriggered breakout is information only; a limit that would fill at once gets no card ------------------

def test_an_on_break_idea_never_gets_a_card_whatever_its_status():
    waiting = idea(entry_type="ON_BREAK")
    unblocked = ti.executability(waiting, [active()])
    assert unblocked["status"] == "POTENTIAL" and unblocked["card_possible"] is False
    assert ti.executability(idea("LONG", entry_type="ON_BREAK"), [])["card_possible"] is False  # NO_PROFILE long too


def test_every_idea_says_whether_its_trigger_is_waiting_and_where_its_levels_come_from():
    for closes in (UP, DOWN, RANGE_LOW, RANGE_HIGH):
        for i in ti.ideas_for_frame("BTCUSDT.BINANCE", "1h", payload(closes)):
            assert i["trigger"] == ("WAITING" if i["entry_type"] == "ON_BREAK" else "NONE")
            assert "not a measured target" in i["level_sources"]["target"]
            if i["entry_type"] == "ON_BREAK":  # the break level lies beyond the newest close: it has not traded
                last = bars_from_closes(closes)[-1]["close"]
                assert (i["entry"] >= last) if i["direction"] == "LONG" else (i["entry"] <= last)


@pytest.mark.parametrize(("side", "limit", "market", "refused"), [
    ("BUY", 101.0, 100.0, True),    # a buy "breakout" limit above the market fills at once
    ("SELL", 99.0, 100.0, True),    # a sell "breakout" limit below the market fills at once
    ("BUY", 99.0, 100.0, False),    # an ordinary resting buy limit
    ("SELL", 101.0, 100.0, False),  # an ordinary resting sell limit
    ("BUY", 100.05, 100.0, False),  # within the tolerance of the market
    ("BUY", 99.0, None, True),      # no current price: not checkable, no card
])
def test_a_limit_entry_that_would_fill_at_once_is_refused(side, limit, market, refused):
    assert (ti.limit_entry_refusal(side, limit, market) is not None) is refused
