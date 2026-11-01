"""P3b: a profile's strategy and timeframe picks (empty = the style defaults) and their TOTP rule."""
from cryptonite_scanner.presets import _PROFILE_PRESETS
from cryptonite_schemas.profile import (
    PRESET_CATALOG,
    ExecutionMode,
    ProfileFields,
    effective_presets,
    effective_timeframes,
    risk_adding_profile_change,
    selection_errors,
)


def test_catalog_matches_the_scanner_registry():
    assert set(PRESET_CATALOG) == set(_PROFILE_PRESETS)
    for slug, preset in _PROFILE_PRESETS.items():
        side = (preset.risk.direction if preset.risk else None) or "LONG"
        assert PRESET_CATALOG[slug].side.value == side, slug
        assert set(PRESET_CATALOG[slug].timeframes) == set(preset.universe.timeframes), slug


def test_empty_picks_follow_the_style():
    assert effective_presets("SWING", "LONG", []) == (
        "swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m")
    assert effective_timeframes("SWING", "LONG", [], []) == ("15m", "1h", "4h")  # the 15m sniper runs on 15m
    assert effective_timeframes("INTRADAY", "BOTH", [], []) == ("15m", "1h")


def test_explicit_picks_are_filtered_by_direction_and_preset_timeframes():
    picks = ["trend-pullback", "scalp-multi-tf-short", "retired-preset"]
    assert effective_presets("INTRADAY", "LONG", picks) == ("trend-pullback",)
    # 4h runs only where a picked preset supports it
    assert effective_timeframes("SWING", "LONG", ["trend-pullback"], ["15m", "4h"]) == ("15m",)
    assert effective_timeframes("SWING", "LONG", ["rsi-oversold-reversal"], []) == ("1h",)


def test_selection_errors():
    assert selection_errors("SWING", "LONG", [], []) == []
    assert selection_errors("SWING", "LONG", ["macd-momentum-shift"], ["15m"]) == []
    assert selection_errors("SWING", "LONG", ["nope"], []) == ["UNKNOWN_STRATEGY"]
    assert selection_errors("SWING", "LONG", ["trend-pullback-short"], []) == ["STRATEGY_DIRECTION_MISMATCH"]
    assert selection_errors("SWING", "LONG", [], ["5m"]) == ["TIMEFRAME_UNSUPPORTED"]
    assert selection_errors("SWING", "LONG", ["trend-pullback"], ["4h"]) == ["TIMEFRAME_UNSUPPORTED"]
    # a 15m/1h-only pick under Swing's 1h/4h defaults still runs 1h; only 4h-only would leave nothing
    assert selection_errors("SWING", "LONG", ["trend-pullback"], []) == []


def _f(**kw) -> ProfileFields:
    return ProfileFields(**{"markets": {"SPOT"}, **kw})


def _codes(old: ProfileFields, new: ProfileFields) -> list[str]:
    return risk_adding_profile_change(old, new, old_mode=ExecutionMode.ASK, new_mode=ExecutionMode.ASK)


def test_totp_compares_what_runs_not_the_stored_lists():
    one = _f(strategies=("swing-trend-pullback-1h",))
    assert _codes(_f(), one) == []  # [] -> [a]: fewer presets
    assert "STRATEGY_ADDED" in _codes(one, _f())  # [a] -> []: the defaults add presets
    # H4: the default SWING presets already run 15m (the sniper preset), so picking 15m adds it only where it was not
    assert "TIMEFRAME_ADDED" in _codes(one, _f(strategies=one.strategies, timeframes=("15m", "1h", "4h")))
    # a preset named for its timeframe (``-1h``) always runs it, so these use one that is not
    rsi = ("rsi-oversold-reversal",)
    assert _codes(_f(strategies=rsi, timeframes=("15m",)), _f(strategies=rsi, timeframes=("15m", "1h"))) == [
        "TIMEFRAME_ADDED"]
    assert _codes(_f(strategies=rsi, timeframes=("15m", "1h")), _f(strategies=rsi, timeframes=("15m",))) == []
    assert _codes(_f(), _f(style="INTRADAY")) == []  # a style change stays a free edit (ADR 0011)
    assert _codes(_f(), _f(direction="BOTH", markets={"PERP"})) == ["MARKET_ADDED", "DIRECTION_WIDENED"]
