"""Configurable risk limits (remediation Phase 8, F7): tighten-only overrides of the code defaults.

The operator may store, per (environment, account), a value for three limits:

| Limit | Code default (= ceiling) | Enforced by |
|---|---|---|
| ``max_daily_loss_pct`` | 5 % of equity | ``GuardedOrderDispatcher.max_daily_loss_pct`` |
| ``max_order_equity_pct`` | 25 % of equity | ``GuardedOrderDispatcher.max_position_concentration_pct`` (the 1–25% clamp's upper end) |
| ``cross_mandate_exposure_pct`` | 25 % of equity | the F4 cross-mandate check (``cryptonite_execution.cross_exposure``) |

Rules (``validate_change``, enforced server-side by ``POST /api/v1/control/risk-limits``):

- A value is never above its code default: the defaults are the invariants (the 25% order clamp, the 5%
  daily loss, the F4 cap). The 20% account backstop is not configurable at all.
- ``None`` means "no override": the code default applies.
- Moving any limit *tighter* than what is stored needs no TOTP; moving it *looser* (a higher value, or
  removing an override, i.e. back toward the default) needs the operator's TOTP.
- The order cap cannot go below the 1% minimum order size (nothing could trade).

``apply_to_dispatcher`` is the only thing that touches the dispatcher: it sets the two attributes to
``min(baseline, override)``, so they are never looser than the code default.
"""

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation

from cryptonite_schemas.mandate import ACCOUNT_BACKSTOP_DRAWDOWN_PCT, MAX_POSITION_PCT_CAP

# Code defaults, in percent. They equal GuardedOrderDispatcher's constructor defaults (a test checks it).
DEFAULT_MAX_DAILY_LOSS_PCT = Decimal("5")
DEFAULT_MAX_ORDER_EQUITY_PCT = MAX_POSITION_PCT_CAP  # 25: the single-order clamp
# F4: the combined beta-weighted BTC exposure of every mandate, as % of account equity. The same number
# as the single-position clamp: all mandates together carry no more BTC risk than one maximal order.
DEFAULT_CROSS_MANDATE_EXPOSURE_PCT = MAX_POSITION_PCT_CAP
MIN_ORDER_EQUITY_PCT = Decimal("1")  # the clamp's lower end; not configurable
NOT_CONFIGURABLE = {"account_backstop_drawdown_pct": ACCOUNT_BACKSTOP_DRAWDOWN_PCT}


@dataclass(frozen=True)
class RiskLimitValues:
    """Override values in percent; ``None`` = no override (the code default applies)."""

    max_daily_loss_pct: Decimal | None = None
    max_order_equity_pct: Decimal | None = None
    cross_mandate_exposure_pct: Decimal | None = None

    @classmethod
    def from_row(cls, row) -> "RiskLimitValues":
        if row is None:
            return cls()
        return cls(**{f.name: (Decimal(str(row[f.name])) if row[f.name] is not None else None) for f in fields(cls)})

    def as_dict(self) -> dict[str, str | None]:
        return {f.name: (_plain(v) if (v := getattr(self, f.name)) is not None else None) for f in fields(self)}


DEFAULTS = RiskLimitValues(DEFAULT_MAX_DAILY_LOSS_PCT, DEFAULT_MAX_ORDER_EQUITY_PCT, DEFAULT_CROSS_MANDATE_EXPOSURE_PCT)
LIMIT_NAMES = tuple(f.name for f in fields(RiskLimitValues))


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def effective(values: RiskLimitValues) -> RiskLimitValues:
    """The limit that applies for each field: the override, never above the code default."""
    return RiskLimitValues(**{
        name: min(getattr(DEFAULTS, name), v) if (v := getattr(values, name)) is not None else getattr(DEFAULTS, name)
        for name in LIMIT_NAMES
    })


@dataclass(frozen=True)
class ChangeCheck:
    errors: dict[str, str]  # field -> why the value is refused (nothing is stored)
    loosened: tuple[str, ...]  # fields that move looser than what is stored (need TOTP)
    tightened: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def needs_totp(self) -> bool:
        return bool(self.loosened)


def validate_change(current: RiskLimitValues, requested: RiskLimitValues) -> ChangeCheck:
    """Pure check of a requested override set against what is stored."""
    errors: dict[str, str] = {}
    loosened: list[str] = []
    tightened: list[str] = []
    for name in LIMIT_NAMES:
        new = getattr(requested, name)
        default = getattr(DEFAULTS, name)
        if new is not None:
            try:
                finite = new.is_finite()
            except (AttributeError, InvalidOperation):
                finite = False
            if not finite:
                errors[name] = "must be a number"
                continue
            if new > default:
                errors[name] = f"{_plain(new)}% is above the code default of {_plain(default)}%; limits can only be tightened"
                continue
            if name == "max_order_equity_pct" and new < MIN_ORDER_EQUITY_PCT:
                errors[name] = (f"{_plain(new)}% is below the {_plain(MIN_ORDER_EQUITY_PCT)}% minimum order size; "
                                "nothing could trade")
                continue
            if new <= 0:
                errors[name] = "must be greater than 0"
                continue
        before = getattr(effective(current), name)
        after = getattr(effective(requested), name)
        if after > before:
            loosened.append(name)
        elif after < before:
            tightened.append(name)
    return ChangeCheck(errors, tuple(loosened), tuple(tightened))


@dataclass
class DispatcherBaseline:
    """The dispatcher's own values (fractions) before any override, never above the code default."""

    max_daily_loss: Decimal
    max_order_equity: Decimal

    @classmethod
    def capture(cls, dispatcher) -> "DispatcherBaseline":
        return cls(
            min(Decimal(str(dispatcher.max_daily_loss_pct)), DEFAULT_MAX_DAILY_LOSS_PCT / 100),
            min(Decimal(str(dispatcher.max_position_concentration_pct)), DEFAULT_MAX_ORDER_EQUITY_PCT / 100),
        )


def dispatcher_targets(baseline: DispatcherBaseline, overrides: RiskLimitValues) -> tuple[Decimal, Decimal]:
    """(max_daily_loss_pct, max_position_concentration_pct) as fractions: the override where it is
    tighter than the baseline, else the baseline. Never looser than the baseline."""
    daily, order = baseline.max_daily_loss, baseline.max_order_equity
    if overrides.max_daily_loss_pct is not None and overrides.max_daily_loss_pct > 0:
        daily = min(daily, overrides.max_daily_loss_pct / 100)
    if overrides.max_order_equity_pct is not None and overrides.max_order_equity_pct >= MIN_ORDER_EQUITY_PCT:
        order = min(order, overrides.max_order_equity_pct / 100)
    return daily, order


def apply_to_dispatcher(dispatcher, baseline: DispatcherBaseline, overrides: RiskLimitValues) -> bool:
    """Set the two dispatcher attributes to ``dispatcher_targets``. Returns True when something changed.
    Only attributes are set; the dispatcher's check logic is untouched."""
    daily, order = dispatcher_targets(baseline, overrides)
    changed = (Decimal(str(dispatcher.max_daily_loss_pct)) != daily
               or Decimal(str(dispatcher.max_position_concentration_pct)) != order)
    dispatcher.max_daily_loss_pct = daily
    dispatcher.max_position_concentration_pct = order
    return changed


def cross_mandate_cap_pct(overrides: RiskLimitValues) -> Decimal:
    """The F4 cap that applies: the override when tighter, else the default."""
    return effective(overrides).cross_mandate_exposure_pct
