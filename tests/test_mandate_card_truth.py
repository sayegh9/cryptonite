"""The order card and the mandate editor tell the operator the true reason a request is refused (WS-2E, 2E-api).

2026-10-15 incident: approving a BTC card while only a PAUSED mandate covered BTC returned NO_MANDATE, so the card
opened its inline create-mandate form. create_mandate checked the TOTP code first and only then refused with 409
MANDATE_PAUSED, so the operator's code was spent on a request that could never succeed. Every retry re-sent the used
code and got TOTP_REQUIRED, which made TOTP look broken. Now the stop is checked before the code is spent, and a code
that was already used says TOTP_REUSED.

Runs on a throwaway migrated database (``cryptonite_ci_truth_*``) and the Redis at REDIS_URL, as test_mandate_stop_guard does.
"""

import json
import time
import uuid

import httpx
import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database

BTC = "BTCUSDT.BINANCE"
APPROVE = "/api/v1/agents/brain/sessions/{sid}/cards/card-1/approve"
REUSED = "TOTP_REUSED: this code was already used (each code works once); wait for the next code from your authenticator"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_truth") as url:
        yield url


@pytest.fixture
async def h(db_url):
    # No 000000 bypass here: every code in this module is a real one, or a wrong one on purpose
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


def create_body(*instruments: str, totp: str, name: str | None = None) -> dict:
    return {"name": name or f"truth-{uuid.uuid4().hex[:6]}", "capital_budget_usd": "10000", "instruments": list(instruments),
            "max_position_pct": "10", "totp_code": totp}


@pytest.fixture
def brain_card(monkeypatch):
    """The Brain session worker as the approve route sees it: one BUY card on BTC. Records every note it receives."""
    from cryptonite_api.routes import brain as brain_routes

    card = {"kind": "order_card", "id": "card-1", "instrument_id": BTC, "side": "BUY", "quantity": 0.01,
            "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 58000.0, "take_profit": None,
            "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None}
    notes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        body = json.loads(request.content)
        notes.append(body)
        if body.get("expect_status") and card["status"] != body["expect_status"]:
            return httpx.Response(409)
        card.update({k: body[k] for k in ("status", "command_id") if body.get(k)})
        return httpx.Response(200, json={"status": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card, notes


# --- The card: a stopped mandate is named, not reported as "no mandate" -------------------------------------------

@pytest.mark.parametrize("status", ["PAUSED", "HALTED"])
async def test_approving_a_buy_card_under_a_stopped_mandate_names_the_stop(h, brain_card, status):
    card, notes = brain_card
    name = f"btc-{status.lower()}-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status=status, name=name)
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409
    assert res.json()["detail"] == (f"MANDATE_{status}: {name} already covers {BTC}; "
                                    "resume it on the Mandates page, then approve this card")
    assert card["status"] == "PENDING_APPROVAL" and notes == []  # nothing claimed, nothing queued
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


async def test_approving_a_buy_card_with_no_mandate_still_says_no_mandate(h, brain_card):
    card, notes = brain_card
    async with h.client() as c:
        res = await c.post(APPROVE.format(sid=uuid.uuid4()), headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("NO_MANDATE")
    assert card["status"] == "PENDING_APPROVAL" and notes == []


# --- The mandate editor: a refused create does not spend the operator's code --------------------------------------

async def test_creating_a_mandate_under_a_stopped_mandate_does_not_spend_the_code(h):
    name = f"btc-stopped-{uuid.uuid4().hex[:6]}"
    await h.mandate(instruments=(BTC,), status="PAUSED", name=name)
    code = h.totp.now()
    async with h.client() as c:
        refused = await c.post("/api/v1/ai/mandates", json=create_body("BTCUSDT", totp=code), headers=h.headers)
        assert refused.status_code == 409
        assert refused.json()["detail"] == f"MANDATE_PAUSED: {name} already covers {BTC}; resume or edit it instead"
        assert await h.pool.fetchval("SELECT count(*) FROM totp_replay_cache WHERE user_id = $1", h.operator_id) == 0
        # The same code still opens a free instrument: the refusal did not use it up
        accepted = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
    assert accepted.status_code == 201, accepted.text


# --- TOTP: a used code says TOTP_REUSED, and neither reuse nor a wrong code is counted the wrong way ----------------

async def test_a_totp_code_used_twice_is_refused_as_reused_and_never_locks_the_operator_out(h):
    key = f"ai:totp_failures:user:{h.operator_id}"
    code = h.totp.now()
    async with h.client() as c:
        first = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
        assert first.status_code == 201, first.text
        for _ in range(7):  # more reuses than the 5-failure lockout allows: none of them counts
            again = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=code), headers=h.headers)
            assert again.status_code == 401 and again.json()["detail"] == REUSED
    assert int(await h.redis.get(key) or 0) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status <> 'ARCHIVED'") == 1


async def test_a_wrong_code_is_refused_as_required_and_counted(h):
    key = f"ai:totp_failures:user:{h.operator_id}"
    now = int(time.time())
    valid = {h.totp.at(now + offset) for offset in (-30, 0, 30)}
    wrong = next(c for c in ("000001", "000002") if c not in valid)  # 000000 is the dev code, never a real one
    async with h.client() as c:
        res = await c.post("/api/v1/ai/mandates", json=create_body("ETHUSDT", totp=wrong), headers=h.headers)
    assert res.status_code == 401 and res.json()["detail"].startswith("TOTP_REQUIRED")
    assert int(await h.redis.get(key) or 0) == 1
