"""A Brain SELL card closes the mandate's own spot holding, or it is refused.

Spot cannot be shorted, so a SELL on an instrument the mandate does not hold can never execute. Before this,
the card was approved and only then failed inside the orchestrator. A SELL is now refused at approval, before
the card is claimed, and an approved SELL is sized to the mandate's holding (as a debate SELL is), not to a
share of the budget.

Runs on a throwaway migrated database (``cryptonite_ci_sell_*``) and the Redis at REDIS_URL.
"""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from migrated_db import migrated_database
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/call-1/approve"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_sell") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")  # 000000 in DEV, as in test_d4_feed_validation
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


@pytest.fixture
def sell_card(monkeypatch):
    """The Brain session worker as the approve route sees it: one SELL card on BTC, status kept in memory."""
    from cryptonite_api.routes import brain as brain_routes

    card = {"kind": "order_card", "id": "call-1", "instrument_id": BTC, "side": "SELL", "quantity": None,
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": None, "take_profit": None,
            "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        body = json.loads(request.content)
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card


async def hold(pool, instance_id: str, instrument_id: str, qty: str) -> None:
    """A filled BUY on the mandate's own instance: what ``instance_spot_holdings`` nets as a holding."""
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'MARKET', $4::numeric, $4::numeric, 50000, 'FILLED')""",
        f"hold-{uuid.uuid4().hex[:8]}", instance_id, instrument_id, qty)


async def run_seeded_poller(pool, command_id, held: Decimal):
    """The orchestrator's command poller, with the dispatcher already holding ``held`` of BTC (a fresh
    dispatcher holds nothing, so it could never sell)."""
    d, _ = make_fresh_dispatcher(starting_cash=Decimal("100000"))
    d.positions[BTC] = held
    engine = make_engine(d)
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(BTC, 50000.0, 50010.0)
    task = asyncio.create_task(command_poller(pool, engine, quotes))
    try:
        for _ in range(100):
            row = await pool.fetchrow("SELECT status, error_message FROM execution_commands WHERE command_id = $1", command_id)
            if row["status"] in ("COMPLETED", "REJECTED"):
                return row, engine
            await asyncio.sleep(0.05)
        raise AssertionError("command not processed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_sell_on_an_instrument_the_mandate_does_not_hold_is_refused_before_it_is_claimed(h, sell_card):
    await h.mandate(instruments=(BTC,))
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"].startswith("SPOT_SELL_NOT_HELD: ") and "reject this card" in res.json()["detail"]
    assert sell_card["status"] == "PENDING_APPROVAL"  # still the operator's to reject
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_holding_a_different_instrument_does_not_cover_the_sell(h, sell_card):
    m = await h.mandate(instruments=(BTC, ETH))
    await hold(h.pool, m.instance_id, ETH, "2")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("SPOT_SELL_NOT_HELD")
    assert sell_card["status"] == "PENDING_APPROVAL"


async def test_an_approved_sell_closes_the_holding_and_completes(h, sell_card):
    m = await h.mandate(instruments=(BTC,), budget=10000)
    # 5% of a 10,000 budget at 50,000 is 0.01 BTC, more than the 0.005 held: the old budget-sized sell would be
    # refused as a position the mandate does not own, so the order must be sized to the holding
    await hold(h.pool, m.instance_id, BTC, "0.005")
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert sell_card["status"] == "APPROVED"

    cmd = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE command_id = $1", uuid.UUID(body["command_id"]))
    payload = json.loads(cmd["payload"])
    assert payload["side"] == "SELL" and payload["quantity"] == 0.005
    assert str(cmd["mandate_id"]) == str(m.id)

    row, engine = await run_seeded_poller(h.pool, cmd["command_id"], Decimal("0.005"))
    assert row["status"] == "COMPLETED", row["error_message"]
    assert engine.dispatcher.positions.get(BTC, Decimal("0")) == 0  # the whole holding was sold
