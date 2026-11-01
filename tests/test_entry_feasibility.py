"""H3: `entry_feasibility` says whether a profile's budget can place an order above the risk engine's minimum."""

from cryptonite_execution.sizing import entry_feasibility, typical_stop_for

EQUITY = 99_500.0  # the perp and margin minimum order is 1% of it: $995


def feas(budget, *, pos=10, risk=1, lev=1, markets=("SPOT", "PERP"), stop=3.5):
    return entry_feasibility(budget, pos, risk, lev, list(markets), EQUITY, stop)


def test_a_1000_budget_cannot_place_a_perp_order_but_spot_is_always_ok():
    out = feas(1_000)
    assert out["spot"]["code"] == "OK" and out["spot"]["min_notional"] == 0
    assert out["perp"]["min_notional"] == 995.0
    assert out["perp"]["max_notional"] == 100.0
    assert out["perp"]["code"] == "ORDER_MIN_UNREACHABLE"
    assert out["perp"]["suggested_budget_usd"] >= 9_950  # 10% position limit: the budget must be 10x the minimum


def test_a_10000_budget_reaches_the_minimum_but_the_risk_sized_order_may_not():
    out = feas(10_000, pos=25, risk=1)
    assert out["perp"]["max_notional"] == 2_500.0 and out["perp"]["risk_notional"] > 995  # 100 / 0.0395
    assert out["perp"]["code"] == "OK" and out["perp"]["suggested_budget_usd"] is None
    thin = feas(10_000, pos=25, risk=0.3)  # 30 / 0.0395 = 759 < 995
    assert thin["perp"]["code"] == "ORDER_MIN_LIKELY_REFUSED"
    assert thin["perp"]["suggested_budget_usd"] >= 13_000


def test_leverage_raises_a_perps_max_notional_not_its_risk_notional():
    one, five = feas(1_000, lev=1)["perp"], feas(1_000, lev=5)["perp"]
    assert five["max_notional"] == 5 * one["max_notional"] and five["risk_notional"] == one["risk_notional"]


def test_margin_has_the_minimum_without_leverage_and_only_listed_markets_are_returned():
    out = entry_feasibility(1_000, 10, 1, 5, ["SPOT_MARGIN"], EQUITY)
    assert list(out) == ["margin"] and out["margin"]["max_notional"] == 100.0
    assert out["margin"]["code"] == "ORDER_MIN_UNREACHABLE"


def test_an_unknown_equity_has_no_minimum():
    assert entry_feasibility(100, 10, 1, 1, ["PERP"], None)["perp"]["code"] == "OK"


def test_the_typical_stop_follows_the_timeframes():
    assert typical_stop_for(["15m"]) == 2.0 and typical_stop_for(["15m", "1h"]) == 3.5
    assert typical_stop_for([]) == 3.5 and typical_stop_for(None) == 3.5
