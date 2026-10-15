"""API side of Brain Home (blueprint A3.3–A3.5): operator auth, HMAC relay to agent_worker, trace reads."""

import datetime
import hashlib
import hmac
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from cryptonite_api.main import create_app
from cryptonite_api.routes import brain as brain_routes
from cryptonite_api.routes.agents import INTERNAL_HMAC_SECRET
from cryptonite_api.routes.auth import COOKIE_NAME
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse
from fastapi.testclient import TestClient
from test_route_auth_guards import CSRF, OPERATOR_SID, VIEWER_SID, _FakeAuthManager

SSE = b'event: session\ndata: {"session_id": "s1"}\n\nevent: token\ndata: {"text": "hi"}\n\nevent: done\ndata: {}\n\n'


def _valid_signature(request: httpx.Request) -> bool:
    h = request.headers
    digest = hashlib.sha256(request.content).hexdigest()
    msg = f"{request.method}:{request.url.path}:{digest}:{h['X-Internal-Timestamp']}:{h['X-Internal-Nonce']}"
    expected = hmac.new(INTERNAL_HMAC_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h["X-Internal-Signature"])


class _TraceConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetchrow(self, _sql, trace_id):
        return self.rows.get(trace_id)

    async def fetch(self, _sql, parent_id):
        return [r for r in self.rows.values() if r["parent_id"] == parent_id]


class _TracePool:
    def __init__(self, rows):
        self.conn = _TraceConn(rows)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def worker(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if not _valid_signature(request):
            return httpx.Response(401)
        if request.url.path == "/api/v1/brain/stream":
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        if request.url.path.endswith("/sessions/search"):
            return httpx.Response(200, json={"sessions": [], "next_cursor": None})
        if request.method == "PATCH" and "/sessions/" in request.url.path:
            return httpx.Response(200, json={"session": {"id": request.url.path.rsplit("/", 1)[-1],
                                                         **json.loads(request.content)}})
        if "/notes" in request.url.path:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


@pytest.fixture
def client():
    app = create_app()
    app.state.auth_manager = _FakeAuthManager()
    return TestClient(app), app


def test_stream_requires_operator_and_csrf(client, worker):
    c, _ = client
    assert c.post("/api/v1/agents/brain/stream", json={"message": "hi"}).status_code == 401
    c.cookies.set(COOKIE_NAME, VIEWER_SID)
    assert c.post("/api/v1/agents/brain/stream", json={"message": "hi"}, headers={"X-CSRF-Token": CSRF}).status_code == 403
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    assert c.post("/api/v1/agents/brain/stream", json={"message": "hi"}).status_code == 403  # no CSRF
    assert worker == []


def test_stream_relays_sse_signed_with_the_operator_identity(client, worker):
    c, _ = client
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = c.post("/api/v1/agents/brain/stream", json={"message": "analyze BTC"}, headers={"X-CSRF-Token": CSRF})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
    assert res.content == SSE
    sent = json.loads(worker[0].content)
    assert sent == {"operator_id": "u-op", "message": "analyze BTC", "session_id": None}


def test_sessions_are_scoped_to_the_logged_in_operator(client, worker):
    c, _ = client
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    assert c.get("/api/v1/agents/brain/sessions").json() == {"sessions": [], "next_cursor": None}
    assert worker[-1].url.path == "/api/v1/brain/operators/u-op/sessions/search"
    assert c.get("/api/v1/agents/brain/sessions/not-a-uuid").status_code == 422


def test_history_filters_travel_in_the_signed_body(client, worker):
    c, _ = client
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = c.get("/api/v1/agents/brain/sessions", params={"q": "btc", "archived": "true", "limit": 5, "before": "x|y"})
    assert res.status_code == 200
    sent = worker[-1]
    assert sent.method == "POST" and not sent.url.query  # nothing outside the signature
    assert json.loads(sent.content) == {"query": "btc", "archived": True, "limit": 5, "before": "x|y"}
    assert c.get("/api/v1/agents/brain/sessions", params={"limit": 500}).status_code == 422


def test_rename_and_archive_need_an_operator_and_csrf(client, worker):
    c, _ = client
    sid = str(uuid.uuid4())
    url = f"/api/v1/agents/brain/sessions/{sid}"
    assert c.patch(url, json={"title": "BTC plan"}).status_code == 401
    c.cookies.set(COOKIE_NAME, VIEWER_SID)
    assert c.patch(url, json={"title": "BTC plan"}, headers={"X-CSRF-Token": CSRF}).status_code == 403
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    assert c.patch(url, json={"title": "BTC plan"}).status_code == 403  # no CSRF
    assert worker == []

    res = c.patch(url, json={"title": "BTC plan"}, headers={"X-CSRF-Token": CSRF})
    assert res.status_code == 200 and res.json()["session"]["title"] == "BTC plan"
    assert worker[-1].method == "PATCH"
    assert worker[-1].url.path == f"/api/v1/brain/operators/u-op/sessions/{sid}"
    assert json.loads(worker[-1].content) == {"title": "BTC plan"}

    assert c.patch(url, json={"archived": True}, headers={"X-CSRF-Token": CSRF}).json()["session"]["archived"] is True
    assert c.patch(url, json={}, headers={"X-CSRF-Token": CSRF}).status_code == 422
    assert c.patch(url, json={"title": ""}, headers={"X-CSRF-Token": CSRF}).status_code == 422
    assert c.patch(url, json={"title": "x" * 121}, headers={"X-CSRF-Token": CSRF}).status_code == 422


def test_note_forwards_only_display_fields(client, worker):
    c, _ = client
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    sid = uuid.uuid4()
    res = c.post(f"/api/v1/agents/brain/sessions/{sid}/notes", headers={"X-CSRF-Token": CSRF},
                 json={"content": "rejected", "widget_id": "c1", "status": "REJECTED"})
    assert res.status_code == 200
    sent = json.loads(worker[-1].content)
    assert sent["operator_id"] == "u-op" and sent["expect_status"] == "PENDING_APPROVAL"  # only from pending
    for status in ("EXECUTE_NOW", "APPROVED"):  # approval only through the approve endpoint
        bad = c.post(f"/api/v1/agents/brain/sessions/{sid}/notes", headers={"X-CSRF-Token": CSRF},
                     json={"content": "x", "widget_id": "c1", "status": status})
        assert bad.status_code == 422


def test_trace_endpoint_returns_trace_with_children(client):
    c, app = client
    root, child = uuid.uuid4(), uuid.uuid4()
    now = datetime.datetime.now(datetime.UTC)

    def row(i, parent, role):
        return {"id": i, "parent_id": parent, "role": role, "model": "google:gemini-2.5-pro", "session_id": "s1",
                "mandate_id": None, "status": "OK", "tool_calls": json.dumps([{"tool": "propose_order", "status": "DENIED"}]),
                "output": json.dumps("staged"), "error": None, "cost_usd": Decimal("0.0123"), "latency_ms": 900,
                "created_at": now}

    app.state.pool = _TracePool({root: row(root, None, "brain.plan"), child: row(child, root, "summarize")})
    assert c.get(f"/api/v1/agents/brain/traces/{root}").status_code == 401
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    body = c.get(f"/api/v1/agents/brain/traces/{root}").json()
    assert body["trace"]["tool_calls"][0]["status"] == "DENIED" and body["trace"]["cost_usd"] == "0.0123"
    assert [ch["role"] for ch in body["children"]] == ["summarize"]
    assert c.get(f"/api/v1/agents/brain/traces/{uuid.uuid4()}").status_code == 404


# --- Order card approval (server-side, idempotent, protected by the exit monitor) ---

CARD = {"kind": "order_card", "id": "card-1", "instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "quantity": 0.01,
        "order_type": "MARKET", "limit_price": None, "allocation_pct": 5.0, "stop_loss": 58000.0,
        "take_profit": None, "status": "PENDING_APPROVAL", "trace_id": str(uuid.uuid4()), "command_id": None}


FED_UNIVERSE = IngestUniverse.build(["BTCUSDT"], ["1m"]).model_dump_json()  # what ingest publishes for BTC


class _Redis:
    """The exit monitor heartbeat, and the ingest universe (None: unknown, as after a TTL expiry)."""

    def __init__(self, value, universe=FED_UNIVERSE):
        self.value = value
        self.universe = universe

    async def get(self, key):
        if key == INGEST_UNIVERSE_KEY:
            return self.universe
        assert key == "exit_monitor:heartbeat"
        return self.value


class _Hub:
    def __init__(self, value, universe=FED_UNIVERSE):
        self.redis = _Redis(value, universe)


class _CommandPool:
    """execution_commands with its unique idempotency key."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def fetchrow(self, _sql, cmd_id, key, ctype, target, requester, payload, env, account):
        if key in self.rows:
            import asyncpg
            raise asyncpg.UniqueViolationError("duplicate key")
        self.rows[key] = {"command_id": cmd_id, "command_type": ctype, "target": target, "requester": requester,
                          "payload": json.loads(payload), "status": "PENDING", "created_at": None}
        return self.rows[key]

    async def fetchval(self, _sql, key):
        return self.rows[key]["command_id"]


@pytest.fixture
def card_worker(monkeypatch):
    """A worker holding one session with one card; notes honour expect_status like the real store."""
    card = dict(CARD)
    notes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not _valid_signature(request):
            return httpx.Response(401)
        if request.method == "GET" and "/sessions/" in request.url.path:
            return httpx.Response(200, json={"session": {}, "messages": [{"role": "assistant", "widgets": [card]}]})
        if "/notes" in request.url.path:
            body = json.loads(request.content)
            notes.append(body)
            if body.get("expect_status") and card["status"] != body["expect_status"]:
                return httpx.Response(409, json={"detail": "not pending"})
            if body.get("status"):
                card["status"] = body["status"]
            if body.get("command_id"):
                card["command_id"] = body["command_id"]
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(brain_routes.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return card, notes


def _approve(c, sid):
    return c.post(f"/api/v1/agents/brain/sessions/{sid}/cards/card-1/approve", headers={"X-CSRF-Token": CSRF})


HEARTBEAT = json.dumps({"stop_loss_pct": 5.0, "trail_activate_pct": 3.0, "trail_pct": 2.0})


# The approve-and-queue path (mandate, ai_approvals row, one bound command, idempotent retries, a crash
# between the claim and the queue) runs against a real database in test_ai_approvals.py (A5.3).


def test_approval_is_refused_while_the_exit_monitor_is_down(client, card_worker):
    c, app = client
    card, notes = card_worker
    app.state.pool, app.state.stream_hub = _CommandPool(), _Hub(None)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = _approve(c, uuid.uuid4())
    assert res.status_code == 409 and "EXIT_MONITOR_DOWN" in res.json()["detail"]
    assert app.state.pool.rows == {} and card["status"] == "PENDING_APPROVAL" and notes == []


def test_rejected_cards_and_non_operators_cannot_approve(client, card_worker):
    c, app = client
    card, _ = card_worker
    app.state.pool, app.state.stream_hub = _CommandPool(), _Hub(HEARTBEAT)
    assert _approve(c, uuid.uuid4()).status_code == 401
    c.cookies.set(COOKIE_NAME, VIEWER_SID)
    assert _approve(c, uuid.uuid4()).status_code == 403
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    card["status"] = "REJECTED"
    assert _approve(c, uuid.uuid4()).status_code == 409
    assert app.state.pool.rows == {}


# --- What an approved card's order did (2026-10-13: a refused order kept reading "Approved") ----------------------

from cryptonite_api.brain_outcomes import card_patch, client_order_id, command_outcome  # noqa: E402

CMD = "6dd7c0ed-f5f4-4b9d-b3f4-2745c3f194fa"


class _OutcomeConn:
    """execution_commands and paper_orders for one command."""

    def __init__(self, command=None, order=None):
        self.command = command
        self.order = order
        self.order_ids: list[str] = []

    async def fetchrow(self, sql, *args):
        if "FROM execution_commands" in sql:
            return self.command
        if "FROM paper_orders" in sql:
            self.order_ids.append(args[0])
            return self.order
        return None


class _OutcomePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _cmd(status, error=None):
    return {"status": status, "error_message": error, "payload": json.dumps({"side": "BUY"})}


def _order(status, filled="0", avg=None, price="1.3825", reason=None):
    return {"status": status, "quantity": Decimal("361.8"), "filled_qty": Decimal(filled),
            "avg_fill_price": Decimal(avg) if avg else None, "price": Decimal(price), "reject_reason": reason}


@pytest.mark.parametrize("command, order, expected", [
    (_cmd("PENDING"), None, {"state": "QUEUED"}),
    (_cmd("REJECTED", "MANDATE_SIZING_CONFLICT"), None, {"state": "NOT_EXECUTED", "reason": "MANDATE_SIZING_CONFLICT"}),
    (_cmd("FAILED"), None, {"state": "NOT_EXECUTED", "reason": "command failed"}),
    (_cmd("COMPLETED"), None, {"state": "QUEUED"}),
    (_cmd("COMPLETED"), _order("FILLED", "361.8", "1.3821"),
     {"state": "FILLED", "filled_qty": "361.8", "avg_fill_price": "1.3821"}),
    (_cmd("COMPLETED"), _order("ACCEPTED"), {"state": "WORKING", "limit_price": "1.3825", "filled_qty": None,
                                              "avg_fill_price": None}),
    (_cmd("COMPLETED"), _order("CANCELED", reason="PRICE_COLLAR_REJECT"),
     {"state": "NOT_EXECUTED", "reason": "PRICE_COLLAR_REJECT"}),
])
async def test_an_approved_cards_order_maps_to_one_state(command, order, expected):
    conn = _OutcomeConn(command, order)
    assert await command_outcome(conn, CMD) == expected
    if command["status"] == "COMPLETED":
        assert conn.order_ids == ["AI-6DD7C0EDF5F4"]  # the orchestrator's client order id for an AI command


async def test_a_partly_filled_then_cancelled_order_is_a_position():
    out = await command_outcome(_OutcomeConn(_cmd("COMPLETED"), _order("CANCELED", "100", "1.38")), CMD)
    assert out["state"] == "FILLED" and out["filled_qty"] == "100" and "partly filled" in out["reason"]


def test_a_card_moves_forward_once_and_never_back():
    card = {"id": "card-1", "side": "BUY", "instrument_id": "XRPUSDT.BINANCE", "command_id": CMD, "status": "APPROVED"}
    note = card_patch(card, {"state": "NOT_EXECUTED", "reason": "MANDATE_SIZING_CONFLICT"})
    assert note["status"] == "NOT_EXECUTED" and note["expect_status"] == "APPROVED"
    assert "NOT EXECUTED: MANDATE_SIZING_CONFLICT" in note["content"] and "No position was opened" in note["content"]
    assert card_patch(card, {"state": "QUEUED"}) is None  # nothing to say yet
    assert card_patch({**card, "status": "WORKING"}, {"state": "WORKING"}) is None  # unchanged
    assert client_order_id(CMD, {"client_order_id": "X-1"}) == "X-1"


def test_loading_a_conversation_marks_a_refused_order_not_executed_once(client, card_worker):
    c, app = client
    card, notes = card_worker
    card.update(status="APPROVED", command_id=CMD)
    app.state.pool = _OutcomePool(_OutcomeConn(_cmd("REJECTED", "MANDATE_SIZING_CONFLICT")))
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    sid = str(uuid.uuid4())

    assert c.get(f"/api/v1/agents/brain/sessions/{sid}").status_code == 200
    assert card["status"] == "NOT_EXECUTED"
    (note,) = notes
    assert note["expect_status"] == "APPROVED" and note["execution_reason"] == "MANDATE_SIZING_CONFLICT"
    assert "NOT EXECUTED" in note["content"]

    c.get(f"/api/v1/agents/brain/sessions/{sid}")
    assert len(notes) == 1  # a final state is recorded once


def test_the_card_outcome_endpoint_follows_a_working_limit_to_its_fill(client, card_worker):
    c, app = client
    card, notes = card_worker
    card.update(status="APPROVED", command_id=CMD)
    conn = _OutcomeConn(_cmd("COMPLETED"), _order("ACCEPTED"))
    app.state.pool = _OutcomePool(conn)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    url = f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/{card['id']}/outcome"

    first = c.get(url).json()
    assert first["status"] == "WORKING" and first["state"] == "WORKING" and card["status"] == "WORKING"
    conn.order = _order("FILLED", "361.8", "1.3821")
    second = c.get(url).json()
    assert second["status"] == "FILLED" and second["filled_qty"] == "361.8"
    assert [n["status"] for n in notes] == ["WORKING", "FILLED"]
    assert c.get(f"/api/v1/agents/brain/sessions/{uuid.uuid4()}/cards/nope/outcome").status_code == 404


# --- An un-fed symbol is refused before anything is claimed (WS-1C) -----------------------------------------------

def test_approval_of_a_card_for_an_unfed_symbol_is_refused_before_anything_is_claimed(client, card_worker):
    c, app = client
    card, notes = card_worker
    card["instrument_id"] = "SOLUSDT.BINANCE"  # SOL is not in the ingest feed (the SOLUSDT incident)
    app.state.pool, app.state.stream_hub = _CommandPool(), _Hub(HEARTBEAT)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = _approve(c, uuid.uuid4())
    assert res.status_code == 422 and "SYMBOL_NOT_INGESTED" in res.json()["detail"]
    assert card["status"] == "PENDING_APPROVAL" and notes == [] and app.state.pool.rows == {}


def test_approval_waits_while_the_ingest_universe_is_unknown(client, card_worker):
    c, app = client
    card, notes = card_worker
    app.state.pool, app.state.stream_hub = _CommandPool(), _Hub(HEARTBEAT, universe=None)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = _approve(c, uuid.uuid4())
    assert res.status_code == 503 and "INGEST_UNIVERSE_UNKNOWN" in res.json()["detail"]
    assert card["status"] == "PENDING_APPROVAL" and notes == [] and app.state.pool.rows == {}


class _Acquire:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class _MandatePool(_CommandPool):
    def acquire(self):
        return _Acquire()


@pytest.mark.parametrize("universe", [FED_UNIVERSE, None], ids=["SOL not fed", "universe unknown"])
def test_a_sell_card_is_not_held_up_by_the_feed(client, card_worker, monkeypatch, universe):
    c, app = client
    card, notes = card_worker
    card.update(instrument_id="SOLUSDT.BINANCE", side="SELL")  # a close of a holding, never gated by the feed
    app.state.pool, app.state.stream_hub = _MandatePool(), _Hub(HEARTBEAT, universe=universe)

    async def no_mandate(conn, instrument_id, mandate_id):
        return None, "NO_MANDATE"  # reached only if the feed check was skipped

    monkeypatch.setattr(brain_routes, "resolve_mandate_for", no_mandate)
    c.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = _approve(c, uuid.uuid4())
    assert res.status_code == 409 and "NO_MANDATE" in res.json()["detail"]
    assert card["status"] == "PENDING_APPROVAL" and notes == [] and app.state.pool.rows == {}
