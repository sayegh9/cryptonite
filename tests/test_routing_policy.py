"""Per-role model routing (ADR 0007, Phase 2): the rules the editor applies, and the order the PUT refuses in."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES
from cryptonite_api.routes import settings as settings_route
from cryptonite_api.routes.settings import RouteWriteRequest, update_model_route
from cryptonite_api.routing_policy import known_model, route_view, route_violations, same_value
from fastapi import HTTPException


def test_retired_and_unknown_models_are_refused_and_priced_ones_accepted():
    assert not known_model("google:text-embedding-004")  # retired
    assert not known_model("google:made-up-model")
    assert known_model("google:gemini-2.5-flash") and known_model("ollama:gemma3:12b")


def test_local_models_are_only_for_the_roles_adr_0005_moves_local():
    assert route_violations("triage", {"primary_model": "ollama:gemma3:12b"}) == []
    assert route_violations("embed", {"primary_model": "ollama:embeddinggemma"}) == []
    found = route_violations("debate.lite", {"primary_model": "ollama:gemma3:12b"})
    assert found and "only for" in found[0]


def test_policy_and_limits_are_checked():
    assert route_violations("triage", {"fallback_policy": "BOGUS"})
    assert route_violations("triage", {"daily_cap_usd": Decimal("600")})
    assert route_violations("triage", {"max_output_tokens": 0})
    assert route_violations("triage", {"thinking_budget": 0, "daily_cap_usd": Decimal("1.5")}) == []


def test_same_value_compares_numbers_by_value_and_text_exactly():
    assert same_value(Decimal("5.0000"), Decimal("5"))
    assert same_value(None, None) and not same_value(None, "x")
    assert same_value("google:gemini-2.5-flash", "google:gemini-2.5-flash")
    assert not same_value("google:gemini-2.5-flash", "google:gemini-2.5-pro")


def test_the_view_separates_the_default_from_what_is_stored():
    base = DEFAULT_ROUTES["triage"]
    plain = route_view("triage", base, None)
    assert plain["overridden"] is False and plain["stored"] is None and plain["editable"] is True
    row = {"primary_model": "google:gemini-2.5-flash", "fallback_model": None, "fallback_policy": None,
           "daily_cap_usd": Decimal("2.5000"), "max_output_tokens": None, "thinking_budget": None}
    overridden = route_view("triage", base, row)
    assert overridden["overridden"] is True
    assert overridden["effective"]["primary_model"] == "google:gemini-2.5-flash"
    assert overridden["default"]["primary_model"] == base.primary_model  # the default is still shown
    locked = route_view("debate.verdict", DEFAULT_ROUTES["debate.verdict"], None)
    assert locked["protected"] is True and locked["editable"] is False


class _RouteConn:
    def __init__(self, row=None):
        self.row, self.executed = row, []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchrow(self, sql, *args):
        return self.row

    @asynccontextmanager
    async def _tx(self):
        yield None

    def transaction(self):
        return self._tx()


class _RoutePool:
    def __init__(self, row=None):
        self.conn = _RouteConn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _Audit:
    def __init__(self):
        self.events = []

    async def log_audit_event(self, **event):
        self.events.append(event)


def _inserts(pool):
    return [c for c in pool.conn.executed if "INSERT INTO ai_model_routes" in c[0]]


OPERATOR = {"username": "op", "user_id": "00000000-0000-0000-0000-000000000001", "role": "operator", "csrf_token": "tok"}


async def put_route(role, pool, audit, *, totp=None, csrf="tok", **fields):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), client=SimpleNamespace(host="127.0.0.1"))
    body = RouteWriteRequest(reason="route change for the test", totp_code=totp, **fields)
    return await update_model_route(role, body, request, auth=OPERATOR, auth_mgr=audit, x_csrf_token=csrf)


@pytest.mark.asyncio
async def test_protected_and_unknown_roles_are_refused_before_anything_else(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("a refused role never reaches the TOTP question")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, audit = _RoutePool(), _Audit()
    with pytest.raises(HTTPException) as locked:
        await put_route("debate.verdict", pool, audit, primary_model="google:gemini-2.5-flash")
    assert locked.value.status_code == 403
    with pytest.raises(HTTPException) as unknown:
        await put_route("no.such.role", pool, audit, primary_model="google:gemini-2.5-flash")
    assert unknown.value.status_code == 404
    assert pool.conn.executed == [] and audit.events == []


@pytest.mark.asyncio
async def test_a_model_the_gateway_cannot_call_is_refused_before_totp(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("a refused model never reaches the TOTP question")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, audit = _RoutePool(), _Audit()
    with pytest.raises(HTTPException) as refused:
        await put_route("triage", pool, audit, primary_model="google:text-embedding-004")
    assert refused.value.status_code == 422 and pool.conn.executed == []


@pytest.mark.asyncio
async def test_every_route_change_needs_totp_and_writes_only_the_editable_fields(monkeypatch):
    seen = {}

    async def missing_totp(request, auth, code):
        seen["asked"] = True
        raise HTTPException(status_code=401, detail="TOTP_REQUIRED")

    monkeypatch.setattr(settings_route, "require_totp", missing_totp)
    pool, audit = _RoutePool(), _Audit()
    with pytest.raises(HTTPException) as refused:
        await put_route("triage", pool, audit, fallback_policy="FAIL_CLOSED", max_output_tokens=256)
    assert refused.value.status_code == 401 and seen["asked"] and not _inserts(pool)
    assert [e["action"] for e in audit.events] == ["ROUTE_UPDATE_FAILED_AUTH"]

    async def good_totp(request, auth, code):
        seen["code"] = code

    monkeypatch.setattr(settings_route, "require_totp", good_totp)
    pool, audit = _RoutePool(), _Audit()
    out = await put_route("triage", pool, audit, totp="123456", fallback_policy="FAIL_CLOSED", max_output_tokens=256)
    assert out == {"success": True, "role": "triage", "changed": True, "applies_within_s": 30}
    (sql, args), = [c for c in pool.conn.executed if "INSERT INTO ai_model_routes" in c[0]]
    assert args[0] == "triage" and args[3] == "FAIL_CLOSED" and args[4] == 256
    assert "priority" not in sql and "shadow" not in sql and "certified" not in sql  # the fields the editor leaves alone
    assert audit.events[-1]["action"] == "ROUTE_UPDATE" and audit.events[-1]["details"]["totp_verified"] is True


@pytest.mark.asyncio
async def test_an_unchanged_route_writes_nothing(monkeypatch):
    async def no_totp(*args, **kwargs):
        raise AssertionError("an unchanged route asks for nothing")

    monkeypatch.setattr(settings_route, "require_totp", no_totp)
    pool, audit = _RoutePool(), _Audit()
    out = await put_route("triage", pool, audit)  # every field null: the same as no override stored
    assert out["changed"] is False and not _inserts(pool) and audit.events == []
