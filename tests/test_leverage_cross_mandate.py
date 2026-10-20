"""Stage 2.5 (leverage plan): the cross-mandate exposure cap (ADR 0006, F4) counts perp notional by |notional| x
beta, whatever the leverage: a perp short and a spot long on the same coin both count (no offset), and 10x
perps on BTC and ETH together hit the 25% cap although their posted margin is a tenth of it."""

import uuid
from decimal import Decimal

import pytest
from cryptonite_execution import cross_exposure as cx
from cryptonite_execution.mandates import MandateOrder
from cryptonite_orchestrator import risk_overrides
from cryptonite_orchestrator.cross_mandate import cross_mandate_holdings, cross_mandate_reject
from cryptonite_schemas.mandate import Mandate, mandate_instance_id
from test_ai_mandates import FakeConn
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_perp_dispatcher

D = Decimal
BTC, BTC_PERP, ETH_PERP = "BTCUSDT.BINANCE", "BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name, value in (("betas", {}), ("cross_cap_pct", cx.DEFAULT_CROSS_MANDATE_EXPOSURE_PCT)):
        monkeypatch.setattr(risk_overrides.STATE, name, value)


class Conn(FakeConn):
    """Lists the mandates too (every non-archived mandate's instance counts)."""

    def __init__(self, mandate_ids, positions=()):
        super().__init__(None, None, positions)
        self.mandate_ids = mandate_ids

    async def fetch(self, sql, *args):
        if "FROM ai_mandates" in sql:
            return [{"id": mid} for mid in self.mandate_ids]
        return await super().fetch(sql, *args)


def perp_mandate(inst) -> Mandate:
    return Mandate(id=uuid.uuid4(), name="p", capital_budget_usd=D("10000"), instruments=[inst],
                   max_position_pct=D("25"), allow_perps=True, max_leverage=10)


def engine_with(*perps):
    """10,000 USDT account; ``perps``: (instrument, signed qty, price, leverage, owner instance)."""
    d = make_perp_dispatcher()
    for inst, qty, px, lev, owner in perps:
        d.update_mark_price(inst, D(px))
        d.set_perp_position(inst, D(qty), D(px), leverage=lev, margin_mode="ISOLATED", instance_id=owner)
    return make_engine(d)


async def test_a_perp_short_and_a_spot_long_on_the_same_coin_both_count():
    spot_owner, perp_owner = uuid.uuid4(), uuid.uuid4()
    held = [{"instance_id": mandate_instance_id(spot_owner), "instrument_id": BTC, "side": "LONG",
             "quantity": D("0.03"), "current_price": D("50000")}]  # $1,500 spot long
    engine = engine_with((BTC_PERP, "-0.02", "50000", 10, mandate_instance_id(perp_owner)))  # $1,000 short
    conn = Conn([spot_owner, perp_owner], held)
    holdings = await cross_mandate_holdings(conn, engine, {mandate_instance_id(spot_owner), mandate_instance_id(perp_owner)}, {})
    assert holdings == {BTC: D("1500"), BTC_PERP: D("1000")}
    assert cx.beta_weighted_exposure(holdings, {}) == D("2500")  # the short does not offset the long

    # a $100 perp entry by a third mandate is over the 25% cap of the 10,000 equity
    third = perp_mandate(BTC_PERP)
    order = MandateOrder(BTC_PERP, "BUY", D("0.002"), D("50000"), order_type="LIMIT", leverage=5)
    reason = await cross_mandate_reject(Conn([spot_owner, perp_owner, third.id], held), engine, third, order, {})
    assert reason is not None and reason.startswith("CROSS_MANDATE_EXPOSURE")


async def test_10x_btc_and_eth_perps_together_hit_the_cap_by_notional_not_margin():
    a, b = perp_mandate(BTC_PERP), perp_mandate(ETH_PERP)
    engine = engine_with((BTC_PERP, "0.04", "50000", 10, a.instance_id),   # $2,000 notional, $200 margin
                         (ETH_PERP, "0.15", "3000", 10, b.instance_id))    # $450 notional, $45 margin
    conn = Conn([a.id, b.id])
    margin = sum(p.initial_margin for p in engine.dispatcher.perp_positions.values())
    assert margin == D("245")  # a tenth of the notional
    small = MandateOrder(ETH_PERP, "BUY", D("0.01"), D("3000"), order_type="LIMIT", leverage=10)  # $30 -> 2,480
    assert await cross_mandate_reject(conn, engine, b, small, {}) is None
    more = MandateOrder(ETH_PERP, "BUY", D("0.04"), D("3000"), order_type="LIMIT", leverage=10)  # $120 -> 2,570
    reason = await cross_mandate_reject(conn, engine, b, more, {})
    assert reason is not None and reason.startswith("CROSS_MANDATE_EXPOSURE")
    assert "$2,450 held" in reason  # 2,000 + 450 notional, beta 1.0 without lake data
