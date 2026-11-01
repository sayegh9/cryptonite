import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("policy_sim", Path(__file__).parents[1] / "scripts" / "policy_sim.py")
sim = importlib.util.module_from_spec(_spec)
sys.modules["policy_sim"] = sim
_spec.loader.exec_module(sim)

T0 = datetime(2026, 11, 2, tzinfo=UTC)


def d(cid, inst, hours, r, *, policy="A", decision="TAKE", side="LONG", size=1.0, hold=4, stop=98.0):
    at = T0 + timedelta(hours=hours)
    return sim.Decision(cid, policy, decision, side, size, inst, at, at + timedelta(hours=hold), 100.0, stop,
                        {"net_r_preset": r, "net_r_profile": None if r is None else r / 2})


def test_size_trade_risk_cap_and_min_notional():
    # 2% stop: 1% risk -> $5,000, capped at the 10% position limit ($1,000), risking $20
    assert sim.size_trade(10_000, 100, 98, 1.0, "spot") == pytest.approx((1_000, 20))
    # a perp order below 1% of equity is refused; spot has no minimum
    assert sim.size_trade(10_000, 100, 50, 0.1, "perp") is None
    assert sim.size_trade(10_000, 100, 50, 0.1, "spot") is not None


def test_one_position_per_coin_and_max_open():
    rows = [d("1", "BTC", 0, 1.0), d("2", "BTC", 1, 1.0), d("3", "ETH", 1, 1.0), d("4", "SOL", 1, 1.0),
            d("5", "XRP", 2, 1.0), d("6", "BTC", 5, -1.0)]
    res = sim.simulate(rows, "A", "net_r_preset")
    assert (res.trades, res.skipped_held, res.skipped_full) == (4, 1, 1)
    assert res.trades == res.wins + 1


def test_skips_count_as_opportunity_cost_and_other_policies_ignored():
    rows = [d("1", "BTC", 0, 2.0, decision="SKIP"), d("2", "ETH", 0, 1.0, policy="B"), d("3", "SOL", 0, None)]
    res = sim.simulate(rows, "A", "net_r_profile")
    assert (res.trades, res.skip_count, res.skip_net_r) == (0, 1, pytest.approx(1.0))


def test_drawdown_half_size_and_interval():
    rows = [d(str(i), f"C{i}", 24 * i, -1.0 if i % 2 else 0.5, hold=1) for i in range(6)]
    rows.append(d("h", "H", 200, 1.0, size=0.5, stop=90.0, hold=1))  # 10% stop, half size: $50 risk = 0.5R
    res = sim.simulate(rows, "A", "net_r_preset")
    assert res.trades == 7 and res.max_dd_r > 0 and res.max_dd_pct > 0
    assert res.trade_rs[-1][1] == pytest.approx(0.5)
    assert res.ci90 is not None and res.ci90[0] <= res.ci90[1]
    assert "| A | preset | 7 |" in sim.render([res], T0, T0)
