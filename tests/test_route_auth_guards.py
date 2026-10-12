"""Auth guards on API routes: mutations need operator + CSRF, trading-data reads need a session or machine token."""

import hashlib
import importlib
import pkgutil
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_api.main import create_app
from cryptonite_api.routes.auth import COOKIE_NAME, require_auth, require_csrf, require_operator
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

CSRF = "csrf-test-token"
OPERATOR_SID = "sid-operator"
VIEWER_SID = "sid-viewer"
OPERATOR_TOKEN = "machine-operator-token"
READONLY_TOKEN = "machine-readonly-token"

_ID = str(uuid.uuid4())

PROTECTED_WRITES = [
    ("POST", "/api/v1/strategies"),
    ("POST", "/api/v1/instruments/onboard"),
    ("POST", "/api/v1/agents/debates/trigger"),
    ("POST", "/api/v1/agents/chat"),
    ("POST", "/api/v1/agents/explain"),
    ("POST", "/api/v1/agents/strategist"),
    ("POST", "/api/v1/agents/cro"),
    ("POST", "/api/v1/agents/onchain"),
    ("POST", "/api/v1/alerts"),
    ("DELETE", f"/api/v1/alerts/{_ID}"),
    ("POST", "/api/v1/backtests"),
    ("POST", "/api/v1/scanners"),
    ("PUT", f"/api/v1/scanners/{_ID}"),
    ("DELETE", f"/api/v1/scanners/{_ID}"),
    ("POST", f"/api/v1/scanners/{_ID}/run"),
    ("POST", "/api/v1/strategist/analyze"),
    ("PUT", "/api/v1/strategist/instructions"),
]

PROTECTED_READS = [
    "/api/v1/strategies",
    "/api/v1/strategies/some-slug",
    "/api/v1/alerts",
    "/api/v1/backtests",
    f"/api/v1/backtests/compare?run_ids={_ID}",
    f"/api/v1/backtests/{_ID}",
    f"/api/v1/backtests/{_ID}/results",
    f"/api/v1/backtests/{_ID}/trades",
    f"/api/v1/backtests/{_ID}/manifest",
    "/api/v1/scanners",
    f"/api/v1/scanners/jobs/{_ID}",
    "/api/v1/signals",
    f"/api/v1/signals/{_ID}",
    "/api/v1/strategist/instructions",
    "/api/v1/control/risk-limits",
]

# Mutations outside the /auth router that enforce CSRF inline (control.verify_csrf) rather than via require_csrf.
INLINE_CSRF_WRITES = {
    ("POST", "/api/v1/control/kill-switch/trip"),
    ("POST", "/api/v1/control/kill-switch/reset"),
    ("POST", "/api/v1/control/risk-limits"),
    ("PUT", "/api/v1/settings/registry/{key}"),
    ("PUT", "/api/v1/settings/routes/{role}"),  # ADR 0007 route write: inline CSRF, like the registry write  # ADR 0007 write path: refuses any caller without the session's CSRF token
}

# Machine-to-machine writes authenticated by an HMAC signature + single-use nonce instead of an
# operator session (tested in test_notifier.py): Telegram decisions relayed by the notifier (A5.4).
SIGNED_MACHINE_WRITES = {
    ("POST", "/api/v1/ai/notifier/approvals/{approval_id}"),
}


class _FakeConn:
    def __init__(self, tokens: dict[str, list[str]]):
        self._tokens = tokens

    async def fetchrow(self, _sql, token_hash):
        for plaintext, scopes in self._tokens.items():
            if hashlib.sha256(plaintext.encode()).hexdigest() == token_hash:
                return {"id": 1, "scopes": scopes}
        return None

    async def execute(self, *_args):
        return None


class _FakePool:
    def __init__(self, tokens):
        self._conn = _FakeConn(tokens)

    @asynccontextmanager
    async def acquire(self):
        yield self._conn


class _FakeAuthManager:
    def __init__(self):
        self._sessions = {
            OPERATOR_SID: {"user_id": "u-op", "username": "op", "role": "operator", "csrf_token": CSRF},
            VIEWER_SID: {"user_id": "u-view", "username": "viewer", "role": "viewer", "csrf_token": CSRF},
        }
        self._pool = _FakePool({OPERATOR_TOKEN: ["operator"], READONLY_TOKEN: ["operator:read"]})

    async def get_session(self, session_id):
        session = self._sessions.get(session_id)
        return dict(session) if session else None


@pytest.fixture
def app():
    app = create_app()
    app.state.auth_manager = _FakeAuthManager()
    app.state.pool = MagicMock()
    app.state.redis = MagicMock(xadd=AsyncMock(return_value="1-0"))
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _send(client, method, path, cookies=None, headers=None):
    client.cookies.clear()
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value)
    return client.request(method, path, json={}, headers=headers or {})


@pytest.mark.parametrize("method,path", PROTECTED_WRITES)
def test_write_without_credentials_is_401(client, method, path):
    assert _send(client, method, path).status_code == 401


@pytest.mark.parametrize("method,path", PROTECTED_WRITES)
def test_write_with_non_operator_session_is_403(client, method, path):
    res = _send(client, method, path, cookies={COOKIE_NAME: VIEWER_SID}, headers={"X-CSRF-Token": CSRF})
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", PROTECTED_WRITES)
def test_write_with_operator_session_but_no_csrf_is_403(client, method, path):
    res = _send(client, method, path, cookies={COOKIE_NAME: OPERATOR_SID})
    assert res.status_code == 403


@pytest.mark.parametrize("method,path", PROTECTED_WRITES)
def test_write_with_machine_token_lacking_operator_scope_is_403(client, method, path):
    assert _send(client, method, path, headers={"X-API-Token": READONLY_TOKEN}).status_code == 403


@pytest.mark.parametrize("path", PROTECTED_READS)
def test_read_without_credentials_is_401(client, path):
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_invalid_session_and_unknown_machine_token_are_401(client):
    path = "/api/v1/agents/debates/trigger"
    assert _send(client, "POST", path, cookies={COOKIE_NAME: "nope"}).status_code == 401
    assert _send(client, "POST", path, headers={"X-API-Token": "nope"}).status_code == 401


def test_debate_trigger_accepts_operator_session_with_csrf(client, app):
    client.cookies.set(COOKIE_NAME, OPERATOR_SID)
    res = client.post("/api/v1/agents/debates/trigger", json={"symbol": "BTCUSDT"}, headers={"X-CSRF-Token": CSRF})
    assert res.status_code == 200, res.text
    assert res.json()["instrument_id"] == "BTCUSDT.BINANCE"
    app.state.redis.xadd.assert_awaited_once()


def test_debate_trigger_accepts_operator_machine_token_without_csrf(client, app):
    """The agent tool (stage_debate_proposal) authenticates with an operator-scoped machine token."""
    client.cookies.clear()
    res = client.post("/api/v1/agents/debates/trigger", json={"symbol": "ETHUSDT"}, headers={"X-API-Token": OPERATOR_TOKEN})
    assert res.status_code == 200, res.text
    app.state.redis.xadd.assert_awaited_once()


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def _api_routes():
    """Every APIRoute declared in cryptonite_api.routes (FastAPI 0.141 hides included routes behind _IncludedRouter)."""
    import cryptonite_api.routes as routes_pkg

    routes = []
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        router = getattr(importlib.import_module(f"cryptonite_api.routes.{info.name}"), "router", None)
        if router is not None:
            routes.extend(r for r in router.routes if isinstance(r, APIRoute))
    return routes


def test_every_api_mutation_requires_operator_and_csrf():
    """Regression guard: a new POST/PUT/PATCH/DELETE route must declare operator auth and CSRF."""
    checked, unguarded = set(), []
    for route in _api_routes():
        if route.path.startswith("/api/v1/auth/"):
            continue
        calls = set(_dependency_calls(route.dependant))
        for method in route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            checked.add((method, route.path))
            if (method, route.path) in SIGNED_MACHINE_WRITES:
                continue
            if require_operator not in calls:
                unguarded.append((method, route.path, "operator"))
            if require_csrf not in calls and (method, route.path) not in INLINE_CSRF_WRITES:
                unguarded.append((method, route.path, "csrf"))
    assert {("POST", "/api/v1/strategies"), ("POST", "/api/v1/agents/debates/trigger")} <= checked
    assert unguarded == []


def test_protected_reads_declare_require_auth():
    reads = [r for r in _api_routes() if "GET" in r.methods]
    for path in PROTECTED_READS:
        route = next(r for r in reads if r.path_regex.match(path.split("?")[0]))
        assert require_auth in set(_dependency_calls(route.dependant)), route.path


@pytest.mark.parametrize("path", [
    "/api/v1/ai/approvals/00000000-0000-4000-8000-000000000001/decision",
    "/api/v1/ai/mandates",
    "/api/v1/agents/brain/sessions/00000000-0000-4000-8000-000000000001/cards/c1/approve",
    "/api/v1/agents/debates/00000000-0000-4000-8000-000000000001/approve",
])
def test_machine_tokens_cannot_approve_ai_actions_or_widen_mandates(client, path):
    """A5: the agent workers hold an operator-scoped token; approvals need a human operator session."""
    res = _send(client, "POST", path, headers={"X-API-Token": OPERATOR_TOKEN})
    assert res.status_code == 403
