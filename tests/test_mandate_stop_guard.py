"""An instrument a PAUSED or HALTED mandate holds cannot be given to another mandate.

Creating a mandate is the one-click path the Brain order card takes when no mandate covers an entry
(approve -> NO_MANDATE -> create -> approve with mandate_id), so the stop has to hold in the API, not only in
the web. The PATCH path that adds an instrument to a mandate is guarded the same way.

Runs on a throwaway migrated database (``cryptonite_ci_stop_*``) and the Redis at REDIS_URL.
"""

import json
import uuid

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database
from test_ai_approvals import run_poller

BTC = "BTCUSDT.BINANCE"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/call-1/approve"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_stop") as url:
        yield url


@pytest.fixture
async def h(db_url, monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_TOTP", "true")  # 000000 in DEV, as in test_d4_feed_validation
    monkeypatch.setenv("AI_ALLOW_DEV_TOTP", "true")
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 1000000")  # ADR 0011 D11: create runs Start under the cap
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


def create_body(*instruments: str, name: str | None = None, totp: str = "000000") -> dict:
    return {"name": name or f"stop-{uuid.uuid4().hex[:6]}", "capital_budget_usd": "10000", "instruments": list(instruments),
            "max_position_pct": "10", "totp_code": totp}


@pytest.fixture
def brain_card(monkeypatch):
    """The Brain session worker, as the approve route sees it: one BUY card on BTC, status kept in memory."""
    from cryptonite_api.routes import brain as brain_routes

    card = {"kind": "order_card", "id": "call-1", "instrument_id": BTC, "side": "BUY", "quantity": 0.01,
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 49000.0, "take_profit": None,
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


@pytest.mark.parametrize("status", ["PAUSED", "HALTED"])
async def test_creating_a_mandate_on_a_stopped_instrument_is_refused(h, status):
    stopped_name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    stopped = await h.mandate(instruments=(BTC,), status=status, name=stopped_name)
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=create_body("BTCUSDT"), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"] == f"MANDATE_{status}: {stopped_name} already covers {BTC}; resume or edit it instead"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED'") == 1
    assert (await h.pool.fetchval("SELECT status FROM ai_mandates WHERE id = $1", stopped.id)) == status


async def test_a_stopped_mandate_only_blocks_its_own_instruments(h):
    await h.mandate(instruments=(BTC,), status="PAUSED")
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT"), headers=h.headers)
    assert res.status_code == 201, res.text
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED'") == 2


async def test_creation_still_needs_the_totp_code_on_a_free_instrument(h):
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp="123456"), headers=h.headers)
    assert res.status_code == 401 and res.json()["detail"].startswith("TOTP_REQUIRED")
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED'") == 0


async def test_adding_a_stopped_instrument_to_a_mandate_is_refused(h):
    stopped_name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status="PAUSED", name=stopped_name)
    active = await h.mandate(instruments=("ETHUSDT.BINANCE",), name="eth-active")
    async with h.client() as c:
        res = await c.patch(f"/api/v1/ai/mandates/{active.id}", headers=h.headers,
                            json={"instruments": ["ETHUSDT", "BTCUSDT"], "totp_code": "000000"})
    assert res.status_code == 409
    assert res.json()["detail"] == f"MANDATE_PAUSED: {stopped_name} already covers {BTC}; resume or edit it instead"
    assert await h.pool.fetchval("SELECT instruments FROM ai_mandates WHERE id = $1", active.id) == ["ETHUSDT.BINANCE"]


async def test_a_stopped_mandate_keeps_its_own_instrument_when_edited(h):
    stopped_name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    paused = await h.mandate(instruments=(BTC,), status="PAUSED", name=stopped_name)
    async with h.client() as c:
        res = await c.patch(f"/api/v1/ai/mandates/{paused.id}", headers=h.headers,
                            json={"name": f"{stopped_name}-renamed", "instruments": ["BTCUSDT"], "totp_code": "000000"})
    assert res.status_code == 200, res.text


async def test_a_card_with_no_mandate_gets_one_inline_and_is_approved(h, brain_card):
    sid = uuid.uuid4()
    path = APPROVE.format(sid=sid)
    async with h.client() as c:
        none = await c.post(path, headers=h.headers)
        assert none.status_code == 409 and "NO_MANDATE" in none.json()["detail"]
        assert brain_card["status"] == "PENDING_APPROVAL"  # not claimed until a mandate covers it

        created = await c.post("/api/v1/ai/mandates", json=create_body("BTCUSDT", name="Brain BTCUSDT.BINANCE"), headers=h.headers)
        assert created.status_code == 201, created.text
        mandate_id = created.json()["id"]
        res = await c.post(path, json={"mandate_id": mandate_id}, headers=h.headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mandate_id"] == mandate_id and brain_card["status"] == "APPROVED"

    cmd = await h.pool.fetchrow("SELECT * FROM execution_commands WHERE command_id = $1", uuid.UUID(body["command_id"]))
    assert str(cmd["mandate_id"]) == mandate_id
    row, _ = await run_poller(h.pool, cmd["command_id"])
    assert row["status"] == "COMPLETED", row["error_message"]


async def test_a_card_on_a_stopped_instrument_is_told_which_mandate_to_resume(h, brain_card):
    stopped_name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status="PAUSED", name=stopped_name)
    path = APPROVE.format(sid=uuid.uuid4())
    async with h.client() as c:
        approve = await c.post(path, headers=h.headers)
        assert approve.status_code == 409
        assert approve.json()["detail"].startswith(f"MANDATE_PAUSED: {stopped_name} already covers {BTC}")
        assert "resume it in Mission Control, then approve this card" in approve.json()["detail"]
        created = await c.post("/api/v1/ai/mandates", json=create_body("BTCUSDT"), headers=h.headers)
    assert created.status_code == 409 and created.json()["detail"].startswith("MANDATE_PAUSED")
    assert brain_card["status"] == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
