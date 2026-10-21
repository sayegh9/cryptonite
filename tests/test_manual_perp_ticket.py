"""Leverage plan 4.5: the manual perp ticket's preview (GET /paper/perp/plan) judges each leverage pill with the
dispatcher's own check for the quantity the operator typed, and shows the margin, liquidation price, buffer and
losses at the chosen leverage; POST /paper/orders carries an attached stop. No Binance call: the mark is a Redis key."""

import json
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database

MARK_KEY = "perp:mark:BTCUSDT"
PLAN = "/api/v1/paper/perp/plan"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_perp_ticket") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 100000 WHERE account_id = 'PAPER-ACCOUNT-001'")
    await harness.redis.set(MARK_KEY, json.dumps({"type": "perp.mark", "mark_price": "60000", "funding_rate": "0.0001",
                                                  "ts_event_ns": 1}), ex=300)
    try:
        yield harness
    finally:
        await harness.redis.delete(MARK_KEY)
        await close_harness(harness)


async def preview(h, **params):
    async with h.client() as c:
        return await c.get(PLAN, params={"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "BUY", **params})


async def test_each_pill_is_judged_for_the_typed_quantity(h):
    res = await preview(h, quantity=0.05, leverage=5, stop_loss=57000)
    body = res.json()
    assert res.status_code == 200 and body["ok"] is True, body
    # $3,000 notional, a 5% stop + 0.3% slippage: 10x leaves a liquidation distance under twice the stop gap
    assert body["pills"]["9"] is None and body["pills"]["10"] == "LIQUIDATION_BUFFER_TOO_THIN"
    assert body["recommended_leverage"] == 9
    assert Decimal(body["notional_usd"]) == Decimal("3000") and Decimal(body["margin_usd"]) == Decimal("600")
    assert Decimal(body["loss_at_stop_usd"]) == Decimal("162.000")  # 3000 x (5% + 0.3% + 0.1% fees)
    assert Decimal(body["loss_if_liquidated_usd"]) == Decimal("600")
    assert Decimal(body["liquidation_price"]) < Decimal("57000")
    assert Decimal(body["funding_8h_usd"]) == Decimal("-0.3")
    assert Decimal(body["risk_budget_usd"]) == Decimal("2000")  # 2% of equity for a manual trade


async def test_without_a_stop_only_the_bracket_judges_the_leverage(h):
    body = (await preview(h, quantity=0.05, leverage=10)).json()
    assert body["ok"] is True and body["recommended_leverage"] == 10
    assert body["loss_at_stop_usd"] is None and body["buffer_used_pct"] is None


async def test_leverage_independent_refusals_disable_every_pill(h):
    body = (await preview(h, quantity=0.5, leverage=3)).json()  # $30,000 > 25% of equity
    assert body["ok"] is False and body["reject"] == "CONCENTRATION_LIMIT_BREACHED"
    assert set(body["pills"].values()) == {"CONCENTRATION_LIMIT_BREACHED"} and body["recommended_leverage"] is None
    body = (await preview(h, quantity=0.05, leverage=3, stop_loss=61000)).json()
    assert body["reject"] == "STOP_WRONG_SIDE"


async def test_no_mark_and_spot_are_refused(h):
    await h.redis.delete(MARK_KEY)
    body = (await preview(h, quantity=0.05, leverage=3)).json()
    assert body["ok"] is False and body["reject"] == "NO_MARK_PRICE"
    async with h.client() as c:
        res = await c.get(PLAN, params={"instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "quantity": 0.05, "leverage": 3})
    assert res.status_code == 422
    async with h.client() as c:
        res = await c.get(PLAN, params={"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "BUY", "quantity": 0.05,
                                        "leverage": 11})
    assert res.status_code == 422  # the 10x account maximum
