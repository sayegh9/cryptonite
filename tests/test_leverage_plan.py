"""Stage 2.2 (leverage plan §2.1, §2.2): ``plan_leveraged_order``. The §2.2 worked example is a test case, and property
tests show that for any inputs a plan never puts the stop beyond 50% of the liquidation distance, never exceeds
the loss cap or the room, and always passes ``check_leveraged_order`` (sizing and checking agree)."""

import time
from decimal import Decimal

import pytest
from cryptonite_execution.brackets import parse_leverage_brackets
from cryptonite_execution.leverage import (
    ACCOUNT_MAX_LEVERAGE,
    LIQUIDATION_BUFFER_FRACTION,
    LeverageLimits,
    check_leveraged_order,
    liquidation_distance,
    plan_leveraged_order,
)
from cryptonite_execution.margin import BRACKETS_MAX_AGE_S, set_live_brackets
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

D = Decimal
BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT-PERP.BINANCE"

def cents(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"))


# §2.2: budget $10,000, position limit 25% ($2,500), max leverage 10, risk 1% ($100), margin 2.5% ($250), grade A
EXAMPLE = LeverageLimits(risk_budget_usd=D("100"), max_notional_usd=D("2500"), max_leverage=10, grade_ceiling=10)


@pytest.mark.parametrize("stop_pct,leverage,binding,notional,loss", [
    ("0.02", 10, "exposure", D("2500"), D("60")),     # risk 16x, liquidation ~20x, exposure 10x
    ("0.05", 7, "risk at stop", D("1750"), D("94.5")),  # risk 7x, liquidation 9x
    ("0.09", 4, "risk at stop", D("1000"), D("94")),    # risk 4x, liquidation 5x, even with grade A
])
def test_worked_example(stop_pct, leverage, binding, notional, loss):
    entry = D("60000")
    plan = plan_leveraged_order(BTC, "BUY", entry, entry * (1 - D(stop_pct)), D("250"), EXAMPLE, budget_usd=D("10000"))
    assert plan.ok, plan.reject
    assert (plan.leverage, plan.binding_ceiling) == (leverage, binding)
    # the quantity is rounded down to 1e-8 BTC, so the dollars are equal to the cent
    assert cents(plan.notional_usd) == notional
    assert cents(plan.loss_at_stop_usd) == loss  # 2,500 x 2.4% = 60; 1,750 x 5.4% = 94.5; 1,000 x 9.4% = 94
    assert cents(plan.loss_at_stop_pct) == cents(loss / 100)
    assert cents(plan.loss_if_liquidated_usd) == D("250")  # the posted margin


def test_worked_example_ceilings_match_the_table():
    entry = D("60000")
    c2 = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.98"), D("250"), EXAMPLE).ceilings
    assert (c2["risk at stop"], c2["exposure"], c2["mandate maximum"], c2["setup grade"]) == (16, 10, 10, 10)
    assert c2["liquidation buffer"] >= 20  # the table's 20x is the approximation; the exact one is not lower
    c5 = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("250"), EXAMPLE).ceilings
    assert (c5["risk at stop"], c5["liquidation buffer"]) == (7, 9)
    c9 = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.91"), D("250"), EXAMPLE).ceilings
    assert (c9["risk at stop"], c9["liquidation buffer"]) == (4, 5)


def test_short_is_symmetric():
    entry = D("3000")
    plan = plan_leveraged_order(ETH, "SELL", entry, entry * D("1.05"), D("250"), EXAMPLE)
    assert (plan.leverage, plan.binding_ceiling) == (7, "risk at stop")
    assert plan.liquidation_price > entry * D("1.05")


def test_requested_leverage_is_kept_below_and_cut_above_the_allowed_maximum():
    entry = D("60000")
    low = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("250"), EXAMPLE, requested_leverage=3)
    assert (low.leverage, low.capped, low.leverage_label) == (3, False, "3x")
    high = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("250"), EXAMPLE, requested_leverage=10)
    assert (high.leverage, high.capped, high.leverage_label) == (7, True, "7x (risk at stop)")


def test_grade_and_maximum_ceilings():
    entry = D("60000")
    b = LeverageLimits(risk_budget_usd=D("100"), max_notional_usd=D("2500"), max_leverage=10, grade_ceiling=5)
    assert plan_leveraged_order(BTC, "BUY", entry, entry * D("0.98"), D("250"), b).leverage_label == "5x"
    m3 = LeverageLimits(risk_budget_usd=D("100"), max_notional_usd=D("2500"), max_leverage=3)
    p = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.98"), D("250"), m3)
    assert (p.leverage, p.binding_ceiling) == (3, "mandate maximum")
    no_trade = LeverageLimits(risk_budget_usd=D("100"), max_notional_usd=D("2500"), max_leverage=10, grade_ceiling=0)
    assert plan_leveraged_order(BTC, "BUY", entry, entry * D("0.98"), D("250"), no_trade).reject == "SETUP_NO_TRADE"
    acct = LeverageLimits(risk_budget_usd=D("1000"), max_notional_usd=D("25000"), max_leverage=20,
                          maximum_label="account maximum")
    p = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.99"), D("1000"), acct, requested_leverage=20)
    assert p.leverage <= ACCOUNT_MAX_LEVERAGE and "account maximum" in p.ceilings


@pytest.mark.parametrize("stop,code", [(None, "STOP_REQUIRED"), (D("61000"), "STOP_WRONG_SIDE"), (D("60000"), "STOP_WRONG_SIDE")])
def test_stop_rules(stop, code):
    assert plan_leveraged_order(BTC, "BUY", D("60000"), stop, D("250"), EXAMPLE).reject == code


def test_too_much_margin_is_refused_unless_sizing_may_shrink_it():
    entry = D("60000")
    big = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("3000"), EXAMPLE)
    assert big.reject == "RISK_AT_STOP_EXCEEDED"  # 3,000 x 5.4% = 162 > 100 even at 1x
    fit = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("3000"), EXAMPLE, shrink_margin_to_fit=True)
    assert fit.ok and fit.margin_cut and fit.leverage == 1
    assert fit.loss_at_stop_usd <= D("100")
    room = LeverageLimits(risk_budget_usd=D("100"), max_notional_usd=D("0"), max_leverage=10)
    assert plan_leveraged_order(BTC, "BUY", entry, entry * D("0.95"), D("250"), room,
                                shrink_margin_to_fit=True).reject == "EXPOSURE_LIMIT_REACHED"


def test_symbol_without_brackets_is_refused():
    p = plan_leveraged_order("SOLUSDT-PERP.BINANCE", "BUY", D("150"), D("140"), D("250"), EXAMPLE)
    assert p.reject == "NO_LEVERAGE_BRACKETS"


def test_stale_brackets_are_refused():
    table = parse_leverage_brackets([{"symbol": "SOLUSDT", "brackets": [
        {"bracket": 1, "initialLeverage": 20, "notionalCap": 100000, "notionalFloor": 0, "maintMarginRatio": 0.01, "cum": 0}]}])
    set_live_brackets("SOLUSDT", table["SOLUSDT"], time.time_ns() - (BRACKETS_MAX_AGE_S + 1) * 1_000_000_000)
    assert plan_leveraged_order("SOLUSDT-PERP.BINANCE", "BUY", D("150"), D("140"), D("250"), EXAMPLE).reject \
        == "STALE_LEVERAGE_BRACKETS"


def test_exchange_bracket_binds_on_a_large_notional():
    # BTC reference tier 4 (1m..10m notional) allows 20x, tier 5 (10m..20m) 10x, tier 6 (20m+) 5x
    big = LeverageLimits(risk_budget_usd=D("10000000"), max_notional_usd=D("100000000"), max_leverage=10)
    p = plan_leveraged_order(BTC, "BUY", D("60000"), D("59000"), D("3000000"), big)
    assert p.binding_ceiling == "exchange" and p.leverage == 6  # 6 x 3m = 18m in tier 5 (10x); 7 x 3m = 21m: 5x


def test_funding_per_8h_sign():
    entry = D("60000")
    long = plan_leveraged_order(BTC, "BUY", entry, entry * D("0.98"), D("250"), EXAMPLE, funding_rate=D("0.0001"))
    short = plan_leveraged_order(BTC, "SELL", entry, entry * D("1.02"), D("250"), EXAMPLE, funding_rate=D("0.0001"))
    assert cents(long.funding_8h_usd) == -D("0.25") and cents(short.funding_8h_usd) == D("0.25")  # longs pay


def test_check_refuses_what_the_plan_would_not_allow():
    entry = D("60000")
    stop = entry * D("0.95")
    qty_10x = D("2500") / entry  # $250 margin at 10x
    assert check_leveraged_order(BTC, "BUY", qty_10x, entry, stop, 10, risk_budget_usd=D("1000")) \
        == "LIQUIDATION_BUFFER_TOO_THIN"
    assert check_leveraged_order(BTC, "BUY", D("1750") / entry, entry, stop, 7, risk_budget_usd=D("90")) \
        == "RISK_AT_STOP_EXCEEDED"
    assert check_leveraged_order(BTC, "BUY", D("1750") / entry, entry, stop, 7, risk_budget_usd=D("100")) is None
    assert check_leveraged_order(BTC, "BUY", D("400"), entry, None, 6, risk_budget_usd=None) \
        == "LEVERAGE_ABOVE_BRACKET"  # 24m notional: tier 6 allows 5x
    assert check_leveraged_order(BTC, "SELL", qty_10x, entry, D("59000"), 2, risk_budget_usd=None) == "STOP_WRONG_SIDE"


# ------------------------------------------------------------------------------------------- properties

prices = st.decimals(min_value=D("0.01"), max_value=D("150000"), places=2)
fractions = st.decimals(min_value=D("0.001"), max_value=D("0.3"), places=4)
money = st.decimals(min_value=D("1"), max_value=D("50000"), places=2)


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inst=st.sampled_from([BTC, ETH]), side=st.sampled_from(["BUY", "SELL"]), entry=prices, stop_frac=fractions,
       margin=money, risk=money, room=money, max_lev=st.integers(1, 10), grade=st.one_of(st.none(), st.integers(0, 10)),
       requested=st.one_of(st.none(), st.integers(1, 20)), shrink=st.booleans(),
       slip=st.decimals(min_value=D("0"), max_value=D("0.01"), places=4))
def test_a_plan_never_breaks_a_ceiling(inst, side, entry, stop_frac, margin, risk, room, max_lev, grade, requested,
                                       shrink, slip):
    stop = entry * (1 - stop_frac) if side == "BUY" else entry * (1 + stop_frac)
    limits = LeverageLimits(risk_budget_usd=risk, max_notional_usd=room, max_leverage=max_lev, grade_ceiling=grade,
                            slippage_pct=slip)
    plan = plan_leveraged_order(inst, side, entry, stop, margin, limits, requested_leverage=requested,
                                shrink_margin_to_fit=shrink)
    if not plan.ok:
        return
    assert 1 <= plan.leverage <= min(max_lev, ACCOUNT_MAX_LEVERAGE, grade if grade is not None else 99)
    assert plan.loss_at_stop_usd <= risk  # never more than the loss cap
    assert plan.notional_usd <= room  # never more than the room
    if plan.quantity > 0:
        _, dist = liquidation_distance(inst, side, plan.quantity, entry, plan.notional_usd / plan.leverage)
        # the stop (plus slippage) sits inside half the liquidation distance
        assert dist is None or stop_frac + slip <= LIQUIDATION_BUFFER_FRACTION * dist
    # sizing and checking agree: what the plan sized passes the check
    assert check_leveraged_order(inst, side, plan.quantity, entry, stop, plan.leverage, risk_budget_usd=risk,
                                 slippage_pct=slip) is None if plan.quantity > 0 else True
