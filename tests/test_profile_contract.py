"""Profile contract (ADR 0011, docs/specs/profile-contract.md): every table row, matrix and code."""

import itertools
from decimal import Decimal

import pytest
from cryptonite_schemas import profile as P
from cryptonite_schemas.profile import (
    Direction,
    Environment,
    ExecutionMode,
    Market,
    ProfileFields,
    RunState,
    Style,
    TradeSide,
)
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

ALL_MARKETS = list(Market)
SUBSETS = [frozenset(c) for n in (1, 2, 3) for c in itertools.combinations(ALL_MARKETS, n)]


def fields(**kw: object) -> ProfileFields:
    base: dict[str, object] = {"markets": {Market.SPOT}, "daily_loss_limit_pct": Decimal("3")}
    return ProfileFields(**{**base, **kw})  # type: ignore[arg-type]


def test_enum_values_equal_names() -> None:
    for enum in (
        P.ExecutionMode, P.RunState, P.Market, P.Direction, P.TradeSide, P.Style, P.Environment,
        P.NotifyLevel, P.DecisionStage,
    ):  # fmt: skip
        assert all(m.value == m.name for m in enum)
    assert [s.name for s in P.DecisionStage] == [
        "SCAN", "GATE", "DEBATE", "ROUTE", "APPROVAL", "ORDER", "FILL", "EXIT", "LIFECYCLE", "BREAKER", "SESSION",
    ]  # fmt: skip


@pytest.mark.parametrize(("mode", "level"), [(ExecutionMode.ASK, 1), (ExecutionMode.VETO, 2), (ExecutionMode.AUTO, 3)])
def test_mode_level(mode: ExecutionMode, level: int) -> None:
    assert P.MODE_LEVEL[mode] == level
    assert P.level_of_mode(mode) == level
    assert P.mode_of_level(level) is mode


@pytest.mark.parametrize("level", [0, 4, -1])
def test_mode_of_level_invalid(level: int) -> None:
    with pytest.raises(ValueError):
        P.mode_of_level(level)


STATUSES = {
    "ACTIVE": (RunState.RUNNING, True, True),
    "PAUSED": (RunState.PAUSED, False, True),
    "STOPPING": (RunState.STOPPING, False, True),
    "STOPPED": (RunState.STOPPED, False, False),
    "HALTED": (RunState.HALTED, False, True),
    "ARCHIVED": (RunState.ARCHIVED, False, False),
}


@pytest.mark.parametrize("status", STATUSES)
def test_status_mapping(status: str) -> None:
    state, entries, locks = STATUSES[status]
    assert P.STATUS_RUN_STATE[status] is state
    assert P.run_state_of(status) is state
    assert P.accepts_entries(status) is entries
    assert P.holds_coin_locks(status) is locks


@pytest.mark.parametrize("fn", [P.run_state_of, P.accepts_entries, P.holds_coin_locks])
def test_unknown_status(fn: object) -> None:
    with pytest.raises(ValueError):
        fn("RUNNING")  # type: ignore[operator]


@pytest.mark.parametrize(
    ("raw", "market", "symbol", "spot"),
    [
        ("BTCUSDT.BINANCE", Market.SPOT, "BTCUSDT", "BTCUSDT.BINANCE"),
        ("btcusdt", Market.SPOT, "BTCUSDT", "BTCUSDT.BINANCE"),
        ("BTCUSDT-PERP.BINANCE", Market.PERP, "BTCUSDT", "BTCUSDT.BINANCE"),
        ("btcusdt-perp", Market.PERP, "BTCUSDT", "BTCUSDT.BINANCE"),
        ("BTCUSDT-PERP", Market.PERP, "BTCUSDT", "BTCUSDT.BINANCE"),
        ("BTCUSDT-MARGIN.BINANCE", Market.SPOT_MARGIN, "BTCUSDT", "BTCUSDT.BINANCE"),
    ],
)
def test_instrument_helpers(raw: str, market: Market, symbol: str, spot: str) -> None:
    assert P.market_of(raw) is market
    assert P.symbol_of(raw) == symbol
    assert P.underlying_spot(raw) == spot


@pytest.mark.parametrize("start", ["btcusdt", "BTCUSDT-PERP", "BTCUSDT-MARGIN.BINANCE"])
def test_instrument_for(start: str) -> None:
    assert P.instrument_for(start, Market.SPOT) == "BTCUSDT.BINANCE"
    assert P.instrument_for(start, Market.PERP) == "BTCUSDT-PERP.BINANCE"
    assert P.instrument_for(start, Market.SPOT_MARGIN) == "BTCUSDT-MARGIN.BINANCE"


@pytest.mark.parametrize(
    ("raw", "base"),
    [
        ("BTCUSDT", "BTC"),
        ("1000PEPEUSDT", "1000PEPE"),
        ("ETHBTC", "ETH"),
        ("BNBETH", "BNB"),
        ("ABCFDUSD", "ABC"),
        ("BTCUSDT-PERP.BINANCE", "BTC"),
        ("ethusdc-margin.binance", "ETH"),
    ],
)
def test_base_asset(raw: str, base: str) -> None:
    assert P.base_asset(raw) == base


@pytest.mark.parametrize("raw", ["FOOBAR", "USDT", "BTC.BINANCE"])
def test_base_asset_no_match_or_empty_base(raw: str) -> None:
    with pytest.raises(ValueError):
        P.base_asset(raw)


def test_lock_keys() -> None:
    ids = ["BTCUSDT.BINANCE", "BTCUSDT-PERP.BINANCE", "ETHBTC", "bnbusdt-margin"]
    assert P.lock_keys(ids) == frozenset({"BTC", "ETH", "BNB"})
    assert P.lock_keys([]) == frozenset()


def expected_market(side: TradeSide, lev: int, markets: frozenset[Market]) -> Market | None:
    if side is TradeSide.LONG and lev == 1:
        order = [Market.SPOT, Market.PERP, Market.SPOT_MARGIN]
    else:
        order = [Market.PERP, Market.SPOT_MARGIN]
    return next((m for m in order if m in markets), None)


@pytest.mark.parametrize("side", list(TradeSide))
@pytest.mark.parametrize("lev", [1, 3])
@pytest.mark.parametrize("markets", SUBSETS)
def test_select_market_matrix(side: TradeSide, lev: int, markets: frozenset[Market]) -> None:
    assert P.select_market(side, lev, markets) is expected_market(side, lev, markets)


def test_select_market_spot_only_short_is_none_and_long_5x_margin() -> None:
    assert P.select_market(TradeSide.SHORT, 1, {Market.SPOT}) is None
    assert P.select_market(TradeSide.LONG, 5, {Market.SPOT, Market.SPOT_MARGIN}) is Market.SPOT_MARGIN
    assert P.select_market(TradeSide.LONG, 1, {Market.PERP, Market.SPOT_MARGIN}) is Market.PERP


@pytest.mark.parametrize("lev", [0, -2])
def test_select_market_bad_leverage(lev: int) -> None:
    with pytest.raises(ValueError):
        P.select_market(TradeSide.LONG, lev, {Market.SPOT})


def test_allows_side() -> None:
    assert P.allows_side(Direction.LONG, TradeSide.LONG)
    assert not P.allows_side(Direction.LONG, TradeSide.SHORT)
    assert P.allows_side(Direction.SHORT, TradeSide.SHORT)
    assert not P.allows_side(Direction.SHORT, TradeSide.LONG)
    assert P.allows_side(Direction.BOTH, TradeSide.LONG)
    assert P.allows_side(Direction.BOTH, TradeSide.SHORT)


def test_style_timeframes() -> None:
    assert P.STYLE_TIMEFRAMES == {Style.SWING: ("1h", "4h"), Style.INTRADAY: ("15m", "1h")}


@pytest.mark.parametrize("style", list(Style))
@pytest.mark.parametrize("direction", list(Direction))
def test_preset_ids_for(style: Style, direction: Direction) -> None:
    long_ids, short_ids = P.PRESETS[(style, TradeSide.LONG)], P.PRESETS[(style, TradeSide.SHORT)]
    expected = {Direction.LONG: long_ids, Direction.SHORT: short_ids, Direction.BOTH: long_ids + short_ids}
    assert P.preset_ids_for(style, direction) == expected[direction]
    assert len(set(P.preset_ids_for(style, direction))) == len(P.preset_ids_for(style, direction))


def test_preset_slugs_exact() -> None:
    assert P.preset_ids_for(Style.SWING, Direction.LONG) == (
        "swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m",
    )  # fmt: skip
    assert P.preset_ids_for(Style.SWING, Direction.SHORT) == (
        "swing-trend-pullback-short-1h", "swing-squeeze-breakdown-short-1h", "swing-sniper-pullback-short-15m",
    )  # fmt: skip
    assert P.preset_ids_for(Style.INTRADAY, Direction.LONG) == ("scalp-multi-tf", "trend-pullback", "bollinger-breakout")
    assert P.preset_ids_for(Style.INTRADAY, Direction.SHORT) == (
        "scalp-multi-tf-short", "trend-pullback-short", "bollinger-breakdown-short",
    )  # fmt: skip


def test_preset_ids_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    shared = dict(P.PRESETS)
    shared[(Style.SWING, TradeSide.SHORT)] = ("a", P.PRESETS[(Style.SWING, TradeSide.LONG)][0])
    monkeypatch.setattr(P, "PRESETS", shared)
    ids = P.preset_ids_for(Style.SWING, Direction.BOTH)
    assert ids.count(shared[(Style.SWING, TradeSide.LONG)][0]) == 1
    assert ids[-1] == "a"


def test_defaults_and_frozen() -> None:
    f = ProfileFields(markets={Market.SPOT})
    assert f.direction is Direction.LONG
    assert f.style is Style.SWING
    assert f.environment is Environment.PAPER
    assert (f.max_leverage, f.max_open_positions, f.max_debates_per_day) == (1, 3, 6)
    assert f.max_risk_per_trade_pct == Decimal("1")
    assert f.daily_loss_limit_pct is None
    assert f.max_drawdown_pct == Decimal("20")
    assert f.exit_tp_r == Decimal("2.0")
    assert f.exit_tp1_scale_pct == Decimal("50")
    assert f.exit_trail_activate_pct is None
    assert f.exit_trail_pct is None
    assert f.exit_max_hold_hours is None
    assert f.dry_run is False
    assert f.notify_level is P.NotifyLevel.IMPORTANT
    assert (f.breaker_max_loss_streak, f.breaker_max_reject_streak) == (4, 5)
    with pytest.raises(ValidationError):
        f.max_leverage = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "bad",
    [
        {"markets": set()},
        {"max_leverage": 0},
        {"max_leverage": 11},
        {"max_open_positions": 0},
        {"max_open_positions": 21},
        {"max_debates_per_day": -1},
        {"max_debates_per_day": 49},
        {"max_risk_per_trade_pct": Decimal("0")},
        {"max_risk_per_trade_pct": Decimal("2.5")},
        {"daily_loss_limit_pct": Decimal("0")},
        {"daily_loss_limit_pct": Decimal("21")},
        {"max_drawdown_pct": Decimal("21")},
        {"exit_tp_r": Decimal("0.4")},
        {"exit_tp_r": Decimal("21")},
        {"exit_tp1_scale_pct": Decimal("-1")},
        {"exit_tp1_scale_pct": Decimal("101")},
        {"exit_trail_activate_pct": Decimal("0")},
        {"exit_trail_activate_pct": Decimal("51")},
        {"exit_trail_pct": Decimal("0")},
        {"exit_trail_pct": Decimal("51")},
        {"exit_max_hold_hours": 0},
        {"exit_max_hold_hours": 721},
        {"breaker_max_loss_streak": 0},
        {"breaker_max_loss_streak": 51},
        {"breaker_max_reject_streak": 0},
        {"breaker_max_reject_streak": 51},
    ],
)
def test_field_bounds_raise(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        fields(**bad)


def test_field_bounds_accept_edges() -> None:
    f = fields(
        max_leverage=10, max_open_positions=20, max_debates_per_day=0, exit_tp_r=Decimal("0.5"),
        exit_tp1_scale_pct=Decimal("0"), exit_trail_activate_pct=Decimal("50"), exit_trail_pct=Decimal("50"),
        exit_max_hold_hours=720, breaker_max_loss_streak=50, breaker_max_reject_streak=1,
    )  # fmt: skip
    assert f.max_leverage == 10


def test_valid_profile_has_no_errors() -> None:
    assert P.profile_errors(fields(), running=True) == []


def test_error_short_needs_margin_or_perp() -> None:
    for d in (Direction.SHORT, Direction.BOTH):
        assert P.profile_errors(fields(direction=d), running=False) == ["SHORT_NEEDS_MARGIN_OR_PERP"]
        for m in (Market.SPOT_MARGIN, Market.PERP):
            assert P.profile_errors(fields(direction=d, markets={Market.SPOT, m}), running=False) == []


def test_error_leverage_needs_margin_or_perp() -> None:
    assert P.profile_errors(fields(max_leverage=2), running=False) == ["LEVERAGE_NEEDS_MARGIN_OR_PERP"]
    assert P.profile_errors(fields(max_leverage=2, markets={Market.PERP}), running=False) == []
    assert P.profile_errors(fields(max_leverage=2, markets={Market.SPOT, Market.PERP}), running=False) == []


def test_error_daily_loss_required() -> None:
    f = fields(daily_loss_limit_pct=None)
    assert P.profile_errors(f, running=True) == ["DAILY_LOSS_REQUIRED"]
    assert P.profile_errors(f, running=False) == []


def test_error_daily_loss_below_risk_and_above_drawdown() -> None:
    assert P.profile_errors(fields(daily_loss_limit_pct=Decimal("0.5")), running=True) == ["DAILY_LOSS_BELOW_RISK"]
    assert P.profile_errors(fields(daily_loss_limit_pct=Decimal("1")), running=True) == []
    f = fields(daily_loss_limit_pct=Decimal("15"), max_drawdown_pct=Decimal("10"))
    assert P.profile_errors(f, running=True) == ["DAILY_LOSS_ABOVE_DRAWDOWN"]
    f = fields(daily_loss_limit_pct=Decimal("10"), max_drawdown_pct=Decimal("10"))
    assert P.profile_errors(f, running=True) == []


def test_error_trail_above_activation() -> None:
    both = {"exit_trail_activate_pct": Decimal("5"), "exit_trail_pct": Decimal("6")}
    assert P.profile_errors(fields(**both), running=False) == ["TRAIL_ABOVE_ACTIVATION"]
    ok = {"exit_trail_activate_pct": Decimal("5"), "exit_trail_pct": Decimal("5")}
    assert P.profile_errors(fields(**ok), running=False) == []
    assert P.profile_errors(fields(exit_trail_pct=Decimal("9")), running=False) == []
    assert P.profile_errors(fields(exit_trail_activate_pct=Decimal("1")), running=False) == []


def test_error_live_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    assert P.LIVE_ENABLED is False
    assert P.profile_errors(fields(environment=Environment.LIVE), running=False) == ["LIVE_NOT_AVAILABLE"]
    monkeypatch.setattr(P, "LIVE_ENABLED", True)
    assert P.profile_errors(fields(environment=Environment.LIVE), running=False) == []


def test_errors_combined_in_spec_order() -> None:
    f = fields(
        direction=Direction.SHORT, max_leverage=2, daily_loss_limit_pct=Decimal("0.5"),
        exit_trail_activate_pct=Decimal("1"), exit_trail_pct=Decimal("2"), environment=Environment.LIVE,
    )  # fmt: skip
    assert P.profile_errors(f, running=True) == [
        "SHORT_NEEDS_MARGIN_OR_PERP", "LEVERAGE_NEEDS_MARGIN_OR_PERP", "DAILY_LOSS_BELOW_RISK",
        "TRAIL_ABOVE_ACTIVATION", "LIVE_NOT_AVAILABLE",
    ]  # fmt: skip
    g = fields(daily_loss_limit_pct=None, max_leverage=3)
    assert P.profile_errors(g, running=True) == ["LEVERAGE_NEEDS_MARGIN_OR_PERP", "DAILY_LOSS_REQUIRED"]


def test_default_daily_loss_pct() -> None:
    assert P.default_daily_loss_pct(Decimal("1"), Decimal("20")) == Decimal("3")
    assert P.default_daily_loss_pct(Decimal("2"), Decimal("5")) == Decimal("5")
    assert P.default_daily_loss_pct(Decimal("1.5"), Decimal("4.5")) == Decimal("4.5")


def change(old: ProfileFields | None = None, new: ProfileFields | None = None, **modes: ExecutionMode) -> list[str]:
    old = old or fields()
    return P.risk_adding_profile_change(
        old, new or old,
        old_mode=modes.get("old_mode", ExecutionMode.VETO),
        new_mode=modes.get("new_mode", ExecutionMode.VETO),
    )  # fmt: skip


def test_no_change_is_free() -> None:
    assert change() == []


def test_mode_raised_only_upward() -> None:
    assert change(new_mode=ExecutionMode.AUTO) == ["MODE_RAISED"]
    assert change(old_mode=ExecutionMode.ASK) == ["MODE_RAISED"]
    assert change(new_mode=ExecutionMode.ASK) == []
    assert change(old_mode=ExecutionMode.AUTO) == []


def test_market_added() -> None:
    assert change(new=fields(markets={Market.SPOT, Market.PERP})) == ["MARKET_ADDED"]
    base = fields(markets={Market.SPOT, Market.PERP})
    assert change(old=base, new=fields(markets={Market.SPOT})) == []
    assert change(old=base, new=fields(markets={Market.PERP, Market.SPOT_MARGIN})) == ["MARKET_ADDED"]


@pytest.mark.parametrize(
    ("old", "new", "widened"),
    [
        (Direction.LONG, Direction.SHORT, True),
        (Direction.LONG, Direction.BOTH, True),
        (Direction.SHORT, Direction.LONG, True),
        (Direction.SHORT, Direction.BOTH, True),
        (Direction.BOTH, Direction.LONG, False),
        (Direction.BOTH, Direction.SHORT, False),
        (Direction.LONG, Direction.LONG, False),
        (Direction.BOTH, Direction.BOTH, False),
    ],
)
def test_direction_widened(old: Direction, new: Direction, widened: bool) -> None:
    m = {Market.PERP}
    assert change(old=fields(direction=old, markets=m), new=fields(direction=new, markets=m)) == (
        ["DIRECTION_WIDENED"] if widened else []
    )


def test_numeric_raises_and_lowers() -> None:
    pm = {Market.PERP}
    base = fields(markets=pm, max_leverage=3, max_open_positions=3, max_debates_per_day=6)
    assert change(old=base, new=fields(markets=pm, max_leverage=4)) == ["LEVERAGE_RAISED"]
    assert change(old=base, new=fields(markets=pm, max_leverage=3, max_open_positions=4)) == ["OPEN_POSITIONS_RAISED"]
    assert change(old=base, new=fields(markets=pm, max_leverage=3, max_debates_per_day=7)) == ["DEBATES_RAISED"]
    lowered = fields(markets=pm, max_leverage=2, max_open_positions=2, max_debates_per_day=0)
    assert change(old=base, new=lowered) == []


def test_dry_run_off() -> None:
    assert change(old=fields(dry_run=True), new=fields(dry_run=False)) == ["DRY_RUN_OFF"]
    assert change(old=fields(dry_run=False), new=fields(dry_run=True)) == []
    assert change(old=fields(dry_run=True), new=fields(dry_run=True)) == []


def test_breaker_loosened() -> None:
    assert change(new=fields(breaker_max_loss_streak=5)) == ["BREAKER_LOOSENED"]
    assert change(new=fields(breaker_max_reject_streak=6)) == ["BREAKER_LOOSENED"]
    assert change(new=fields(breaker_max_loss_streak=5, breaker_max_reject_streak=6)) == ["BREAKER_LOOSENED"]
    assert change(new=fields(breaker_max_loss_streak=3, breaker_max_reject_streak=4)) == []


def test_hold_limit_loosened() -> None:
    hold = fields(exit_max_hold_hours=24)
    assert change(old=hold, new=fields(exit_max_hold_hours=25)) == ["HOLD_LIMIT_LOOSENED"]
    assert change(old=hold, new=fields(exit_max_hold_hours=None)) == ["HOLD_LIMIT_LOOSENED"]
    assert change(old=hold, new=fields(exit_max_hold_hours=12)) == []
    assert change(old=hold, new=hold) == []
    assert change(old=fields(exit_max_hold_hours=None), new=hold) == []
    assert change(old=fields(exit_max_hold_hours=None), new=fields(exit_max_hold_hours=None)) == []


def test_environment_live() -> None:
    paper, live = fields(), fields(environment=Environment.LIVE)
    assert change(old=paper, new=live) == ["ENVIRONMENT_LIVE"]
    assert change(old=live, new=paper) == []
    assert change(old=live, new=live) == []


def test_free_edits() -> None:
    new = fields(
        style=Style.INTRADAY, notify_level=P.NotifyLevel.NONE, exit_tp_r=Decimal("9"),
        exit_trail_activate_pct=Decimal("5"), exit_trail_pct=Decimal("3"), max_risk_per_trade_pct=Decimal("2"),
        daily_loss_limit_pct=Decimal("10"), max_drawdown_pct=Decimal("20"),
    )  # fmt: skip
    assert change(new=new) == []


def test_all_codes_together_in_spec_order() -> None:
    old = fields(
        markets={Market.PERP}, direction=Direction.LONG, dry_run=True, exit_max_hold_hours=10,
        max_leverage=2, max_open_positions=2, max_debates_per_day=2,
    )  # fmt: skip
    new = fields(
        markets={Market.PERP, Market.SPOT}, direction=Direction.BOTH, dry_run=False, exit_max_hold_hours=None,
        max_leverage=3, max_open_positions=3, max_debates_per_day=3, environment=Environment.LIVE,
        breaker_max_loss_streak=9,
    )  # fmt: skip
    assert change(old=old, new=new, old_mode=ExecutionMode.ASK, new_mode=ExecutionMode.AUTO) == [
        "MODE_RAISED", "MARKET_ADDED", "DIRECTION_WIDENED", "LEVERAGE_RAISED", "OPEN_POSITIONS_RAISED",
        "DEBATES_RAISED", "DRY_RUN_OFF", "BREAKER_LOOSENED", "HOLD_LIMIT_LOOSENED", "ENVIRONMENT_LIVE",
    ]  # fmt: skip


@given(
    markets=st.sets(st.sampled_from(ALL_MARKETS), min_size=1),
    side=st.sampled_from(list(TradeSide)),
    leverage=st.integers(min_value=1, max_value=10),
)
def test_select_market_property(markets: set[Market], side: TradeSide, leverage: int) -> None:
    f = ProfileFields(markets=markets, max_leverage=leverage)
    result = P.select_market(side, leverage, f.markets)
    if result is not None:
        assert result in f.markets
        if side is TradeSide.SHORT:
            assert result is not Market.SPOT
