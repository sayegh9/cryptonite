"""Synthetic liquidation engine: GuardedOrderDispatcher.check_liquidations + PaperTradingEngine wiring.

Account: 10,000 USDT, taker fee 0.1%, clearance fee 0.5%. BTCUSDT tier 1: MMR 0.4%, cum 0.
Isolated 0.04 BTC @ 50,000, 10x -> IM 200
  long:  P_liq = (2,000 - 200) / (0.04 * 0.996) = 45,180.72..., bankruptcy 45,000
  short: P_liq = (2,000 + 200) / (0.04 * 1.004) = 54,780.87..., bankruptcy 55,000
"""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from cryptonite_execution.engine import LIQUIDATION_CHANNEL, PaperTradingEngine
from test_bridge_perpetuals_persistence import (  # noqa: F401 - ctx/db_url are fixtures
    ACCOUNT,
    account,
    ctx,
    db_url,
    fill,
    positions,
)
from test_paper_risk_guardrails import PERP, make_perp_dispatcher, perp_intent

D = Decimal
ETH = "ETHUSDT-PERP.BINANCE"


def isolated(side_sign: int, instance_id: str = "INST-001"):
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.04") * side_sign, D("50000"), leverage=10, margin_mode="ISOLATED", instance_id=instance_id)
    return d


def test_long_liquidates_at_or_below_liquidation_price_only():
    d = isolated(+1)
    assert d.check_liquidations({PERP: D("45181")}) == []  # just above P_liq
    assert d.get_perp_position_qty(PERP) == D("0.04")

    (liq,) = d.check_liquidations({PERP: D("45180")})
    assert (liq.side, liq.quantity, liq.margin_mode) == ("LONG", D("0.04"), "ISOLATED")
    assert liq.liquidation_price.quantize(D("0.01")) == D("45180.72")
    assert (liq.fill_price, liq.bankruptcy_price) == (D("45180"), D("45000"))
    assert liq.realized_pnl == D("-192.8")  # 0.04 * (45,180 - 50,000)
    assert liq.trading_fee == D("0.90360") and liq.clearance_fee == D("9.03600")  # 1,807.2 * 0.05% and * 0.5%
    # 192.8 + 9.9396 exceeds the 200 margin: the loss stops at the margin, the fund covers 2.7396
    assert liq.shortfall_covered == D("2.73960") == liq.insurance_fund_deficit
    assert d.get_perp_position_qty(PERP) == 0


def test_short_liquidates_at_or_above_liquidation_price_only():
    d = isolated(-1)
    assert d.check_liquidations({PERP: D("54780")}) == []
    (liq,) = d.check_liquidations({PERP: D("54781")})
    assert (liq.side, liq.fill_price, liq.bankruptcy_price) == ("SHORT", D("54781"), D("55000"))
    assert liq.realized_pnl == D("-191.24")  # 0.04 * (50,000 - 54,781)
    assert d.get_perp_position_qty(PERP) == 0


def test_liquidation_at_exact_liquidation_price_triggers():
    d = isolated(+1)
    _, liq_price = d._perp_risk_levels(PERP)
    (liq,) = d.check_liquidations({PERP: liq_price})
    assert liq.fill_price == liq_price


def test_non_breached_positions_are_untouched_by_normal_moves():
    d = isolated(+1)
    d.set_perp_position(ETH, D("-0.5"), D("3000"), leverage=5, margin_mode="ISOLATED")
    cash, locked = d.cash_balance, d.locked_margin
    for btc, eth in [("48000", "3100"), ("52000", "2900"), ("46000", "3400")]:
        assert d.check_liquidations({PERP: D(btc), ETH: D(eth)}) == []
    assert (d.get_perp_position_qty(PERP), d.get_perp_position_qty(ETH)) == (D("0.04"), D("-0.5"))
    assert (d.cash_balance, d.locked_margin) == (cash, locked)


def test_isolated_liquidation_clears_margin_and_loses_exactly_the_margin():
    d = isolated(+1)
    d.check_liquidations({PERP: D("45180")})
    assert d.perp_positions == {}
    assert d.locked_margin == 0 and d.reserved_cash == 0
    assert d.cash_balance == D("9800")  # 10,000 - 200 isolated margin, nothing more
    assert d.available_cash == D("9800")


def test_gap_through_bankruptcy_fills_at_bankruptcy_and_books_fund_deficit():
    d = isolated(+1)
    (liq,) = d.check_liquidations({PERP: D("44000")})  # wick below the 45,000 bankruptcy price
    assert liq.fill_price == D("45000") and liq.realized_pnl == D("-200")
    assert liq.shortfall_covered == D("9.9000")  # fees (0.05% + 0.5%) on 1,800 notional, beyond the margin
    assert liq.insurance_fund_deficit == D("49.9000")  # + 0.04 * (45,000 - 44,000) gap
    assert d.insurance_fund_deficit == D("49.9000")
    assert d.cash_balance == D("9800")


def test_cross_liquidation_stops_account_at_zero_equity():
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("-0.04"), D("50000"), leverage=10)  # CROSS: whole wallet backs it
    _, liq_price = d._perp_risk_levels(PERP)
    assert liq_price > D("298000")
    assert d.check_liquidations({PERP: D("298000")}) == []
    (liq,) = d.check_liquidations({PERP: D("300000")})
    assert liq.bankruptcy_price is None and liq.realized_pnl == D("-10000")
    assert liq.shortfall_covered == D("66.000")  # 12,000 notional * 0.55% fees beyond the wallet
    assert d.cash_balance == 0 and d.locked_margin == 0


def test_liquidation_cancels_resting_orders_and_ignores_kill_switch():
    d = isolated(+1)
    ok, _ = d.dispatch(perp_intent("SELL", "0.02", "RO-1", reduce_only=True), MagicMock(), MagicMock())
    assert ok
    d.kill_switch.trip(latched_by="test", reason="latched during crash")
    (liq,) = d.check_liquidations({PERP: D("45000")})
    assert liq.quantity == D("0.04")
    assert d.open_orders == {} and d.reserved_cash == 0


def test_symbol_without_brackets_is_skipped_not_liquidated():
    d = make_perp_dispatcher()
    sol = "SOLUSDT-PERP.BINANCE"
    d.set_perp_position(sol, D("1"), D("150"), leverage=10, margin_mode="ISOLATED")
    assert d.check_liquidations({sol: D("1")}) == []
    assert d.get_perp_position_qty(sol) == D("1")


# --- Engine wiring -----------------------------------------------------------------

async def test_engine_emits_liquidation_fill_event_and_ui_notification():
    engine = PaperTradingEngine(
        account_id="PAPER-ACCOUNT-001", trader_id="TRADER-LIQ", engine_run_id="RUN-LIQ",
        pg_pool=MagicMock(), redis_client=AsyncMock(),
    )
    engine.dispatcher = isolated(+1)

    (liq,) = await engine.process_liquidations({PERP: D("45180")})

    evt = engine.bridge.queue.get_nowait()
    assert evt["event_type"] == "ORDER_FILLED" and evt["liquidation"] is True
    assert (evt["side"], evt["quantity"], evt["price"]) == ("SELL", "0.04", "45180")
    assert evt["position"] == {"signed_qty": "0"}
    assert evt["shortfall_covered"] == str(liq.shortfall_covered)
    channel, payload = engine.redis.publish.await_args.args
    assert channel == LIQUIDATION_CHANNEL == "channel:perp_liquidations"
    body = json.loads(payload)
    assert body["type"] == "perp.liquidation" and body["instrument_id"] == PERP and body["side"] == "LONG"


async def test_liquidation_persists_flat_row_and_reconciled_cash(ctx):  # noqa: F811 - fixture
    await fill(ctx, "L-1", "BUY", "0.04", "50000", leverage=10, margin_mode="ISOLATED")
    engine = PaperTradingEngine(
        account_id=ACCOUNT, trader_id="TRADER-LIQ-DB", engine_run_id="RUN-LIQ-DB",
        pg_pool=ctx["pool"], redis_client=None,
    )
    engine.dispatcher, engine.bridge = ctx["dispatcher"], ctx["bridge"]
    ctx["dispatcher"].account_id = ACCOUNT

    (liq,) = await engine.process_liquidations({PERP: D("45180")})
    assert await ctx["worker"].drain_and_commit_batch() == 1

    (row,) = await positions(ctx)
    assert (row["side"], row["quantity"], row["status"]) == ("FLAT", 0, "CLOSED")
    assert row["closed_at"] is not None
    assert row["realized_pnl"] == D("-200")  # loss stopped at the isolated margin
    order = await ctx["pool"].fetchrow("SELECT * FROM paper_orders WHERE client_order_id = $1", liq.client_order_id)
    assert (order["side"], order["status"]) == ("SELL", "FILLED")
    acct = await account(ctx)
    assert acct["cash_balance"] == ctx["dispatcher"].cash_balance == D("9800")
