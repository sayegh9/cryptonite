"""Auto-trade profile contract (ADR 0011, spec docs/specs/profile-contract.md).

A profile is an extended mandate. This module is the single home of the names later tasks import: enums, the
mode/level and status/run-state maps, instrument helpers, deterministic market selection, preset slugs and the
profile field rules. Pure data and pure policy (standard library, pydantic and ``mandate`` only, no I/O).
"""

from collections.abc import Collection, Iterable, Mapping
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cryptonite_schemas.mandate import (
    MAX_DRAWDOWN_PCT_CAP,
    MAX_PERP_LEVERAGE_CAP,
    MAX_RISK_PER_TRADE_PCT_CAP,
    canonical_instrument,
)

LIVE_ENABLED = False  # ADR 0011: LIVE profiles stay unavailable until P5 lifts this


class ExecutionMode(StrEnum):
    ASK = "ASK"
    VETO = "VETO"
    AUTO = "AUTO"


class RunState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    HALTED = "HALTED"
    ARCHIVED = "ARCHIVED"


class Market(StrEnum):
    SPOT = "SPOT"
    SPOT_MARGIN = "SPOT_MARGIN"
    PERP = "PERP"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class TradeSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Style(StrEnum):
    SWING = "SWING"
    INTRADAY = "INTRADAY"


class Environment(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class NotifyLevel(StrEnum):
    ALL = "ALL"
    IMPORTANT = "IMPORTANT"
    NONE = "NONE"


class DecisionStage(StrEnum):
    SCAN = "SCAN"
    GATE = "GATE"
    DEBATE = "DEBATE"
    ROUTE = "ROUTE"
    APPROVAL = "APPROVAL"
    ORDER = "ORDER"
    FILL = "FILL"
    EXIT = "EXIT"
    LIFECYCLE = "LIFECYCLE"
    BREAKER = "BREAKER"
    SESSION = "SESSION"


MODE_LEVEL: Mapping[ExecutionMode, int] = {ExecutionMode.ASK: 1, ExecutionMode.VETO: 2, ExecutionMode.AUTO: 3}
STATUS_RUN_STATE: Mapping[str, RunState] = {
    "ACTIVE": RunState.RUNNING,
    "PAUSED": RunState.PAUSED,
    "STOPPING": RunState.STOPPING,
    "STOPPED": RunState.STOPPED,
    "HALTED": RunState.HALTED,
    "ARCHIVED": RunState.ARCHIVED,
}
# HALTED keeps its locks: positions may still be open and resume needs TOTP
_LOCKING_STATES = frozenset({RunState.RUNNING, RunState.PAUSED, RunState.STOPPING, RunState.HALTED})


def mode_of_level(level: int) -> ExecutionMode:
    for mode, lvl in MODE_LEVEL.items():
        if lvl == level:
            return mode
    raise ValueError(f"unknown autonomy level {level!r}")


def level_of_mode(mode: ExecutionMode | str) -> int:
    return MODE_LEVEL[ExecutionMode(mode)]


def run_state_of(status: str) -> RunState:
    try:
        return STATUS_RUN_STATE[status]
    except KeyError:
        raise ValueError(f"unknown mandate status {status!r}") from None


def accepts_entries(status: str) -> bool:
    return run_state_of(status) is RunState.RUNNING


def holds_coin_locks(status: str) -> bool:
    return run_state_of(status) in _LOCKING_STATES


QUOTE_ASSETS: tuple[str, ...] = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
_QUOTES_LONGEST_FIRST = sorted(QUOTE_ASSETS, key=len, reverse=True)
_VENUE = "BINANCE"
_MARKET_SUFFIX = {Market.SPOT: "", Market.SPOT_MARGIN: "-MARGIN", Market.PERP: "-PERP"}


def _local(instrument_id: str) -> str:
    return canonical_instrument(instrument_id).split(".", 1)[0]


def market_of(instrument_id: str) -> Market:
    local = _local(instrument_id)
    if local.endswith("-PERP"):
        return Market.PERP
    if local.endswith("-MARGIN"):
        return Market.SPOT_MARGIN
    return Market.SPOT


def symbol_of(instrument_id: str) -> str:
    local = _local(instrument_id)
    suffix = _MARKET_SUFFIX[market_of(instrument_id)]
    return local.removesuffix(suffix)


def instrument_for(spot_or_any_id: str, market: Market) -> str:
    return f"{symbol_of(spot_or_any_id)}{_MARKET_SUFFIX[market]}.{_VENUE}"


def underlying_spot(instrument_id: str) -> str:
    return instrument_for(instrument_id, Market.SPOT)


def base_asset(instrument_id: str) -> str:
    symbol = symbol_of(instrument_id)
    for quote in _QUOTES_LONGEST_FIRST:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            if base:
                return base
            break  # the longest matching quote leaves nothing: not a pair
    raise ValueError(f"cannot find the base asset of {instrument_id!r}")


def lock_keys(instruments: Iterable[str]) -> frozenset[str]:
    return frozenset(base_asset(i) for i in instruments)


def select_market(side: TradeSide | str, leverage: int, markets: Collection[Market | str]) -> Market | None:
    """ADR 0011 D8: LONG at 1x prefers SPOT; everything else PERP, then SPOT_MARGIN.

    Inputs are coerced to the enums: callers pass strings read from the DB or JSON, and an identity check on a plain
    string would silently skip SPOT for a 1x long (unknown values raise ValueError)."""
    side = TradeSide(side)
    markets = {Market(m) for m in markets}
    if leverage < 1:
        raise ValueError(f"leverage must be at least 1, got {leverage}")
    order = [Market.PERP, Market.SPOT_MARGIN]
    if side is TradeSide.LONG and leverage == 1:
        order.insert(0, Market.SPOT)
    return next((m for m in order if m in markets), None)


def allows_side(direction: Direction | str, side: TradeSide | str) -> bool:
    direction, side = Direction(direction), TradeSide(side)
    return direction is Direction.BOTH or direction.value == side.value


STYLE_TIMEFRAMES: Mapping[Style, tuple[str, ...]] = {Style.SWING: ("1h", "4h"), Style.INTRADAY: ("15m", "1h")}
PRESETS: Mapping[tuple[Style, TradeSide], tuple[str, ...]] = {
    (Style.SWING, TradeSide.LONG): ("swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m"),
    (Style.SWING, TradeSide.SHORT): (
        "swing-trend-pullback-short-1h",
        "swing-squeeze-breakdown-short-1h",
        "swing-sniper-pullback-short-15m",
    ),
    (Style.INTRADAY, TradeSide.LONG): ("scalp-multi-tf", "trend-pullback", "bollinger-breakout"),
    (Style.INTRADAY, TradeSide.SHORT): ("scalp-multi-tf-short", "trend-pullback-short", "bollinger-breakdown-short"),
}


def preset_ids_for(style: Style | str, direction: Direction | str) -> tuple[str, ...]:
    style = Style(style)
    ids = [i for side in TradeSide if allows_side(direction, side) for i in PRESETS[(style, side)]]
    return tuple(dict.fromkeys(ids))


class ProfileFields(BaseModel):
    """The ``ai_mandates`` columns a profile adds, plus the ones the profile rules read."""

    model_config = ConfigDict(frozen=True)

    markets: frozenset[Market] = Field(min_length=1)
    direction: Direction = Direction.LONG
    style: Style = Style.SWING
    environment: Environment = Environment.PAPER
    max_leverage: int = Field(default=1, ge=1, le=MAX_PERP_LEVERAGE_CAP)
    max_open_positions: int = Field(default=3, ge=1, le=20)
    max_debates_per_day: int = Field(default=6, ge=0, le=48)
    max_risk_per_trade_pct: Decimal = Field(default=Decimal("1"), gt=0, le=MAX_RISK_PER_TRADE_PCT_CAP)
    daily_loss_limit_pct: Decimal | None = Field(default=None, gt=0, le=MAX_DRAWDOWN_PCT_CAP)
    max_drawdown_pct: Decimal = Field(default=Decimal("20"), gt=0, le=MAX_DRAWDOWN_PCT_CAP)
    exit_tp_r: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0.5"), le=20)
    exit_tp1_scale_pct: Decimal = Field(default=Decimal("50"), ge=0, le=100)
    exit_trail_activate_pct: Decimal | None = Field(default=None, gt=0, le=50)
    exit_trail_pct: Decimal | None = Field(default=None, gt=0, le=50)
    exit_max_hold_hours: int | None = Field(default=None, ge=1, le=720)
    dry_run: bool = False
    notify_level: NotifyLevel = NotifyLevel.IMPORTANT
    breaker_max_loss_streak: int = Field(default=4, ge=1, le=50)
    breaker_max_reject_streak: int = Field(default=5, ge=1, le=50)


def profile_errors(f: ProfileFields, *, running: bool) -> list[str]:
    """Stable machine codes for the cross-field rules; empty means valid."""
    errors: list[str] = []
    if f.direction is not Direction.LONG and not f.markets & {Market.SPOT_MARGIN, Market.PERP}:
        errors.append("SHORT_NEEDS_MARGIN_OR_PERP")
    if f.max_leverage > 1 and f.markets == {Market.SPOT}:
        errors.append("LEVERAGE_NEEDS_MARGIN_OR_PERP")
    loss = f.daily_loss_limit_pct
    if loss is None:
        if running:
            errors.append("DAILY_LOSS_REQUIRED")
    else:
        if loss < f.max_risk_per_trade_pct:
            errors.append("DAILY_LOSS_BELOW_RISK")
        if loss > f.max_drawdown_pct:
            errors.append("DAILY_LOSS_ABOVE_DRAWDOWN")
    if f.exit_trail_activate_pct is not None and f.exit_trail_pct is not None:
        if f.exit_trail_pct > f.exit_trail_activate_pct:
            errors.append("TRAIL_ABOVE_ACTIVATION")
    if f.environment is Environment.LIVE and not LIVE_ENABLED:
        errors.append("LIVE_NOT_AVAILABLE")
    return errors


def default_daily_loss_pct(max_risk_per_trade_pct: Decimal, max_drawdown_pct: Decimal) -> Decimal:
    """ADR 0011 D9: three times the risk per trade, capped at the hard stop."""
    return min(3 * max_risk_per_trade_pct, max_drawdown_pct)


def risk_adding_profile_change(
    old: ProfileFields, new: ProfileFields, *, old_mode: ExecutionMode, new_mode: ExecutionMode
) -> list[str]:
    """Codes of every risk-adding difference (TOTP). Daily loss, risk, drawdown, R:R and conviction stay with
    ``mandate.risk_adding_change``."""
    codes: list[str] = []
    if level_of_mode(new_mode) > level_of_mode(old_mode):
        codes.append("MODE_RAISED")
    if new.markets - old.markets:
        codes.append("MARKET_ADDED")
    if any(allows_side(new.direction, s) and not allows_side(old.direction, s) for s in TradeSide):
        codes.append("DIRECTION_WIDENED")
    if new.max_leverage > old.max_leverage:
        codes.append("LEVERAGE_RAISED")
    if new.max_open_positions > old.max_open_positions:
        codes.append("OPEN_POSITIONS_RAISED")
    if new.max_debates_per_day > old.max_debates_per_day:
        codes.append("DEBATES_RAISED")
    if old.dry_run and not new.dry_run:
        codes.append("DRY_RUN_OFF")
    if (
        new.breaker_max_loss_streak > old.breaker_max_loss_streak
        or new.breaker_max_reject_streak > old.breaker_max_reject_streak
    ):
        codes.append("BREAKER_LOOSENED")
    if old.exit_max_hold_hours is not None and (
        new.exit_max_hold_hours is None or new.exit_max_hold_hours > old.exit_max_hold_hours
    ):
        codes.append("HOLD_LIMIT_LOOSENED")
    if old.environment is Environment.PAPER and new.environment is Environment.LIVE:
        codes.append("ENVIRONMENT_LIVE")
    return codes
