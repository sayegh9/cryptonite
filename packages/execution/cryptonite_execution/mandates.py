"""Mandate check for AI-originated orders (AI-first blueprint §4.4, A5.1).

Runs in the orchestrator's AI order path **before** ``GuardedOrderDispatcher``. It can only reject;
it never resizes an order and never skips the dispatcher, so the 1–25% equity clamp, the kill switches,
the daily breaker and the perp guards stay exactly as they are (§10 invariant 2: mandate checks are
additive).

``check_mandate`` is pure: the caller supplies the mandate, the order and the mandate's current
exposure. ``max_entry_quantity`` is for the caller that derives an entry's quantity from its
allocation: it sizes the entry down to the room left under the position limit and the budget, using the
same worst-case notional the check uses, so a correctly sized entry never fails as a breach. REDUCE_RISK is always allowed for what the mandate itself holds, even when it is halted
(§10 invariant 3).
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from cryptonite_schemas.mandate import (
    MAX_PERP_LEVERAGE_CAP,
    ActionClass,
    AutonomyLevel,
    Mandate,
    MandateReject,
    MandateStatus,
    canonical_instrument,
    is_perp_instrument,
)

# Market buys are valued like the dispatcher reserves them: ask + 2% collar + 0.5% slippage
MARKET_BUFFER = Decimal("0.025")
QUANTITY_STEP = Decimal("0.00000001")  # finer than any exchange lot size
# The dispatcher resizes by conviction (OVERWEIGHT x1.5); the mandate check prices that in up front
CONVICTION_MULTIPLIER = {"OVERWEIGHT": Decimal("1.5"), "UNDERWEIGHT": Decimal("0.5")}


@dataclass(frozen=True)
class MandateOrder:
    instrument_id: str
    side: str  # BUY | SELL
    quantity: Decimal
    ref_price: Decimal | None  # limit price, else the live ask (buy) / bid (sell)
    order_type: str = "MARKET"
    reduce_only: bool = False
    leverage: int | None = None
    conviction_level: str | None = None
    margin_mode: str | None = None  # perps: None means the ISOLATED default

    @property
    def is_perp(self) -> bool:
        return is_perp_instrument(self.instrument_id)


@dataclass(frozen=True)
class MandateExposure:
    """What the mandate already holds, valued in USD."""

    gross_usd: Decimal = Decimal("0")  # all open positions + working entry orders of the mandate
    instrument_usd: Decimal = Decimal("0")  # the part of gross_usd in this order's instrument
    instrument_qty: Decimal = Decimal("0")  # the mandate's own position size in this instrument (abs)
    instrument_side: str | None = None  # LONG | SHORT for perps; LONG for spot
    other_owner: bool = False  # another instance (manual, legacy AI, another mandate) holds or works it


def action_class(order: MandateOrder) -> ActionClass:
    """Spot is long-only (the dispatcher rejects spot shorts), so a spot SELL only reduces.
    A perp order reduces only when it is reduce-only."""
    if order.is_perp:
        return ActionClass.REDUCE_RISK if order.reduce_only else ActionClass.ADD_RISK
    return ActionClass.REDUCE_RISK if order.side.upper() == "SELL" else ActionClass.ADD_RISK


def order_notional(order: MandateOrder) -> Decimal | None:
    """Worst-case USD notional the order can add, including the conviction resize."""
    if order.ref_price is None or order.ref_price <= 0:
        return None
    qty = order.quantity * CONVICTION_MULTIPLIER.get((order.conviction_level or "").upper(), Decimal("1"))
    buffer = MARKET_BUFFER if order.order_type.upper() == "MARKET" else Decimal("0")
    return qty * order.ref_price * (Decimal("1") + buffer)


def entry_room_usd(mandate: Mandate, exposure: MandateExposure) -> Decimal:
    """USD a new entry may still add: the room under the instrument's position limit and under the
    budget, whichever is smaller (0 when either is used up)."""
    position_room = mandate.capital_budget_usd * mandate.max_position_pct / Decimal("100") - exposure.instrument_usd
    budget_room = mandate.capital_budget_usd - exposure.gross_usd
    return max(Decimal("0"), min(position_room, budget_room))


def max_entry_quantity(mandate: Mandate, exposure: MandateExposure, ref_price: Decimal, *,
                       order_type: str = "MARKET", conviction_level: str | None = None) -> Decimal:
    """The largest quantity whose worst-case notional (``order_notional``: market buffer and conviction
    resize included) still fits in ``entry_room_usd``, so an entry sized to it passes the
    position-limit and budget checks."""
    if ref_price is None or ref_price <= 0:
        return Decimal("0")
    per_unit = order_notional(MandateOrder("X", "BUY", Decimal("1"), ref_price, order_type=order_type,
                                           conviction_level=conviction_level))
    # rounded down: a quotient rounded up in its last digit must not put the entry a hair over the room
    return (entry_room_usd(mandate, exposure) / per_unit).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)


def check_mandate(
    mandate: Mandate | None,
    order: MandateOrder,
    exposure: MandateExposure,
    *,
    approval_ok: bool,
) -> MandateReject | None:
    """None when the order is inside the mandate, else the reject code.

    ``approval_ok``: the command carries a valid operator approval for this ADD_RISK action (or, at L2,
    a veto window that ran out with policy PROCEED). L3 needs none.
    """
    if mandate is None:
        return MandateReject.REQUIRED

    if action_class(order) is ActionClass.REDUCE_RISK:
        # Always allowed (halted, over budget, any level) for what the mandate itself holds
        qty = order.quantity
        if not order.reduce_only:  # the dispatcher resizes spot sells by conviction too
            qty = qty * CONVICTION_MULTIPLIER.get((order.conviction_level or "").upper(), Decimal("1"))
        if qty > exposure.instrument_qty:
            return MandateReject.POSITION_NOT_OWNED
        if order.is_perp and exposure.instrument_side is not None:
            closing_side = "SELL" if exposure.instrument_side == "LONG" else "BUY"
            if order.side.upper() != closing_side:
                return MandateReject.POSITION_NOT_OWNED
        return None

    if mandate.status is not MandateStatus.ACTIVE:
        return MandateReject.HALTED
    if not mandate.allows_instrument(order.instrument_id):
        return MandateReject.INSTRUMENT_NOT_ALLOWED
    if order.is_perp:
        if not mandate.allow_perps or mandate.autonomy_level < AutonomyLevel.L3:
            return MandateReject.PERPS_NOT_ALLOWED
        if (order.leverage or 1) > min(mandate.max_leverage, MAX_PERP_LEVERAGE_CAP):
            return MandateReject.LEVERAGE_EXCEEDED
        if (order.margin_mode or "ISOLATED").upper() != "ISOLATED":
            return MandateReject.MARGIN_MODE_NOT_ALLOWED
    if exposure.other_owner:
        return MandateReject.INSTRUMENT_CONFLICT
    notional = order_notional(order)
    if notional is None:
        return MandateReject.NO_REFERENCE_PRICE
    if exposure.gross_usd + notional > mandate.capital_budget_usd:
        return MandateReject.BUDGET_EXCEEDED
    if exposure.instrument_usd + notional > mandate.capital_budget_usd * mandate.max_position_pct / Decimal("100"):
        return MandateReject.POSITION_PCT_EXCEEDED
    if mandate.autonomy_level < AutonomyLevel.L3 and not approval_ok:
        return MandateReject.APPROVAL_REQUIRED
    return None


def same_instrument(a: str, b: str) -> bool:
    return canonical_instrument(a) == canonical_instrument(b)
