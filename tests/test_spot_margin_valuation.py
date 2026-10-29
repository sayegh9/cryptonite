"""Spot-margin marking, mandate equity, proximity exits and correlated exposure."""

import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

from cryptonite_execution.cross_exposure import beta_symbol
from cryptonite_orchestrator.mandate_monitor import mandate_equity, session_pnl
from cryptonite_orchestrator.marks import live_mark_price
from cryptonite_scanner_service.exit_monitor import ExitAction, Position, QuoteBook, evaluate_exit
from cryptonite_schemas.session import SessionVerdict, session_verdict


def test_margin_mark_and_exit_quote_use_the_underlying_spot_instrument():
    spot = "BTCUSDT.BINANCE"
    margin = "BTCUSDT-MARGIN.BINANCE"
    quote = SimpleNamespace(ts_init=2_000_000_000, bid_price=D("49999"), ask_price=D("50001"))
    cache = SimpleNamespace(quote=lambda instrument: quote if str(instrument) == spot else None)
    dispatcher = SimpleNamespace(mark_prices={}, mark_ts_ns={}, is_mark_stale=lambda _inst: True)
    engine = SimpleNamespace(dispatcher=dispatcher)
    assert live_mark_price(engine, cache, margin, now_ns=2_100_000_000) == (D("49999"), 2_000_000_000)

    book = QuoteBook()
    book.apply({"instrument_id": spot, "bid_price": "49999", "ask_price": "50001", "arrival_ns": 2_000_000_000})
    assert book.exit_price(margin, "LONG", now_s=2.1) == 49999.0
    assert book.exit_price(margin, "SHORT", now_s=2.1) == 50001.0


def test_mandate_equity_uses_margin_pnl_and_deducts_accrued_interest():
    class Connection:
        async def fetchval(self, _query, _instance):
            return D("0")

        async def fetch(self, query, _instance):
            if "paper_trades" in query:
                return []
            return [{
                "instrument_id": "BTCUSDT-MARGIN.BINANCE", "side": "LONG", "quantity": D("0.01"),
                "entry_price": D("50000"), "current_price": D("50000"), "realized_pnl": D("-2"),
                "unrealized_pnl": D("25"), "status": "OPEN", "interest_base": D("0.0001"),
                "interest_quote": D("1"),
            }]

    mandate = SimpleNamespace(instance_id="AI_MANDATE_1", capital_budget_usd=D("1000"))
    quotes = SimpleNamespace(quote=lambda inst: SimpleNamespace(bid_price=D("50000"))
                             if inst == "BTCUSDT.BINANCE" else None)
    equity = asyncio.run(mandate_equity(Connection(), mandate, quotes))
    assert equity == D("1017")  # -2 realized, +25 unrealized, -6 accrued interest


def test_margin_loss_flows_into_the_daily_session_loss_latch():
    class Connection:
        async def fetchval(self, _query, _instance):
            return D("0")

        async def fetch(self, query, _instance):
            if "paper_trades" in query:
                return []
            return [{
                "instrument_id": "BTCUSDT-MARGIN.BINANCE", "side": "LONG", "quantity": D("0.01"),
                "entry_price": D("50000"), "current_price": D("50000"), "realized_pnl": D("-2"),
                "unrealized_pnl": D("-50"), "status": "OPEN", "interest_base": D("0.0001"),
                "interest_quote": D("1"),
            }]

    budget = D("1000")
    mandate = SimpleNamespace(instance_id="AI_MANDATE_1", capital_budget_usd=budget)
    equity = asyncio.run(mandate_equity(Connection(), mandate))
    pnl = session_pnl(equity, budget, budget, budget)
    verdict = session_verdict(budget, D("3"), None, D("50"), pnl, None, None, None)
    assert equity == D("942") and pnl == D("-58.00")
    assert verdict is SessionVerdict.LOSS_LATCHED


def test_margin_call_alert_and_buffer_close_apply_to_long_and_short():
    for side in ("LONG", "SHORT"):
        base = Position("BTCUSDT-MARGIN.BINANCE", side, 100, 100, True,
                        margin_call_level=1.10, margin_liquidation_level=1.05)
        assert evaluate_exit(base.__class__(**{**base.__dict__, "margin_level": 1.09}), 100, True) == [
            ExitAction("ALERT", "MARGIN_CALL")
        ]
        assert evaluate_exit(base.__class__(**{**base.__dict__, "margin_level": 1.07}), 100, True) == [
            ExitAction("CLOSE", "MARGIN_LEVEL_PROXIMITY")
        ]
        manual = base.__class__(**{**base.__dict__, "margin_level": 1.07, "ai_owned": False})
        assert evaluate_exit(manual, 100, True) == [ExitAction("ALERT", "MARGIN_LEVEL_PROXIMITY")]


def test_margin_instrument_uses_same_cross_exposure_beta_as_spot():
    assert beta_symbol("ETHUSDT-MARGIN.BINANCE") == "ETHUSDT"
    assert beta_symbol("ETHUSDT-PERP.BINANCE") == "ETHUSDT"
