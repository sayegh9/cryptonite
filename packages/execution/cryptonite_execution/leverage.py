"""Leveraged order planning (leverage plan §2.1, Stage 2.2; ADR 0009).

Pure: no I/O and no clock, apart from the bracket tables ``margin`` holds (live from Redis, else the BTC/ETH
reference snapshot). An order is a margin, a leverage and a stop. Notional = margin x leverage, and the leverage
is capped at the lowest of six ceilings; the plan says which one binds:

==================  =======================================================================================
risk at stop        margin x L x (stop distance + slippage allowance + fees) <= the risk budget
liquidation buffer  stop distance + slippage allowance <= 50% of the liquidation distance (exact ISOLATED
                    liquidation price from ``margin.py``, isolated margin = the margin)
exposure            margin x L <= the notional allowed (the mandate's room, and 25% of account equity, D1)
mandate maximum     L <= the mandate's ``max_leverage`` (the account maximum, 10x, for the manual ticket)
setup grade         L <= the grade's ceiling (§2.3), when graded
exchange            L <= the bracket's maximum leverage for the notional
==================  =======================================================================================

``plan_leveraged_order`` sizes (the card dry run, the orchestrator); ``check_leveraged_order`` verifies an order
that is already sized (the mandate gate and ``GuardedOrderDispatcher``). Both value an order with the same helpers,
so an order the plan sized always passes the check (the #86 lesson: sizing and checking value an order alike).
"""

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from cryptonite_schemas.runtime_settings import MARGIN_PAIR_SETTINGS

from cryptonite_execution.derivatives import MarginMode
from cryptonite_execution.margin import bracket_source, calculate_liquidation_price, get_bracket
from cryptonite_execution.spot_margin import liquidation_price as spot_margin_liquidation_price
from cryptonite_execution.spot_margin import size_margin_borrow

ACCOUNT_MAX_LEVERAGE = 10  # ADR 0009 D2: the manual ticket and the dispatcher (was 20)
ACCOUNT_EXPOSURE_CAP = Decimal("0.25")  # D1: perp notional <= 25% of account equity
DEFAULT_SLIPPAGE_ALLOWANCE = Decimal("0.003")  # an XRP stop filled 0.28% past its level; operator setting
DEFAULT_FEE_ROUND_TRIP = Decimal("0.001")  # 2 x the USD-M taker fee (0.05%)
LIQUIDATION_BUFFER_FRACTION = Decimal("0.5")  # the stop fires before half the liquidation distance is used
MANUAL_RISK_PER_TRADE_PCT = Decimal("2")  # manual orders with a stop: the D6 cap, of account equity
# §2.3: the leverage each setup grade allows by default (operator settings, lowering free, raising with TOTP)
DEFAULT_GRADE_CEILINGS = {"A": 10, "B": 5, "C": 2, "NO_TRADE": 0}
MAX_SCAN_LEVERAGE = 125  # the widest Binance bracket; ceilings are scanned up to it for display
QUANTITY_STEP = Decimal("0.00000001")


class Ceiling(StrEnum):
    """The six ceilings, in the order the card lists them (ties bind in this order)."""

    RISK_AT_STOP = "risk at stop"
    LIQUIDATION_BUFFER = "liquidation buffer"
    EXPOSURE = "exposure"
    MAXIMUM = "mandate maximum"
    SETUP_GRADE = "setup grade"
    EXCHANGE = "exchange"


class LeverageReject(StrEnum):
    STOP_REQUIRED = "STOP_REQUIRED"
    STOP_WRONG_SIDE = "STOP_WRONG_SIDE"
    RISK_AT_STOP_EXCEEDED = "RISK_AT_STOP_EXCEEDED"
    LIQUIDATION_BUFFER_TOO_THIN = "LIQUIDATION_BUFFER_TOO_THIN"
    EXPOSURE_LIMIT_REACHED = "EXPOSURE_LIMIT_REACHED"
    LEVERAGE_ABOVE_BRACKET = "LEVERAGE_ABOVE_BRACKET"
    LEVERAGE_ABOVE_MAXIMUM = "LEVERAGE_ABOVE_MAXIMUM"
    ORDER_BELOW_MIN_NOTIONAL = "ORDER_BELOW_MIN_NOTIONAL"
    SETUP_NO_TRADE = "SETUP_NO_TRADE"
    NO_LEVERAGE_BRACKETS = "NO_LEVERAGE_BRACKETS"
    STALE_LEVERAGE_BRACKETS = "STALE_LEVERAGE_BRACKETS"
    INVALID_ORDER = "INVALID_ORDER"
    MARGIN_PAIR_NOT_SUPPORTED = "MARGIN_PAIR_NOT_SUPPORTED"
    MARGIN_BORROW_LIMIT = "MARGIN_BORROW_LIMIT"


# The refusal when a ceiling allows not even 1x
_CEILING_REJECT = {
    Ceiling.RISK_AT_STOP: LeverageReject.RISK_AT_STOP_EXCEEDED,
    Ceiling.LIQUIDATION_BUFFER: LeverageReject.LIQUIDATION_BUFFER_TOO_THIN,
    Ceiling.EXPOSURE: LeverageReject.EXPOSURE_LIMIT_REACHED,
    Ceiling.MAXIMUM: LeverageReject.LEVERAGE_ABOVE_MAXIMUM,
    Ceiling.SETUP_GRADE: LeverageReject.SETUP_NO_TRADE,
    Ceiling.EXCHANGE: LeverageReject.LEVERAGE_ABOVE_BRACKET,
}


@dataclass(frozen=True)
class LeverageLimits:
    """What the order is held to. ``risk_budget_usd``: the loss at the stop one trade may take (mandate budget x
    ``max_risk_per_trade_pct``). ``max_notional_usd``: the notional it may add (the mandate's room and 25% of
    account equity, whichever is smaller)."""

    risk_budget_usd: Decimal
    max_notional_usd: Decimal
    max_leverage: int
    grade_ceiling: int | None = None
    slippage_pct: Decimal = DEFAULT_SLIPPAGE_ALLOWANCE
    fee_pct: Decimal = DEFAULT_FEE_ROUND_TRIP
    maximum_label: str = Ceiling.MAXIMUM.value  # "account maximum" on the manual ticket
    min_notional_usd: Decimal = Decimal("0")  # the dispatcher's 1%-of-equity minimum order


@dataclass(frozen=True)
class LeveragePlan:
    instrument_id: str
    side: str  # BUY = long, SELL = short
    reject: str | None
    leverage: int  # 0 when refused
    requested_leverage: int | None
    allowed_max_leverage: int  # the lowest ceiling, at most the maximum
    binding_ceiling: str | None  # which ceiling the allowed maximum comes from
    capped: bool  # a request above the allowed maximum was cut to it
    ceilings: dict[str, int] = field(default_factory=dict)  # each ceiling's own maximum
    margin_usd: Decimal = Decimal("0")
    notional_usd: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    stop_price: Decimal | None = None
    stop_distance_pct: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None
    liquidation_distance_pct: Decimal | None = None
    buffer_used_pct: Decimal | None = None  # (stop distance + slippage) / liquidation distance, as %
    loss_at_stop_usd: Decimal = Decimal("0")  # notional x (stop distance + slippage + fees)
    loss_at_stop_pct: Decimal | None = None  # of the budget given
    loss_if_liquidated_usd: Decimal = Decimal("0")  # the posted margin
    funding_8h_usd: Decimal | None = None  # cash flow per 8 h at the given rate: negative = the position pays
    margin_cut: bool = False  # the margin was reduced to fit the risk budget / room (sizing only)

    @property
    def ok(self) -> bool:
        return self.reject is None

    @property
    def leverage_label(self) -> str:
        """``7x (risk at stop)`` when a ceiling cut the request, else ``7x``."""
        if not self.ok:
            return "refused"
        return f"{self.leverage}x ({self.binding_ceiling})" if self.capped else f"{self.leverage}x"

    def as_dict(self) -> dict:
        def s(v):
            return None if v is None else str(v)

        return {
            "instrument_id": self.instrument_id, "side": self.side, "reject": self.reject, "ok": self.ok,
            "leverage": self.leverage, "leverage_label": self.leverage_label, "margin_mode": MarginMode.ISOLATED.value,
            "requested_leverage": self.requested_leverage, "allowed_max_leverage": self.allowed_max_leverage,
            "binding_ceiling": self.binding_ceiling, "capped": self.capped, "ceilings": dict(self.ceilings),
            "margin_usd": s(self.margin_usd), "notional_usd": s(self.notional_usd), "quantity": s(self.quantity),
            "entry_price": s(self.entry_price), "stop_price": s(self.stop_price),
            "stop_distance_pct": s(self.stop_distance_pct), "liquidation_price": s(self.liquidation_price),
            "liquidation_distance_pct": s(self.liquidation_distance_pct), "buffer_used_pct": s(self.buffer_used_pct),
            "loss_at_stop_usd": s(self.loss_at_stop_usd), "loss_at_stop_pct": s(self.loss_at_stop_pct),
            "loss_if_liquidated_usd": s(self.loss_if_liquidated_usd), "funding_8h_usd": s(self.funding_8h_usd),
            "margin_cut": self.margin_cut,
        }


def _is_long(side: str) -> bool:
    return side.upper() == "BUY"


def stop_distance(side: str, entry: Decimal, stop: Decimal) -> Decimal | None:
    """|entry - stop| / entry as a fraction, or None when the stop is on the wrong side of the entry."""
    if entry <= 0 or stop <= 0:
        return None
    gap = (entry - stop) if _is_long(side) else (stop - entry)
    return gap / entry if gap > 0 else None


def liquidation_distance(instrument_id: str, side: str, quantity: Decimal, entry: Decimal,
                         isolated_margin: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """(ISOLATED liquidation price, its distance from the entry as a fraction). (None, None) when no positive price
    liquidates the position. Raises ValueError without a bracket table."""
    liq = calculate_liquidation_price(instrument_id, side.upper(), quantity, entry, isolated_margin, MarginMode.ISOLATED)
    if liq is None:
        return None, None
    return liq, abs(entry - liq) / entry


def _liquidation_ok(instrument_id: str, side: str, entry: Decimal, notional: Decimal, leverage: int,
                    stop_gap: Decimal) -> bool:
    if notional <= 0:
        return True
    _, dist = liquidation_distance(instrument_id, side, notional / entry, entry, notional / Decimal(leverage))
    return dist is None or stop_gap <= LIQUIDATION_BUFFER_FRACTION * dist


def _bracket_ok(instrument_id: str, notional: Decimal, leverage: int) -> bool:
    try:
        return leverage <= get_bracket(instrument_id, notional).max_leverage
    except ValueError:  # above the table's top tier
        return False


def _scan(ok, top: int = MAX_SCAN_LEVERAGE) -> int:
    """The highest L in 1..top with ok(L) true for every L up to it (both conditions tighten as L grows)."""
    best = 0
    for lev in range(1, top + 1):
        if not ok(lev):
            break
        best = lev
    return best


def _floor_int(x: Decimal) -> int:
    return max(0, int(x.to_integral_value(rounding=ROUND_DOWN)))


def plan_leveraged_order(
    instrument_id: str,
    side: str,
    entry_price: Decimal,
    stop_price: Decimal | None,
    margin_usd: Decimal,
    limits: LeverageLimits,
    *,
    requested_leverage: int | None = None,
    budget_usd: Decimal | None = None,
    funding_rate: Decimal | None = None,
    shrink_margin_to_fit: bool = False,
) -> LeveragePlan:
    """Size a leveraged entry. ``requested_leverage`` None: the highest the ceilings allow. ``shrink_margin_to_fit``
    (sizing): a margin so large that not even 1x fits the risk budget or the room is cut until 1x fits, as spot
    entries are sized down to the room; otherwise such a margin is refused."""
    side = side.upper()

    def refused(code: str, **kw) -> LeveragePlan:
        return LeveragePlan(instrument_id, side, code, 0, requested_leverage, 0, kw.pop("binding", None), False,
                            entry_price=entry_price, stop_price=stop_price, margin_usd=margin_usd, **kw)

    if side not in ("BUY", "SELL") or entry_price <= 0 or margin_usd <= 0:
        return refused(LeverageReject.INVALID_ORDER)
    if stop_price is None:
        return refused(LeverageReject.STOP_REQUIRED)
    d = stop_distance(side, entry_price, stop_price)
    if d is None:
        return refused(LeverageReject.STOP_WRONG_SIDE)
    if (bracket_reject := bracket_source(instrument_id).entry_reject) is not None:
        return refused(bracket_reject)

    stop_gap = d + limits.slippage_pct  # how far the fill can be from the entry when the stop fires
    cost = stop_gap + limits.fee_pct  # loss at the stop per unit of notional
    margin_cut = False
    if shrink_margin_to_fit:
        fits_1x = min(limits.risk_budget_usd / cost, limits.max_notional_usd)
        if margin_usd > fits_1x:
            margin_usd, margin_cut = max(Decimal("0"), fits_1x), True
            if margin_usd <= 0:
                return refused(LeverageReject.EXPOSURE_LIMIT_REACHED, binding=Ceiling.EXPOSURE.value)

    ceilings = {
        Ceiling.RISK_AT_STOP.value: _floor_int(limits.risk_budget_usd / (margin_usd * cost)),
        Ceiling.LIQUIDATION_BUFFER.value: _scan(
            lambda lev: _liquidation_ok(instrument_id, side, entry_price, margin_usd * lev, lev, stop_gap)),
        Ceiling.EXPOSURE.value: _floor_int(limits.max_notional_usd / margin_usd),
        limits.maximum_label: min(limits.max_leverage, ACCOUNT_MAX_LEVERAGE),
    }
    if limits.grade_ceiling is not None:
        ceilings[Ceiling.SETUP_GRADE.value] = limits.grade_ceiling
    ceilings[Ceiling.EXCHANGE.value] = _scan(lambda lev: _bracket_ok(instrument_id, margin_usd * lev, lev))
    binding = min(ceilings, key=lambda k: ceilings[k])  # dicts keep insertion order: ties bind in table order
    allowed = ceilings[binding]
    if allowed < 1:
        code = _CEILING_REJECT.get(Ceiling(binding) if binding in Ceiling._value2member_map_ else Ceiling.MAXIMUM)
        return refused(code, binding=binding, ceilings=ceilings, margin_cut=margin_cut)

    leverage = allowed if requested_leverage is None else max(1, min(int(requested_leverage), allowed))
    capped = requested_leverage is not None and requested_leverage > allowed
    quantity = (margin_usd * leverage / entry_price).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
    notional = quantity * entry_price
    isolated_margin = notional / Decimal(leverage)
    liq, dist = liquidation_distance(instrument_id, side, quantity, entry_price, isolated_margin) if quantity > 0 \
        else (None, None)
    if notional < limits.min_notional_usd:
        return refused(LeverageReject.ORDER_BELOW_MIN_NOTIONAL, binding=binding, ceilings=ceilings, margin_cut=margin_cut)
    loss = notional * cost
    sign = Decimal("1") if _is_long(side) else Decimal("-1")
    return LeveragePlan(
        instrument_id=instrument_id, side=side, reject=None, leverage=leverage,
        requested_leverage=requested_leverage, allowed_max_leverage=allowed, binding_ceiling=binding, capped=capped,
        ceilings=ceilings, margin_usd=isolated_margin, notional_usd=notional, quantity=quantity,
        entry_price=entry_price, stop_price=stop_price, stop_distance_pct=d * 100,
        liquidation_price=liq, liquidation_distance_pct=dist * 100 if dist is not None else None,
        buffer_used_pct=stop_gap / dist * 100 if dist else None,
        loss_at_stop_usd=loss, loss_at_stop_pct=loss / budget_usd * 100 if budget_usd else None,
        loss_if_liquidated_usd=isolated_margin,
        funding_8h_usd=-sign * notional * funding_rate if funding_rate is not None else None,
        margin_cut=margin_cut,
    )


def plan_spot_margin_order(
    instrument_id: str,
    side: str,
    entry_price: Decimal,
    stop_price: Decimal | None,
    margin_usd: Decimal,
    limits: LeverageLimits,
    *,
    base_asset: str,
    requested_leverage: int | None = None,
    pair_settings: dict[str, Decimal] | None = None,
    quote_asset: str = "USDT",
    budget_usd: Decimal | None = None,
    shrink_margin_to_fit: bool = False,
) -> LeveragePlan:
    """Plan an isolated spot-margin order using pair risk levels and borrow caps.

    Unlike a perpetual, this path has no exchange leverage bracket table. It uses the
    configured pair maximum, the spot-margin liquidation equation and the per-side
    displayed borrow cap. A pair with no configuration or a borrow above its cap is
    refused. The result keeps the existing plan shape for shared dry-run consumers.
    """
    side = side.upper()

    def refused(code: str, **kw) -> LeveragePlan:
        return LeveragePlan(instrument_id, side, code, 0, requested_leverage, 0, kw.pop("binding", None), False,
                            entry_price=entry_price, stop_price=stop_price, margin_usd=margin_usd, **kw)

    if side not in ("BUY", "SELL") or entry_price <= 0 or margin_usd <= 0:
        return refused(LeverageReject.INVALID_ORDER)
    pair = instrument_id.split("-", 1)[0].split(".", 1)[0].upper()
    config = pair_settings if pair_settings is not None else MARGIN_PAIR_SETTINGS.get(pair)
    if not config:
        return refused(LeverageReject.MARGIN_PAIR_NOT_SUPPORTED)
    if stop_price is None:
        return refused(LeverageReject.STOP_REQUIRED)
    distance = stop_distance(side, entry_price, stop_price)
    if distance is None:
        return refused(LeverageReject.STOP_WRONG_SIDE)

    max_leverage = min(int(config["max_leverage"]), limits.max_leverage, ACCOUNT_MAX_LEVERAGE)
    requested = max(1, min(requested_leverage or max_leverage, max_leverage))
    capped = requested_leverage is not None and requested_leverage > max_leverage
    leverage = requested
    stop_gap = distance + limits.slippage_pct
    cost = stop_gap + limits.fee_pct
    # A long's purchased notional is collateral * leverage. A short borrows and sells
    # only the base worth collateral * (leverage - 1); its collateral remains quote.
    # Keep exposure/risk sizing aligned with the order quantity that is actually sent.
    exposure_per_margin = Decimal(leverage) if side == "BUY" else Decimal(leverage - 1)
    if exposure_per_margin <= 0:
        return refused(LeverageReject.MARGIN_BORROW_LIMIT, binding=f"max_borrow_{base_asset}")
    original_margin = margin_usd
    if shrink_margin_to_fit:
        max_notional = min(limits.max_notional_usd, limits.risk_budget_usd / cost)
        if max_notional <= 0:
            return refused(LeverageReject.EXPOSURE_LIMIT_REACHED)
        # Floor to cents: at the exact risk boundary, Decimal rounding can push the loss a hair over the budget
        margin_usd = min(margin_usd, (max_notional / exposure_per_margin).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    sizing = size_margin_borrow("LONG" if side == "BUY" else "SHORT", margin_usd,
                                entry_price, Decimal(leverage))
    borrow_asset = quote_asset if side == "BUY" else base_asset
    borrow_field = f"max_borrow_{borrow_asset}"
    borrow_amount = sizing.borrowed_quote if side == "BUY" else sizing.borrowed_base
    borrow_cap = config.get(borrow_field, Decimal("0"))
    if borrow_amount > borrow_cap:
        return refused(LeverageReject.MARGIN_BORROW_LIMIT, binding=borrow_field)

    try:
        liq = spot_margin_liquidation_price(
            "LONG" if side == "BUY" else "SHORT", base_qty=sizing.base_qty, quote_qty=sizing.quote_qty,
            borrowed_base=sizing.borrowed_base, borrowed_quote=sizing.borrowed_quote,
            interest_base=Decimal("0"), interest_quote=Decimal("0"),
            liquidation_level=config["liquidation_level"], entry_price=entry_price,
        )
    except ValueError:
        return refused(LeverageReject.LIQUIDATION_BUFFER_TOO_THIN)
    liq_distance = abs(entry_price - liq) / entry_price
    if stop_gap > LIQUIDATION_BUFFER_FRACTION * liq_distance:
        return refused(LeverageReject.LIQUIDATION_BUFFER_TOO_THIN)

    notional = borrow_amount * entry_price if side == "SELL" else sizing.base_qty * entry_price
    if notional > limits.max_notional_usd:
        return refused(LeverageReject.EXPOSURE_LIMIT_REACHED)
    loss = notional * cost
    if loss > limits.risk_budget_usd:
        return refused(LeverageReject.RISK_AT_STOP_EXCEEDED)
    if notional < limits.min_notional_usd:
        return refused(LeverageReject.ORDER_BELOW_MIN_NOTIONAL)
    return LeveragePlan(
        instrument_id=instrument_id, side=side, reject=None, leverage=leverage,
        requested_leverage=requested_leverage, allowed_max_leverage=max_leverage,
        binding_ceiling="pair maximum", capped=capped, ceilings={"pair maximum": max_leverage},
        margin_usd=margin_usd, notional_usd=notional,
        quantity=(sizing.borrowed_base if side == "SELL" else sizing.base_qty)
        .quantize(QUANTITY_STEP, rounding=ROUND_DOWN),
        entry_price=entry_price, stop_price=stop_price, stop_distance_pct=distance * 100,
        liquidation_price=liq, liquidation_distance_pct=liq_distance * 100,
        buffer_used_pct=stop_gap / liq_distance * 100,
        loss_at_stop_usd=loss,
        loss_at_stop_pct=loss / budget_usd * 100 if budget_usd else None,
        loss_if_liquidated_usd=margin_usd,
        margin_cut=margin_usd < original_margin,
    )


def check_leveraged_order(
    instrument_id: str,
    side: str,
    quantity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal | None,
    leverage: int,
    *,
    risk_budget_usd: Decimal | None,
    slippage_pct: Decimal = DEFAULT_SLIPPAGE_ALLOWANCE,
    fee_pct: Decimal = DEFAULT_FEE_ROUND_TRIP,
) -> str | None:
    """Verify a sized perp entry; the reject code, or None. ``stop_price`` None is not judged here (the caller decides
    whether a stop is required). The same valuation as ``plan_leveraged_order``."""
    if quantity <= 0 or entry_price <= 0 or leverage < 1:
        return LeverageReject.INVALID_ORDER.value
    notional = quantity * entry_price
    if not _bracket_ok(instrument_id, notional, leverage):
        return LeverageReject.LEVERAGE_ABOVE_BRACKET.value
    if stop_price is None:
        return None
    d = stop_distance(side, entry_price, stop_price)
    if d is None:
        return LeverageReject.STOP_WRONG_SIDE.value
    stop_gap = d + slippage_pct
    if not _liquidation_ok(instrument_id, side, entry_price, notional, leverage, stop_gap):
        return LeverageReject.LIQUIDATION_BUFFER_TOO_THIN.value
    if risk_budget_usd is not None and notional * (stop_gap + fee_pct) > risk_budget_usd:
        return LeverageReject.RISK_AT_STOP_EXCEEDED.value
    return None
