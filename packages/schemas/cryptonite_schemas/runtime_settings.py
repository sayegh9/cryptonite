"""Settings the services read every cycle (ADR 0007). Pure: a service reads the stored value from ``system_settings``
and passes it here, so the parsing and the fall-back rule are the same in every service and in the API.

Rule: a missing value takes the default; a valid stored value is applied; an invalid one (does not parse, or is
outside its bounds) keeps the last valid value and is reported as a problem.
"""

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


# registry / system_settings key -> bounds and the code default (the env default, where a service has one)
@dataclass(frozen=True)
class Bound:
    default: float
    low: float
    high: float


RUNTIME_BOUNDS: dict[str, Bound] = {
    "alerts.heartbeat_grace_s": Bound(300, 60, 3600),
    "alerts.sentiment_stale_s": Bound(86400, 3600, 604800),
    "scanner.max_active_specs": Bound(10, 1, 50),
    "exit.monitor_interval_s": Bound(30, 5, 300),
    # Operator session and login policy (ADR 0007, Phase 2). The ceilings are the current defaults, so a session
    # can only be made shorter from the default; the lockout window is the one whose higher value is the tighter.
    "auth.session_ttl_s": Bound(43200, 900, 43200),
    "auth.idle_timeout_s": Bound(1800, 300, 1800),
    "auth.lockout_s": Bound(300, 60, 3600),
    "auth.max_login_attempts": Bound(5, 3, 10),
    # Leverage (ADR 0009, leverage plan §2.1, §2.3): each setup grade's leverage ceiling, at most the 10x account
    # maximum (lower is tighter), and the slippage allowance in % (higher is tighter: the stop is priced further away)
    "leverage.grade_ceiling_a": Bound(10, 1, 10),
    "leverage.grade_ceiling_b": Bound(5, 1, 10),
    "leverage.grade_ceiling_c": Bound(2, 1, 10),
    "leverage.slippage_allowance_pct": Bound(0.3, 0.05, 2.0),
    # Auto-trade profiles (ADR 0011 D10, D11): the master switch (0 off, 1 on; enabling is risk-adding) and the share of
    # account equity the budgets of the running profiles may add up to (raising is risk-adding)
    "autotrade.enabled": Bound(0, 0, 1),
    "autotrade.max_allocated_pct": Bound(90, 10, 100),
}


@dataclass(frozen=True)
class DecimalBound:
    """Exact decimal bounds for settings that feed accounting calculations."""

    default: Decimal
    low: Decimal
    high: Decimal
    decimals: int


# Binance's public Margin Data values as observed on 2026-10-29. These are percentage
# points per hour, matching the display (0.0004 means 0.0004%, or Decimal("0.000004")
# as the rate multiplier). They are snapshots of a dynamic rate, not a live rate feed.
MARGIN_INTEREST_KEYS = {
    "BTC": "margin.interest_hourly.BTC",
    "ETH": "margin.interest_hourly.ETH",
    "XRP": "margin.interest_hourly.XRP",
    "WLD": "margin.interest_hourly.WLD",
    "USDT": "margin.interest_hourly.USDT",
}
MARGIN_SETTING_BOUNDS: dict[str, DecimalBound] = {
    MARGIN_INTEREST_KEYS["BTC"]: DecimalBound(Decimal("0.00004933"), Decimal("0"), Decimal("1"), 8),
    MARGIN_INTEREST_KEYS["ETH"]: DecimalBound(Decimal("0.00022700"), Decimal("0"), Decimal("1"), 8),
    MARGIN_INTEREST_KEYS["XRP"]: DecimalBound(Decimal("0.00039871"), Decimal("0"), Decimal("1"), 8),
    MARGIN_INTEREST_KEYS["WLD"]: DecimalBound(Decimal("0.00140158"), Decimal("0"), Decimal("1"), 8),
    MARGIN_INTEREST_KEYS["USDT"]: DecimalBound(Decimal("0.00065600"), Decimal("0"), Decimal("1"), 8),
    # The liquidation fee is a percentage of liquidated assets, as documented by Binance.
    "margin.liquidation_fee_pct": DecimalBound(Decimal("2"), Decimal("0"), Decimal("5"), 2),
}
# Pair values are settings so planning and margin math share the same source. Leverage
# and risk ratios are verified against Binance's public pair data and tier FAQ; the
# displayed borrow limits are unverified as tier caps and may only be loosened with TOTP.
# A missing asset limit is represented by an explicit zero cap for that side.
MARGIN_PAIR_SETTINGS: dict[str, dict[str, Decimal]] = {
    "BTCUSDT": {
        "max_leverage": Decimal("10"), "call_level": Decimal("1.10"),
        "liquidation_level": Decimal("1.05"), "max_borrow_BTC": Decimal("80"),
        "max_borrow_USDT": Decimal("450000"),
    },
    "ETHUSDT": {
        "max_leverage": Decimal("10"), "call_level": Decimal("1.10"),
        "liquidation_level": Decimal("1.05"), "max_borrow_ETH": Decimal("1600"),
        "max_borrow_USDT": Decimal("450000"),
    },
    "XRPUSDT": {
        "max_leverage": Decimal("10"), "call_level": Decimal("1.10"),
        "liquidation_level": Decimal("1.05"), "max_borrow_XRP": Decimal("584000"),
        "max_borrow_USDT": Decimal("450000"),
    },
    "WLDUSDT": {
        "max_leverage": Decimal("5"), "call_level": Decimal("1.19"),
        "liquidation_level": Decimal("1.15"), "max_borrow_WLD": Decimal("66600"),
        "max_borrow_USDT": Decimal("119000"),
    },
}


def _pair_setting_bounds() -> dict[str, DecimalBound]:
    """Build bounded, exact-decimal settings for the supported pair rows."""
    bounds: dict[str, DecimalBound] = {}
    for pair, values in MARGIN_PAIR_SETTINGS.items():
        for field, default in values.items():
            if field == "max_leverage":
                low, decimals = Decimal("1"), 0
            elif field == "call_level":
                low, decimals = Decimal("1"), 2
            elif field == "liquidation_level":
                low, decimals = Decimal("1"), 2
            else:
                low, decimals = Decimal("0"), 8
            if field in {"call_level", "liquidation_level"}:
                high = Decimal("2")
            elif field.startswith("max_borrow_"):
                # The displayed value is the default. A bounded, risk-adding increase
                # is available only through the registry's TOTP-gated loosening path.
                high = default * Decimal("2")
            else:
                high = default
            bounds[f"margin.pair.{pair}.{field}"] = DecimalBound(default, low, high, decimals)
    return bounds


MARGIN_SETTING_BOUNDS.update(_pair_setting_bounds())
AUTOTRADE_ENABLED_KEY = "autotrade.enabled"
AUTOTRADE_MAX_ALLOCATED_KEY = "autotrade.max_allocated_pct"
GRADE_CEILING_KEYS = {"A": "leverage.grade_ceiling_a", "B": "leverage.grade_ceiling_b", "C": "leverage.grade_ceiling_c"}
SLIPPAGE_KEY = "leverage.slippage_allowance_pct"
LEVERAGE_KEYS = (*GRADE_CEILING_KEYS.values(), SLIPPAGE_KEY)


def _parse(raw: Any) -> float:
    """A stored value: a JSON string (as the API writes it), a bare number or text."""
    if isinstance(raw, str) and raw.strip().startswith('"'):
        raw = json.loads(raw)
    return float(str(raw).strip())


def read_number(raw: Any, bound: Bound, last_good: float) -> tuple[float, str | None]:
    """The value to apply for a stored ``raw``, and a problem to report when the stored value was refused."""
    if raw is None:
        return bound.default, None
    try:
        value = _parse(raw)
    except (TypeError, ValueError):
        return last_good, f"stored value {raw!r} is not a number"
    if not math.isfinite(value) or not bound.low <= value <= bound.high:
        return last_good, f"stored value {raw!r} is outside {bound.low:g}..{bound.high:g}"
    return value, None


def read_decimal(raw: Any, bound: DecimalBound, last_good: Decimal) -> tuple[Decimal, str | None]:
    """Read a bounded setting without passing through binary floating-point."""
    if raw is None:
        return bound.default, None
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith('"') else raw
        parsed = Decimal(str(value).strip())
    except (TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return last_good, f"stored value {raw!r} is not a decimal number"
    if not parsed.is_finite() or not bound.low <= parsed <= bound.high:
        return last_good, f"stored value {raw!r} is outside {bound.low}..{bound.high}"
    if parsed != parsed.quantize(Decimal(1).scaleb(-bound.decimals)):
        return last_good, f"stored value {raw!r} has more than {bound.decimals} decimal places"
    return parsed, None


@dataclass(frozen=True)
class MarginSettings:
    """Exact rates, fees and supported pair risk settings used by paper margin."""

    interest_hourly_pct: dict[str, Decimal]
    liquidation_fee_pct: Decimal
    pair_settings: dict[str, dict[str, Decimal]]


def default_margin_settings() -> MarginSettings:
    """Return the documented 2026-10-29 snapshots for assets and liquidation fee."""
    rates = {
        asset: MARGIN_SETTING_BOUNDS[key].default
        for asset, key in MARGIN_INTEREST_KEYS.items()
    }
    pairs = {pair: dict(values) for pair, values in MARGIN_PAIR_SETTINGS.items()}
    return MarginSettings(rates, MARGIN_SETTING_BOUNDS["margin.liquidation_fee_pct"].default, pairs)


def read_margin_settings(
    stored: dict[str, Any], last_good: MarginSettings,
) -> tuple[MarginSettings, list[str]]:
    """Apply valid exact-decimal margin settings; retain the last valid value on refusal."""
    problems: list[str] = []
    rates: dict[str, Decimal] = {}
    for asset, key in MARGIN_INTEREST_KEYS.items():
        value, problem = read_decimal(stored.get(key), MARGIN_SETTING_BOUNDS[key],
                                      last_good.interest_hourly_pct.get(asset,
                                                                       MARGIN_SETTING_BOUNDS[key].default))
        if problem:
            problems.append(f"{key}: {problem}")
        rates[asset] = value
    fee_key = "margin.liquidation_fee_pct"
    fee, problem = read_decimal(stored.get(fee_key), MARGIN_SETTING_BOUNDS[fee_key],
                                last_good.liquidation_fee_pct)
    if problem:
        problems.append(f"{fee_key}: {problem}")
    pairs: dict[str, dict[str, Decimal]] = {}
    for pair, defaults in MARGIN_PAIR_SETTINGS.items():
        pair_values: dict[str, Decimal] = {}
        for field, default in defaults.items():
            key = f"margin.pair.{pair}.{field}"
            bound = MARGIN_SETTING_BOUNDS[key]
            last_good_value = last_good.pair_settings.get(pair, {}).get(field, default)
            value, problem = read_decimal(stored.get(key), bound, last_good_value)
            if problem:
                problems.append(f"{key}: {problem}")
            pair_values[field] = value
        pairs[pair] = pair_values
    return MarginSettings(rates, fee, pairs), problems


@dataclass(frozen=True)
class LeverageSettings:
    grade_ceilings: dict[str, int]  # A/B/C -> leverage; NO_TRADE is always 0
    slippage_pct: float  # in %, e.g. 0.3


def default_leverage_settings() -> LeverageSettings:
    return LeverageSettings({g: int(RUNTIME_BOUNDS[k].default) for g, k in GRADE_CEILING_KEYS.items()} | {"NO_TRADE": 0},
                            RUNTIME_BOUNDS[SLIPPAGE_KEY].default)


def read_leverage_settings(stored: dict[str, Any], last_good: LeverageSettings) -> tuple[LeverageSettings, list[str]]:
    """The grade ceilings and slippage allowance to apply (same rule as every runtime value), and the problems."""
    problems: list[str] = []
    ceilings: dict[str, int] = {"NO_TRADE": 0}
    for grade, key in GRADE_CEILING_KEYS.items():
        value, problem = read_number(stored.get(key), RUNTIME_BOUNDS[key], last_good.grade_ceilings.get(grade, 0))
        if problem:
            problems.append(f"{key}: {problem}")
        ceilings[grade] = int(value)
    slip, problem = read_number(stored.get(SLIPPAGE_KEY), RUNTIME_BOUNDS[SLIPPAGE_KEY], last_good.slippage_pct)
    if problem:
        problems.append(f"{SLIPPAGE_KEY}: {problem}")
    return LeverageSettings(ceilings, slip), problems
