"""F2 shadow mode: a candidate model answers sampled live calls after the primary, logged and never acted on.

Gateway behaviour runs against a recording fake pool (no DB) and pydantic-ai test models (no network,
no paid model); rows, grants, the report endpoint and migration 036 run on a throwaway database at head.
"""

import asyncio
import datetime
import json
import os
import random
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptonite_agents.gateway import (
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    RiskClass,
    RoleRoute,
)
from cryptonite_agents.gateway import shadow as sh
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.registry import ToolSpec
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES, PROTECTED_ROLES, route_from_row
from cryptonite_api import shadow as report
from cryptonite_schemas.debate import PortfolioDecision, TriageDecision
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

REPO = Path(__file__).resolve().parents[1]
PRIMARY, SHADOW = "test:primary", "test:shadow"
PAID_SHADOW = "google:gemini-2.5-flash"  # served by a FunctionModel below; costed at the Flash price
D = Decimal


# --- a pool that records every statement -------------------------------------------------------

class _Ctx:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, sink: list):
        self.sink = sink

    async def fetch(self, *args):
        return []

    async def fetchval(self, *args):
        return None

    async def execute(self, sql, *args):
        self.sink.append((sql, args))

    def transaction(self):
        return _Ctx()


class FakePool:
    def __init__(self):
        self.statements: list = []

    def acquire(self):
        return _Ctx(FakeConn(self.statements))

    def shadow_rows(self) -> list[dict]:
        cols = ("id", "role", "primary_model", "shadow_model", "status", "primary_trace_id", "parent_trace_id",
                "primary_output", "shadow_output", "key_field", "agrees", "primary_latency_ms", "shadow_latency_ms",
                "primary_cost_usd", "shadow_cost_usd", "error")
        rows = [dict(zip(cols, args, strict=True)) for sql, args in self.statements if "INSERT INTO ai_shadow_results" in sql]
        for r in rows:
            r["primary_output"], r["shadow_output"] = json.loads(r["primary_output"]), json.loads(r["shadow_output"])
        return rows

    def ledger_roles(self) -> list[str]:
        return [args[1] for sql, args in self.statements if "INSERT INTO llm_cost_ledger" in sql]


def gw(rate=1.0, shadow=SHADOW, pool=None, budget=None, priority=BudgetPriority.DEBATE, seed=1, allow_paid=False,
       role="r") -> Gateway:
    route = RoleRoute(role, PRIMARY, priority=priority, fallback_policy=FallbackPolicy.FAIL_CLOSED,
                      shadow_model=shadow, shadow_sample_rate=rate)
    return Gateway(pool=pool if pool is not None else FakePool(), routes={role: route},
                   budget=budget or BudgetGuard(), allow_paid_shadow=allow_paid, shadow_rng=random.Random(seed))


def triage_model(worth: bool, delay_s: float = 0.0) -> FunctionModel:
    async def fn(messages, info: AgentInfo):
        if delay_s:
            await asyncio.sleep(delay_s)
        args = {"worth_debate": worth, "confidence": 0.8, "reason": "r"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])
    return FunctionModel(fn)


def raising_model() -> FunctionModel:
    def fn(messages, info: AgentInfo):
        raise RuntimeError("shadow exploded")
    return FunctionModel(fn)


# --- the primary is never affected --------------------------------------------------------------

async def test_slow_shadow_never_delays_or_changes_the_primary():
    pool = FakePool()
    g = gw(pool=pool)
    with override_model(PRIMARY, triage_model(True)), override_model(SHADOW, triage_model(False, delay_s=1.5)):
        started = time.monotonic()
        res = await g.run("r", "brief", output_type=TriageDecision)
        elapsed = time.monotonic() - started
        assert elapsed < 0.75  # the shadow sleeps 1.5 s
        assert res.output == TriageDecision(worth_debate=True, confidence=0.8, reason="r")
        assert (res.model, res.fell_back) == (PRIMARY, False)
        assert pool.shadow_rows() == []  # still running in the background
        await g.drain_shadows(5)
    [row] = pool.shadow_rows()
    assert res.output.worth_debate is True  # the shadow's "false" never reached the caller
    assert (row["status"], row["key_field"], row["agrees"]) == ("OK", "worth_debate", False)
    assert row["primary_trace_id"] == res.trace_id and row["shadow_latency_ms"] >= 1400
    assert row["shadow_output"]["worth_debate"] is False and row["primary_output"]["worth_debate"] is True


async def test_failing_shadow_never_fails_the_primary():
    pool = FakePool()
    g = gw(pool=pool)
    with override_model(PRIMARY, TestModel(custom_output_text="primary answer")), override_model(SHADOW, raising_model()):
        res = await g.run("r", "hello")
        await g.drain_shadows(5)
    assert res.output == "primary answer"
    [row] = pool.shadow_rows()
    assert row["status"] == "ERROR" and "shadow exploded" in row["error"] and row["agrees"] is None
    assert pool.ledger_roles().count("shadow:r") == 1  # the failed shadow call is still costed


async def test_a_broken_shadow_scheduler_never_fails_the_primary(monkeypatch):
    g = gw()
    monkeypatch.setattr(g, "_shadow_rng", None)  # .random() raises inside _maybe_shadow
    with override_model(PRIMARY, TestModel(custom_output_text="ok")):
        assert (await g.run("r", "x")).output == "ok"


async def test_stream_calls_are_shadowed_too():
    pool = FakePool()
    g = gw(pool=pool)
    with override_model(PRIMARY, TestModel(custom_output_text="streamed")), override_model(SHADOW, TestModel(custom_output_text="s")):
        events = [e async for e in g.stream("r", "x")]
        await g.drain_shadows(5)
    assert events[-1].result.output == "streamed"
    [row] = pool.shadow_rows()
    assert (row["status"], row["primary_trace_id"], row["agrees"]) == ("OK", events[-1].result.trace_id, None)


async def test_pinned_calls_and_pool_less_gateways_are_not_shadowed():
    pool = FakePool()
    g = gw(pool=pool)
    calls = []
    with override_model(PRIMARY, TestModel(custom_output_text="a")), \
         override_model(SHADOW, FunctionModel(lambda m, i: calls.append(1) or ModelResponse(parts=[TextPart("s")]))):
        await g.run("r", "x", model_override=PRIMARY)  # evals / operator overrides
        no_pool = Gateway(routes=g._defaults, budget=BudgetGuard(), shadow_rng=random.Random(1))
        await no_pool.run("r", "x")
        await g.drain_shadows(5)
        await no_pool.drain_shadows(5)
    assert calls == [] and pool.shadow_rows() == []


# --- the shadow never acts ------------------------------------------------------------------------

async def test_shadow_gets_only_read_tools_without_side_effects():
    ran: list[str] = []
    offered: list[list[str]] = []

    def read_prices(symbol: str) -> str:
        """Read prices."""
        ran.append("read_prices")
        return "100"

    def stage_debate_proposal(instrument_id: str) -> str:
        """Stage a debate."""
        ran.append("stage_debate_proposal")
        return "staged"

    def place_order(symbol: str) -> str:
        """Place an order."""
        ran.append("place_order")
        return "placed"

    tools = [ToolSpec("read_prices", read_prices, RiskClass.READ, "Read prices."),
             ToolSpec("stage_debate_proposal", stage_debate_proposal, RiskClass.READ, "Stage.", side_effects=True),
             ToolSpec("place_order", place_order, RiskClass.ADD_RISK, "Place an order."),
             ToolSpec("close_position", place_order, RiskClass.REDUCE_RISK, "Close.")]

    def shadow_fn(messages, info: AgentInfo):
        offered.append(sorted(t.name for t in info.function_tools))
        if any(getattr(p, "part_kind", "") == "tool-return" for m in messages for p in getattr(m, "parts", [])):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(t.name, {"symbol": "BTC"}) for t in info.function_tools])

    pool = FakePool()
    g = gw(pool=pool)
    with override_model(PRIMARY, TestModel(call_tools=[], custom_output_text="primary")), \
         override_model(SHADOW, FunctionModel(shadow_fn)):
        res = await g.run("r", "go", tools=tools)
        await g.drain_shadows(5)
    assert res.output == "primary"
    assert offered[0] == ["read_prices"]
    assert ran == ["read_prices"]  # nothing with side effects or risk ran, in the primary or the shadow
    assert pool.shadow_rows()[0]["status"] == "OK"
    # and the guard refuses anything else even if a tool slipped through
    for spec in tools[1:]:
        assert sh.shadow_guard(spec) is not None
    assert sh.shadow_guard(tools[0]) is None


# --- sampling ------------------------------------------------------------------------------------

async def test_sample_rate_is_respected_with_a_seeded_rng():
    rng = random.Random(42)
    expected = sum(1 for _ in range(200) if rng.random() < 0.25)
    pool = FakePool()
    g = gw(rate=0.25, pool=pool, seed=42)
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model(SHADOW, TestModel(custom_output_text="s")):
        for _ in range(200):
            await g.run("r", "x")
            await g.drain_shadows(5)  # keep under the in-flight cap
    assert len(pool.shadow_rows()) == expected
    assert 30 <= expected <= 70  # and the seeded draw is near 25%


@pytest.mark.parametrize("rate, shadowed", [(0.0, 0), (1.0, 5)])
async def test_rate_zero_never_and_rate_one_always(rate, shadowed):
    pool = FakePool()
    g = gw(rate=rate, pool=pool)
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model(SHADOW, TestModel(custom_output_text="s")):
        for _ in range(5):
            await g.run("r", "x")
        await g.drain_shadows(5)
    assert len(pool.shadow_rows()) == shadowed


async def test_in_flight_cap_skips_instead_of_piling_up():
    pool = FakePool()
    g = gw(pool=pool)
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model(SHADOW, triage_model(True, delay_s=0.5)):
        for _ in range(sh.MAX_SHADOW_INFLIGHT + 2):
            await g.run("r", "x")
        await g.drain_shadows(5)
    rows = pool.shadow_rows()
    assert len(rows) == sh.MAX_SHADOW_INFLIGHT + 2
    assert sum(1 for r in rows if r["error"] == "SHADOW_CONCURRENCY_LIMIT") == 2


# --- budget --------------------------------------------------------------------------------------

async def test_budget_guard_skips_the_shadow_first():
    guard = BudgetGuard(default_cap_usd=D("25"))
    await guard.record("x", D("18"))  # 72%: past the degrade threshold, below the 80% optional stop
    await guard.check(RoleRoute("o", PRIMARY, priority=BudgetPriority.OPTIONAL))  # the least important role still runs
    calls = []
    pool = FakePool()
    g = gw(pool=pool, budget=guard, priority=BudgetPriority.CRITICAL)
    with override_model(PRIMARY, TestModel(custom_output_text="p")), \
         override_model(SHADOW, FunctionModel(lambda m, i: calls.append(1) or ModelResponse(parts=[TextPart("s")]))):
        assert (await g.run("r", "x")).output == "p"
        await g.drain_shadows(5)
    [row] = pool.shadow_rows()
    assert (row["status"], row["error"], calls) == ("SKIPPED", "SHADOW_BUDGET_DEGRADED", [])
    assert pool.ledger_roles() == ["r"]  # no shadow call, no shadow ledger row


async def test_unknown_spend_skips_the_shadow():
    guard = BudgetGuard(spend_unknown=True)
    pool = FakePool()
    g = gw(pool=pool, budget=guard, priority=BudgetPriority.CRITICAL)
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model(SHADOW, TestModel()):
        await g.run("r", "x")
        await g.drain_shadows(5)
    assert pool.shadow_rows()[0]["error"] == "SHADOW_SPEND_UNKNOWN"


async def test_paid_shadow_needs_explicit_opt_in_and_counts_toward_the_budget():
    calls = []

    def paid(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("paid answer " * 50)])

    blocked_pool, guard = FakePool(), BudgetGuard(default_cap_usd=D("25"))
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model(PAID_SHADOW, FunctionModel(paid)):
        g = gw(shadow=PAID_SHADOW, pool=blocked_pool, budget=guard)
        await g.run("r", "x")
        await g.drain_shadows(5)
        assert (blocked_pool.shadow_rows()[0]["error"], calls) == ("PAID_SHADOW_NOT_ALLOWED", [])

        allowed_pool = FakePool()
        g = gw(shadow=PAID_SHADOW, pool=allowed_pool, budget=guard, allow_paid=True)
        await g.run("r", "x")
        await g.drain_shadows(5)
    [row] = allowed_pool.shadow_rows()
    assert row["status"] == "OK" and row["shadow_cost_usd"] > 0 and calls == [1]
    assert guard._by_role["shadow:r"] == row["shadow_cost_usd"]  # counted toward the daily budget
    assert "shadow:r" in allowed_pool.ledger_roles()


async def test_local_shadow_is_free_without_opt_in():
    pool = FakePool()
    with override_model(PRIMARY, TestModel(custom_output_text="p")), override_model("ollama:tiny", TestModel(custom_output_text="s")):
        g = gw(shadow="ollama:tiny", pool=pool)

        async def up(model=None):
            return False
        g.local_tier.is_down = up
        await g.run("r", "x")
        await g.drain_shadows(5)
    [row] = pool.shadow_rows()
    assert (row["status"], row["shadow_cost_usd"]) == ("OK", D("0"))


async def test_a_down_local_tier_skips_the_shadow():
    pool = FakePool()
    g = gw(shadow="ollama:tiny", pool=pool)

    async def down(model=None):
        return True
    g.local_tier.is_down = down
    with override_model(PRIMARY, TestModel(custom_output_text="p")):
        await g.run("r", "x")
        await g.drain_shadows(5)
    assert pool.shadow_rows()[0]["error"] == "LOCAL_TIER_DOWN"


# --- routes --------------------------------------------------------------------------------------

def test_route_rows_set_shadows_also_on_protected_roles():
    for role in PROTECTED_ROLES:
        r = route_from_row({"role": role, "shadow_model": "qwen-x", "shadow_sample_rate": D("0.25"),
                            "primary_model": "ollama:tiny"}, DEFAULT_ROUTES[role])
        assert (r.shadow_model, r.shadow_sample_rate) == ("google:qwen-x", 0.25)
        assert r.primary_model == DEFAULT_ROUTES[role].primary_model  # uncertified primary still ignored
    r = route_from_row({"role": "triage", "shadow_model": "ollama:x", "shadow_sample_rate": 7}, DEFAULT_ROUTES["triage"])
    assert r.shadow_sample_rate == 1.0
    assert DEFAULT_ROUTES["triage"].shadow_model is None and DEFAULT_ROUTES["triage"].shadow_sample_rate == 0


# --- agreement and the report ---------------------------------------------------------------------

def test_agreement_on_the_key_field():
    pm = PortfolioDecision(recommendation="BUY", conviction_score=0.7, allocation_pct=5, reasoning="x")
    assert sh.agreement("debate.verdict", pm, {"recommendation": "BUY", "conviction_score": 0.1}) == ("recommendation", True)
    assert sh.agreement("debate.verdict", pm, {"recommendation": "HOLD"}) == ("recommendation", False)
    assert sh.agreement("triage", {"worth_debate": True}, {"worth_debate": False}) == ("worth_debate", False)
    assert sh.agreement("debate.cro", {"conviction_level": "Buy"}, {"conviction_level": "buy"}) == ("conviction_level", True)
    assert sh.agreement("chat.explain", "text", "other text") == (None, None)
    assert sh.agreement("triage", {"worth_debate": True}, None) == ("worth_debate", None)
    assert sh.agreement("custom", {"recommendation": "SELL"}, {"recommendation": "SELL"}) == ("recommendation", True)


def _row(**kw):
    base = {"role": "triage", "primary_model": PRIMARY, "shadow_model": SHADOW, "status": "OK", "key_field": "worth_debate",
            "agrees": True, "error": None, "primary_latency_ms": 100, "shadow_latency_ms": 300,
            "primary_cost_usd": D("0"), "shadow_cost_usd": D("0"), "primary_key": "true", "shadow_key": "true",
            "final_recommendation": None, "return_pct": None}
    return {**base, **kw}


def test_report_counts_agreement_latency_and_errors():
    rows = [_row(shadow_latency_ms=v) for v in (100, 200, 300, 400)] + [
        _row(agrees=False, shadow_key="false"),
        _row(status="ERROR", agrees=None, error="boom", shadow_latency_ms=9000, shadow_cost_usd=D("0.01")),
        _row(status="SKIPPED", agrees=None, error="SHADOW_BUDGET_DEGRADED", shadow_latency_ms=None),
    ]
    [m] = report.report(rows)["models"]
    assert (m["n"], m["ok"], m["errors"], m["skipped"]) == (7, 5, 1, 1)
    assert m["skip_reasons"] == {"SHADOW_BUDGET_DEGRADED": 1}
    assert m["agreement"] == {"n": 5, "agree": 4, "rate": 0.8}
    assert m["latency_ms"]["shadow_p50"] == 300 and m["latency_ms"]["shadow_p95"] == 400  # OK rows only
    assert m["cost_usd"]["shadow"] == "0.010000"
    assert report.percentile([], 0.5) is None and report.percentile([5], 0.95) == 5


def test_triage_outcomes_once_labelled():
    rows = [
        _row(final_recommendation="BUY", return_pct=0.02),  # agreed, debated, won
        _row(agrees=False, shadow_key="false", final_recommendation="BUY", return_pct=-0.03),  # shadow would skip a loser
        _row(agrees=False, shadow_key="false", final_recommendation="Overweight", return_pct=0.01),  # ... and a winner
        _row(),  # not labelled yet
    ]
    out = report.report(rows)["models"][0]["outcomes"]
    assert out["labelled"] == 3 and out["agreement_rate"] == pytest.approx(1 / 3)
    assert out["debated"] == {"wins": 2, "losses": 1, "neutral": 0}
    assert out["shadow_would_skip"] == {"n": 2, "wins": 1, "losses": 1, "neutral": 0}


def test_verdict_outcomes_score_both_models_with_score_decision():
    rows = [_row(role="debate.verdict", key_field="recommendation", primary_key="BUY", shadow_key="HOLD", agrees=False,
                 return_pct=0.05),
            _row(role="debate.verdict", key_field="recommendation", primary_key="SELL", shadow_key="SELL", return_pct=0.001)]
    out = report.report(rows)["models"][0]["outcomes"]
    assert out["labelled"] == 2
    assert out["primary"] == {"wins": 1, "losses": 0, "neutral": 1}  # BUY +5% wins; SELL inside the band
    assert out["shadow"] == {"wins": 0, "losses": 1, "neutral": 1}  # HOLD missed the +5% rise


# --- database: rows, grants, endpoint, migration ---------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_shadowtest") as url:
        yield url


def as_role(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{parts.netloc.rsplit('@', 1)[-1]}"))


async def test_rows_land_in_ai_shadow_results_as_the_telemetry_role(db_url):
    admin = await asyncpg.connect(db_url)
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"), min_size=1, max_size=3)
    try:
        await admin.execute("INSERT INTO ai_model_routes (role, shadow_model, shadow_sample_rate) VALUES ('triage', 'test:shadow', 1)")
        g = Gateway(pool=pool, budget=BudgetGuard(pool=pool, refresh_s=0), shadow_rng=random.Random(3))
        parent = uuid.uuid4()
        with override_model(DEFAULT_ROUTES["triage"].primary_model, triage_model(True)), override_model(SHADOW, triage_model(False)):
            res = await g.run("triage", "brief", output_type=TriageDecision, parent_trace_id=parent)
            await g.drain_shadows(10)
        row = await admin.fetchrow("SELECT * FROM ai_shadow_results WHERE primary_trace_id = $1", res.trace_id)
        assert (row["role"], row["shadow_model"], row["status"], row["key_field"], row["agrees"]) == (
            "triage", SHADOW, "OK", "worth_debate", False)
        assert row["parent_trace_id"] == parent and row["primary_model"] == DEFAULT_ROUTES["triage"].primary_model
        assert json.loads(row["shadow_output"])["worth_debate"] is False
        ledger = await admin.fetchrow("SELECT role, status FROM llm_cost_ledger WHERE trace_id = $1", row["id"])
        assert tuple(ledger) == ("shadow:triage", "OK")
        # telemetry may only insert; the API may only read
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.fetch("SELECT * FROM ai_shadow_results")
        api = await asyncpg.connect(as_role(db_url, "cryptonite_api", "api_dev_pass"))
        try:
            assert await api.fetchval("SELECT count(*) FROM ai_shadow_results") >= 1
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await api.execute("DELETE FROM ai_shadow_results")
        finally:
            await api.close()
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute("UPDATE ai_model_routes SET shadow_sample_rate = 1.5 WHERE role = 'triage'")
    finally:
        await admin.execute("DELETE FROM ai_model_routes")
        await admin.close()
        await pool.close()


async def test_shadow_endpoint(db_url, operator_auth):
    from cryptonite_api.main import app
    from httpx import ASGITransport, AsyncClient

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    app.state.pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM ai_shadow_results")
            await conn.execute("INSERT INTO ai_model_routes (role, shadow_model, shadow_sample_rate) VALUES "
                               "('triage', 'ollama:qwen3.5:4b', 0.25), ('debate.verdict', 'google:gemini-2.5-flash', 0.1)")
            trace = uuid.uuid4()
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status, decision_trace_id) "
                                      "VALUES ('ETHUSDT.BINANCE', 'scanner', 'COMPLETED', $1) RETURNING id", trace)
            dec = await conn.fetchval(
                "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
                "reasoning, decision_price) VALUES ($1, 'ETHUSDT.BINANCE', 'BUY', 0.7, 5, 'r', 3000) RETURNING id", run)
            await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                               "VALUES ($1, '24h', -0.03, 0.01, -0.04, 1440)", dec)
            old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)
            for i, (role, status, agrees, p, s, lat, parent, created) in enumerate([
                ("triage", "OK", False, True, False, 200, trace, None),
                ("triage", "OK", True, True, True, 400, None, None),
                ("triage", "ERROR", None, True, None, None, None, None),
                ("debate.verdict", "SKIPPED", None, None, None, None, None, None),
                ("triage", "OK", True, True, True, 100, None, old),  # outside the window
            ]):
                key = "worth_debate" if role == "triage" else "recommendation"
                await conn.execute(
                    """INSERT INTO ai_shadow_results (id, created_at, role, primary_model, shadow_model, status, parent_trace_id,
                           primary_output, shadow_output, key_field, agrees, primary_latency_ms, shadow_latency_ms, error)
                       VALUES ($1, COALESCE($2, now()), $3, 'ollama:gemma4:12b-it-qat', $4, $5, $6, $7::jsonb, $8::jsonb,
                               $9, $10, 150, $11, $12)""",
                    uuid.uuid4(), created, role, "ollama:qwen3.5:4b" if role == "triage" else "google:gemini-2.5-flash",
                    status, parent, json.dumps({key: p}), json.dumps({key: s} if s is not None else None), key, agrees,
                    lat, "boom" if status == "ERROR" else ("PAID_SHADOW_NOT_ALLOWED" if status == "SKIPPED" else None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = (await client.get("/api/v1/ai/shadow")).json()
            only = (await client.get("/api/v1/ai/shadow", params={"role": "debate.verdict", "days": 7})).json()
            bad = await client.get("/api/v1/ai/shadow", params={"days": 0})
        assert body["available"] is True and body["days"] == 7 and body["allow_paid"] is False
        routes = {r["role"]: r for r in body["routes"]}
        assert routes["triage"]["active"] is True and routes["triage"]["shadow_sample_rate"] == 0.25
        assert routes["debate.verdict"]["shadow_paid"] is True and routes["debate.verdict"]["active"] is False
        assert routes["debate.verdict"]["primary_model"] == DEFAULT_ROUTES["debate.verdict"].primary_model
        tri = next(m for m in body["models"] if m["role"] == "triage")
        assert (tri["n"], tri["ok"], tri["errors"]) == (3, 2, 1)
        assert tri["agreement"] == {"n": 2, "agree": 1, "rate": 0.5}
        assert tri["latency_ms"]["shadow_p50"] == 200 and tri["latency_ms"]["shadow_p95"] == 400
        assert tri["outcomes"]["labelled"] == 1
        assert tri["outcomes"]["shadow_would_skip"] == {"n": 1, "wins": 0, "losses": 1, "neutral": 0}
        assert {r["role"] for r in body["roles"]} == {"triage", "debate.verdict"}
        assert [m["role"] for m in only["models"]] == ["debate.verdict"]
        assert only["models"][0]["skip_reasons"] == {"PAID_SHADOW_NOT_ALLOWED": 1}
        assert bad.status_code == 422
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM ai_model_routes")
            await conn.execute("TRUNCATE ai_shadow_results, ai_decision_outcomes, portfolio_decisions, debate_runs CASCADE")
        await pool.close()


def _alembic(url: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)


async def _schema(url: str) -> tuple[bool, bool]:
    conn = await asyncpg.connect(url)
    try:
        cols = await conn.fetchval("""SELECT count(*) FROM information_schema.columns WHERE table_name = 'ai_model_routes'
                                      AND column_name IN ('shadow_model', 'shadow_sample_rate')""")
        table = await conn.fetchval("SELECT to_regclass('ai_shadow_results') IS NOT NULL")
        return cols == 2, table
    finally:
        await conn.close()


def test_migration_036_up_and_down():
    with migrated_database("cryptonite_m036") as url:
        assert asyncio.run(_schema(url)) == (True, True)
        _alembic(url, "downgrade", "035")
        assert asyncio.run(_schema(url)) == (False, False)
        _alembic(url, "upgrade", "head")
        assert asyncio.run(_schema(url)) == (True, True)
