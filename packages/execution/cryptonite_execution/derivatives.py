"""Perpetual futures domain models (USD-M linear, one-way position mode).

Money is Decimal throughout. Positions carry a signed quantity (>0 long, <0 short);
``side`` is derived for the wire/DB, which stores absolute quantity plus side.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from cryptonite_schemas.identifiers import parse_instrument_id
from pydantic import BaseModel, Field, field_validator, model_validator

MAX_EXCHANGE_LEVERAGE = 125


class MarginMode(StrEnum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


def _require_perp(instrument_id: str) -> str:
    if not parse_instrument_id(instrument_id)[2]:
        raise ValueError(f"not a perpetual instrument: {instrument_id}")
    return instrument_id


class FundingRateRecord(BaseModel):
    """One settled funding event for an instrument (mirrors the funding_rates table)."""

    instrument_id: str
    funding_time_ns: int = Field(gt=0)
    funding_rate: Decimal
    mark_price: Decimal | None = Field(default=None, gt=0)
    funding_interval_hours: Literal[1, 2, 4, 8] | None = None
    source: Literal["BINANCE_REST", "STREAM_SETTLEMENT"]

    _perp_only = field_validator("instrument_id")(_require_perp)


class PerpetualPosition(BaseModel):
    """Perpetual position state (mirrors paper_positions perp rows).

    ``liquidation_price`` and ``maintenance_margin`` are snapshots written with each
    update; the engine recomputes them from mark price and never trusts them as input.
    """

    position_id: UUID
    account_id: str
    instance_id: str
    instrument_id: str
    margin_mode: MarginMode
    leverage: int = Field(ge=1, le=MAX_EXCHANGE_LEVERAGE)
    signed_qty: Decimal
    entry_price: Decimal = Field(ge=0)
    isolated_margin: Decimal = Field(default=Decimal("0"), ge=0)
    realized_pnl: Decimal = Decimal("0")
    liquidation_price: Decimal | None = Field(default=None, ge=0)
    maintenance_margin: Decimal | None = Field(default=None, ge=0)

    _perp_only = field_validator("instrument_id")(_require_perp)

    @model_validator(mode="after")
    def _check_margin_consistency(self) -> "PerpetualPosition":
        if self.margin_mode is MarginMode.CROSS and self.isolated_margin != 0:
            raise ValueError("isolated_margin must be 0 in CROSS mode")
        if self.signed_qty != 0 and self.entry_price <= 0:
            raise ValueError("open position requires entry_price > 0")
        return self

    @property
    def side(self) -> PositionSide:
        if self.signed_qty > 0:
            return PositionSide.LONG
        if self.signed_qty < 0:
            return PositionSide.SHORT
        return PositionSide.FLAT

    @property
    def quantity(self) -> Decimal:
        """Absolute quantity, as stored in paper_positions.quantity."""
        return abs(self.signed_qty)


class FundingPaymentEvent(BaseModel):
    """One funding settlement applied to an open perpetual position."""

    account_id: str
    instance_id: str
    instrument_id: str
    margin_mode: MarginMode
    signed_qty: Decimal
    mark_price: Decimal
    funding_rate: Decimal
    cash_flow: Decimal  # -signed_qty * mark_price * funding_rate
    cumulative_funding: Decimal  # position total after this payment
    funding_time_ns: int


class LiquidationEvent(BaseModel):
    """A forced close of a perpetual position whose mark price breached its liquidation price."""

    client_order_id: str
    account_id: str
    instance_id: str
    instrument_id: str
    margin_mode: MarginMode
    side: PositionSide  # side of the liquidated position
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal
    bankruptcy_price: Decimal | None  # ISOLATED only
    fill_price: Decimal  # mark, or the bankruptcy price when mark gapped through it
    realized_pnl: Decimal  # gross, at fill_price
    trading_fee: Decimal
    clearance_fee: Decimal
    shortfall_covered: Decimal  # cash credited so the loss stops at the margin (isolated) or zero equity (cross)
    insurance_fund_deficit: Decimal  # shortfall_covered + (mark gap beyond bankruptcy price) * quantity
