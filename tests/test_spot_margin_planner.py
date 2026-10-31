"""Pair-specific spot-margin sizing uses exact Decimals and refuses unsupported risk."""

from decimal import ROUND_DOWN
from decimal import Decimal as D

import pytest
from cryptonite_execution.leverage import LeverageLimits, LeverageReject, plan_spot_margin_order
from cryptonite_schemas.runtime_settings import MARGIN_PAIR_SETTINGS


def limits(*, budget="10000", room="10000", leverage=10):
    return LeverageLimits(D(budget), D(room), leverage, slippage_pct=D("0.003"), fee_pct=D("0.001"))


@pytest.mark.parametrize(("side", "stop", "expected_liquidation"), [
    ("BUY", "90", D("52.5")),
    ("SELL", "110", D("190.4761904761904761904761905")),
])
def test_margin_planner_uses_pair_liquidation_levels_without_perp_brackets(side, stop, expected_liquidation):
    result = plan_spot_margin_order("BTCUSDT-MARGIN.BINANCE", side, D("100"), D(stop), D("10"), limits(),
                                    base_asset="BTC", requested_leverage=2)
    assert result.ok
    assert result.leverage == 2
    assert result.liquidation_price == pytest.approx(expected_liquidation)


def test_pair_max_leverage_is_the_ceiling_and_no_exchange_brackets_are_read():
    result = plan_spot_margin_order("WLDUSDT-MARGIN.BINANCE", "BUY", D("1"), D("0.97"), D("10"),
                                    limits(leverage=10), base_asset="WLD", requested_leverage=10)
    assert result.ok
    assert (result.leverage, result.allowed_max_leverage, result.capped) == (5, 5, True)


def test_stop_outside_pair_liquidation_buffer_is_refused():
    result = plan_spot_margin_order("BTCUSDT-MARGIN.BINANCE", "BUY", D("100"), D("60"), D("10"),
                                    limits(), base_asset="BTC", requested_leverage=2)
    assert result.reject == LeverageReject.LIQUIDATION_BUFFER_TOO_THIN


def test_pair_borrow_cap_refuses_only_the_side_that_uses_it():
    config = dict(MARGIN_PAIR_SETTINGS["BTCUSDT"])
    config["max_borrow_USDT"] = D("0")
    long = plan_spot_margin_order("BTCUSDT-MARGIN", "BUY", D("100"), D("90"), D("10"), limits(),
                                  base_asset="BTC", requested_leverage=2, pair_settings=config)
    short = plan_spot_margin_order("BTCUSDT-MARGIN", "SELL", D("100"), D("110"), D("10"), limits(),
                                   base_asset="BTC", requested_leverage=2, pair_settings=config)
    assert long.reject == LeverageReject.MARGIN_BORROW_LIMIT
    assert short.ok


def test_short_plan_quantity_exposure_and_stop_loss_match_borrowed_base():
    result = plan_spot_margin_order(
        "BTCUSDT-MARGIN.BINANCE", "SELL", D("100"), D("110"), D("10"), limits(),
        base_asset="BTC", requested_leverage=2,
    )
    assert result.ok
    # At 2x the short borrows BTC worth one collateral unit; unlike a long,
    # it does not sell collateral * leverage worth of base.
    assert result.quantity == D("0.1")
    assert result.notional_usd == D("10")
    assert result.loss_at_stop_usd == D("1.040")


def test_short_shrink_to_fit_uses_short_notional_per_collateral():
    result = plan_spot_margin_order(
        "BTCUSDT-MARGIN.BINANCE", "SELL", D("100"), D("110"), D("100"),
        limits(budget="10", room="100", leverage=3), base_asset="BTC", requested_leverage=3,
        shrink_margin_to_fit=True,
    )
    assert result.ok
    assert result.margin_usd == (D("10") / D("0.104") / D("2")).quantize(D("0.01"), rounding=ROUND_DOWN)  # floored to cents
    assert result.notional_usd == result.margin_usd * D("2")
    assert D("9.99") < result.loss_at_stop_usd <= D("10")  # the floored margin keeps the loss within budget


def test_unlisted_pair_is_refused_without_a_breach_code():
    result = plan_spot_margin_order("DOGEUSDT-MARGIN", "BUY", D("0.1"), D("0.09"), D("10"), limits(),
                                    base_asset="DOGE", requested_leverage=2)
    assert result.reject == LeverageReject.MARGIN_PAIR_NOT_SUPPORTED
