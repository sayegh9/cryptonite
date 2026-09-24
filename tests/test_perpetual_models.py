"""Contract tests for perpetual futures domain models."""

import uuid
from decimal import Decimal

import pytest
from cryptonite_execution.derivatives import (
    FundingRateRecord,
    MarginMode,
    PerpetualPosition,
    PositionSide,
)
from pydantic import ValidationError

PERP = "BTCUSDT-PERP.BINANCE"


def _position(**overrides) -> PerpetualPosition:
    fields = {
        "position_id": uuid.uuid4(),
        "account_id": "ACC",
        "instance_id": "INST",
        "instrument_id": PERP,
        "margin_mode": MarginMode.ISOLATED,
        "leverage": 10,
        "signed_qty": Decimal("-0.5"),
        "entry_price": Decimal("60000"),
        "isolated_margin": Decimal("3000"),
    }
    fields.update(overrides)
    return PerpetualPosition(**fields)


@pytest.mark.parametrize(
    ("signed_qty", "side"),
    [(Decimal("0.5"), PositionSide.LONG), (Decimal("-0.5"), PositionSide.SHORT), (Decimal("0"), PositionSide.FLAT)],
)
def test_side_derives_from_signed_quantity(signed_qty, side):
    pos = _position(signed_qty=signed_qty)
    assert pos.side is side
    assert pos.quantity == abs(signed_qty)


def test_rejects_spot_instrument():
    with pytest.raises(ValidationError, match="not a perpetual"):
        _position(instrument_id="BTCUSDT.BINANCE")


@pytest.mark.parametrize("leverage", [0, 126])
def test_rejects_leverage_out_of_exchange_bounds(leverage):
    with pytest.raises(ValidationError):
        _position(leverage=leverage)


def test_cross_mode_forbids_isolated_margin():
    with pytest.raises(ValidationError, match="CROSS"):
        _position(margin_mode=MarginMode.CROSS)
    assert _position(margin_mode=MarginMode.CROSS, isolated_margin=Decimal("0")).isolated_margin == 0


def test_open_position_requires_entry_price():
    with pytest.raises(ValidationError, match="entry_price"):
        _position(entry_price=Decimal("0"))
    assert _position(signed_qty=Decimal("0"), entry_price=Decimal("0")).side is PositionSide.FLAT


def test_funding_record_validation():
    rec = FundingRateRecord(
        instrument_id=PERP,
        funding_time_ns=1_758_700_800_000_000_000,
        funding_rate=Decimal("0.0001"),
        mark_price=Decimal("60000"),
        funding_interval_hours=4,
        source="BINANCE_REST",
    )
    assert rec.funding_rate == Decimal("0.0001")
    with pytest.raises(ValidationError):
        FundingRateRecord.model_validate({**rec.model_dump(), "funding_interval_hours": 3})
    with pytest.raises(ValidationError, match="not a perpetual"):
        FundingRateRecord.model_validate({**rec.model_dump(), "instrument_id": "BTCUSDT.BINANCE"})
