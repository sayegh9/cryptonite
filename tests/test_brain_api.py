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
        if request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"sessions": []})
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
    assert c.get("/api/v1/agents/brain/sessions").json() == {"sessions": []}
    assert worker[-1].url.path == "/api/v1/brain/operators/u-op/sessions"
    assert c.get("/api/v1/agents/brain/sessions/not-a-uuid").status_code == 422


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


class _Redis:
    def __init__(self, value):
        self.value = value

    async def get(self, key):
        assert key == "exit_monitor:heartbeat"
        return self.value


class _Hub:
    def __init__(self, value):
        self.redis = _Redis(value)


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
