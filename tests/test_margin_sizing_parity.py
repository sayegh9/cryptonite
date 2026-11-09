"""One buffered sizing for AI spot-margin entries: a profile whose room cannot reach the 1% minimum order is refused
early, with the numbers, instead of being queued and refused when the orchestrator sizes it again (2026-11-08/09: six
SOL/SUI/NEAR entries on $10,000 budgets at a 10% max position)."""

from decimal import Decimal as D

import pytest
from cryptonite_execution.leverage import (
    SIZING_MARGIN,
    LeverageReject,
    buffered_entry_limits,
    plan_spot_margin_order,
)

EQUITY = D("99502.53907341861139414672640")  # the orchestrator's equity at the 2026-11-09 day roll
SUI = "SUIUSDT-MARGIN.BINANCE"
ENTRY, SHORT_STOP, LONG_STOP = D("0.82055"), D("0.8764"), D("0.7647")  # the 00:07 short's levels, mirrored for a long


def limits(pos_pct, equity=EQUITY, budget=D("10000")):
    return buffered_entry_limits(room_usd=budget * D(pos_pct) / 100, equity_usd=equity,
                                 risk_budget_usd=budget / 100, max_leverage=3)


def plan(side, pos_pct, equity=EQUITY):
    """Sized as the route sizes a grade C entry: half the max position as margin (grade_size_pct)."""
    stop = SHORT_STOP if side == "SELL" else LONG_STOP
    margin = D("10000") * D(pos_pct) / 100 / 2
    return plan_spot_margin_order(SUI, side, ENTRY, stop, margin, limits(pos_pct, equity), base_asset="SUI",
                                  requested_leverage=3, budget_usd=D("10000"), shrink_margin_to_fit=True)


def test_the_limits_carry_the_margin_on_every_side():
    lim = limits(10)
    assert lim.max_notional_usd == D("990.00")
    assert lim.min_notional_usd == D("0.01") * EQUITY / SIZING_MARGIN
    assert lim.risk_budget_usd == D("99.00") and lim.slippage_pct == D("0.004")
    assert lim.max_notional_binding == "profile max position"


@pytest.mark.parametrize("side", ["SELL", "BUY"])
def test_a_10000_budget_at_10_percent_cannot_reach_the_minimum_and_says_why(side):
    p = plan(side, 10)
    assert p.reject == LeverageReject.ORDER_BELOW_MIN_NOTIONAL
    assert p.notional_usd == D("990.00")
    text = p.binding_ceiling
    assert "largest order $990.00 set by profile max position" in text
    assert "minimum order $1,005.08" in text and "account equity $99,502.54" in text


@pytest.mark.parametrize("side", ["SELL", "BUY"])
def test_15_percent_fits_between_the_minimum_and_the_room(side):
    p = plan(side, 15)
    assert p.ok, p.binding_ceiling
    assert limits(15).min_notional_usd <= p.notional_usd <= D("1485.00")
    assert p.loss_at_stop_usd <= D("99.00")


def test_the_minimum_follows_equity():
    assert plan("SELL", 10, equity=D("80000")).ok  # minimum $808.08 < $990
    assert not plan("SELL", 10, equity=D("100000")).ok  # minimum $1,010.10 > $990


def test_the_account_cap_binds_when_it_is_smaller_than_the_room():
    lim = buffered_entry_limits(room_usd=D("5000"), equity_usd=D("10000"), held_usd=D("2000"),
                                risk_budget_usd=D("100"), max_leverage=3)
    assert lim.max_notional_binding == "25% account exposure cap" and lim.max_notional_usd == D("495.00")


def test_a_tight_risk_budget_is_named_when_it_binds():
    lim = buffered_entry_limits(room_usd=D("2500"), equity_usd=EQUITY, risk_budget_usd=D("30"), max_leverage=3)
    p = plan_spot_margin_order(SUI, "SELL", ENTRY, SHORT_STOP, D("500"), lim, base_asset="SUI",
                               requested_leverage=3, shrink_margin_to_fit=True)
    assert p.reject == LeverageReject.ORDER_BELOW_MIN_NOTIONAL and "the risk budget at this stop" in p.binding_ceiling


def test_a_short_whose_margin_is_too_small_names_the_margin_and_leverage():
    """A 3x short sells twice its margin: a $500 margin is a $1,000 order, below the minimum whatever the room."""
    p = plan_spot_margin_order(SUI, "SELL", ENTRY, SHORT_STOP, D("500"), limits(25), base_asset="SUI",
                               requested_leverage=3, shrink_margin_to_fit=True)
    assert p.reject == LeverageReject.ORDER_BELOW_MIN_NOTIONAL
    assert "largest order $1,000.00 set by the margin at 3x" in p.binding_ceiling


@pytest.mark.parametrize("room_over_min", [D("-0.01"), D("0.01"), D("0.02"), D("1.00")])
def test_the_boundary_never_sends_an_order_under_the_minimum(room_over_min):
    """Around the boundary the margin floors to cents: an order is either inside both limits or refused with a reason."""
    room = (limits(10).min_notional_usd + room_over_min) / SIZING_MARGIN
    lim = buffered_entry_limits(room_usd=room, equity_usd=EQUITY, risk_budget_usd=D("1000"), max_leverage=3)
    p = plan_spot_margin_order(SUI, "SELL", ENTRY, SHORT_STOP, D("5000"), lim, base_asset="SUI",
                               requested_leverage=3, shrink_margin_to_fit=True)
    if room_over_min < 0:
        assert p.reject == LeverageReject.ORDER_BELOW_MIN_NOTIONAL and "largest order" in p.binding_ceiling
    elif p.ok:
        assert lim.min_notional_usd <= p.notional_usd <= lim.max_notional_usd
    else:
        assert p.reject == LeverageReject.ORDER_BELOW_MIN_NOTIONAL and "largest order" in p.binding_ceiling
    if room_over_min == D("1.00"):
        assert p.ok
