"""AI Gateway (blueprint A1.2, A1.3, A1.5): routing, fallback policy, budget guard, permission gate,
pricing and ledger writes. Models are pydantic-ai test doubles; no network, no API key."""

import datetime
import uuid
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptonite_agents.gateway import (
    BudgetExceeded,
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    InvalidModelOutput,
    ModelUnavailable,
    RiskClass,
    RoleRoute,
)
from cryptonite_agents.gateway.ledger import CallRecord, write_call
from cryptonite_agents.gateway.pricing import cost_usd
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.registry import ToolSpec
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES, route_from_row
from migrated_db import migrated_database
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

D = Decimal
PRIMARY, FALLBACK = "test:primary", "test:fallback"


class Verdict(BaseModel):
    recommendation: str = Field(pattern="^(BUY|HOLD|SELL)$")
    conviction_score: float = Field(ge=0, le=1)


def failing_model() -> FunctionModel:
    def fn(messages, info: AgentInfo):
        raise ModelHTTPError(503, "primary", "overloaded")
    return FunctionModel(fn)


def gateway(policy=FallbackPolicy.FAIL_CLOSED, priority=BudgetPriority.DEBATE, budget=None) -> Gateway:
    route = RoleRoute("r", PRIMARY, FALLBACK, policy, priority)
    return Gateway(routes={"r": route}, budget=budget or BudgetGuard())


# --- Routing and fallback policy ---------------------------------------------------------

async def test_run_returns_structured_output_and_cost():
    with override_model(PRIMARY, TestModel(custom_output_args={"recommendation": "HOLD", "conviction_score": 0.4})):
        res = await gateway().run("r", "decide", output_type=Verdict)
    assert isinstance(res.output, Verdict)
    assert (res.model, res.fell_back, res.cost_usd) == (PRIMARY, False, D("0"))  # test models are free


async def test_fail_closed_role_never_falls_back_to_a_weaker_model():
    with override_model(PRIMARY, failing_model()), override_model(FALLBACK, TestModel()):
        with pytest.raises(ModelUnavailable):
            await gateway(FallbackPolicy.FAIL_CLOSED).run("r", "decide")


async def test_degrade_role_uses_the_fallback():
    with override_model(PRIMARY, failing_model()), override_model(FALLBACK, TestModel(custom_output_text="ok")):
        res = await gateway(FallbackPolicy.DEGRADE).run("r", "summarize")
    assert (res.output, res.model, res.fell_back) == ("ok", FALLBACK, True)


async def test_invalid_structured_output_fails_closed():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("not json at all")])
    with override_model(PRIMARY, FunctionModel(fn)):
        with pytest.raises(InvalidModelOutput):
            await gateway().run("r", "decide", output_type=Verdict)


async def test_unknown_role_is_an_error():
    with pytest.raises(Exception, match="unknown role"):
        await gateway().run("nope", "x")


def test_route_rows_override_defaults_and_normalize_model_ids():
    base = DEFAULT_ROUTES["triage"]
    r = route_from_row({"role": "triage", "primary_model": "gemini-2.5-flash", "thinking_budget": 512}, base)
    assert (r.primary_model, r.thinking_budget, r.priority) == ("google:gemini-2.5-flash", 512, base.priority)


def test_trade_decision_roles_fail_closed_and_exits_are_critical():
    for role in ("debate.verdict", "debate.cro", "exit.verdict"):
        assert DEFAULT_ROUTES[role].fallback_policy is FallbackPolicy.FAIL_CLOSED
    assert DEFAULT_ROUTES["exit.verdict"].priority is BudgetPriority.CRITICAL
    assert DEFAULT_ROUTES["exit.side"].priority is BudgetPriority.CRITICAL


# --- Budget guard (A1.3) -------------------------------------------------------------------

async def spent(guard: BudgetGuard, usd: str) -> BudgetGuard:
    await guard.record("x", D(usd))
    return guard


@pytest.mark.parametrize("usd, blocked", [
    ("17.0", set()),
    ("20.0", {"optional"}),
    ("22.5", {"optional", "interactive"}),
    ("25.0", {"optional", "interactive", "debate"}),
])
async def test_budget_stops_roles_in_priority_order_and_never_stops_exits(usd, blocked):
    guard = await spent(BudgetGuard(default_cap_usd=D("25")), usd)
    for priority in BudgetPriority:
        route = RoleRoute("r", PRIMARY, priority=priority)
        if priority.value in blocked:
            with pytest.raises(BudgetExceeded):
                await guard.check(route)
        else:
            await guard.check(route)  # critical always passes


async def test_budget_blocked_call_never_reaches_the_model():
    guard = await spent(BudgetGuard(default_cap_usd=D("25")), "30")
    with override_model(PRIMARY, failing_model()):  # would raise ModelUnavailable if called
        with pytest.raises(BudgetExceeded):
            await gateway(budget=guard).run("r", "x")


async def test_role_daily_cap():
    guard = BudgetGuard(default_cap_usd=D("25"))
    await guard.record("r", D("1.0"))
    with pytest.raises(BudgetExceeded, match="ROLE_DAILY_CAP"):
        await guard.check(RoleRoute("r", PRIMARY, daily_cap_usd=D("1.0")))


async def test_budget_alerts_once_at_70_percent():
    class Redis:
        published = []
        keys = set()

        async def set(self, key, value, nx=False, ex=None):
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

        async def publish(self, channel, msg):
            self.published.append((channel, msg))

    redis = Redis()
    guard = BudgetGuard(redis=redis, default_cap_usd=D("10"))
    for _ in range(3):
        await guard.record("x", D("3"))
    assert len(redis.published) == 1 and redis.published[0][0] == "channel:system_alerts"


async def test_budget_resets_at_utc_midnight():
    now = [datetime.datetime(2026, 9, 25, 23, 59, tzinfo=datetime.UTC).timestamp()]
    guard = BudgetGuard(default_cap_usd=D("10"), clock=lambda: now[0])
    await guard.record("x", D("10"))
    with pytest.raises(BudgetExceeded):
        await guard.check(RoleRoute("r", PRIMARY, priority=BudgetPriority.DEBATE))
    now[0] += 120
    await guard.check(RoleRoute("r", PRIMARY, priority=BudgetPriority.DEBATE))


# --- Permission gate (A1.5) ----------------------------------------------------------------

async def test_add_risk_tool_is_denied_without_approval_and_read_tools_run():
    ran = []

    def read_prices(symbol: str) -> str:
        """Read prices."""
        ran.append("read")
        return "100"

    def place_order(symbol: str, qty: float) -> str:
        """Place an order."""
        ran.append("order")
        return "placed"

    tools = [ToolSpec("read_prices", read_prices, RiskClass.READ, "Read prices."),
             ToolSpec("place_order", place_order, RiskClass.ADD_RISK, "Place an order.")]
    with override_model(PRIMARY, TestModel(call_tools="all")):
        res = await gateway().run("r", "go", tools=tools)

    assert ran == ["read"]
    by_tool = {c["tool"]: c["status"] for c in res.tool_calls}
    assert by_tool == {"read_prices": "OK", "place_order": "DENIED"}


async def test_a_turn_can_stage_at_most_one_paid_debate():
    """LE-8: stage_debate_proposal queues a paid debate; one model turn may call it once."""
    staged = []

    def stage_debate_proposal(instrument_id: str) -> str:
        """Stage a debate."""
        staged.append(instrument_id)
        return "staged"

    def two_calls(messages, info: AgentInfo):
        if any(getattr(p, "part_kind", "") == "tool-return" for m in messages for p in getattr(m, "parts", [])):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("stage_debate_proposal", {"instrument_id": "BTCUSDT.BINANCE"}),
                                    ToolCallPart("stage_debate_proposal", {"instrument_id": "ETHUSDT.BINANCE"})])

    spec = ToolSpec("stage_debate_proposal", stage_debate_proposal, RiskClass.READ, "Stage a debate.", side_effects=True)
    with override_model(PRIMARY, FunctionModel(two_calls)):
        res = await gateway().run("r", "go", tools=[spec])
        again = await gateway().run("r", "go", tools=[spec])  # a new turn gets a fresh allowance
    assert staged == ["BTCUSDT.BINANCE", "BTCUSDT.BINANCE"]  # each turn: the first call runs, the second is denied
    assert [c["status"] for c in res.tool_calls] == ["OK", "DENIED"]
    assert [c["status"] for c in again.tool_calls] == ["OK", "DENIED"]


# --- Pricing ---------------------------------------------------------------------------------

def test_pricing_matches_the_gemini_price_list():
    # Live probe 2026-09-25: 13 input + 454 output (incl. 415 thinking) on 2.5 Flash cost $0.0011389
    assert cost_usd("google:gemini-2.5-flash", 13, 454) == D("0.001139")
    assert cost_usd("google:gemini-2.5-pro", 200_000, 100_000) == D("1.25")  # 0.25 in + 1.00 out
    assert cost_usd("google:gemini-2.5-pro", 300_000, 0) == D("0.75")  # >200k prompt: $2.50/M
    assert cost_usd("google:gemini-2.5-flash", 1_000_000, 0, cached_input_tokens=1_000_000) == D("0.03")
    assert cost_usd("ollama:gemma4:12b", 10**6, 10**6) == 0
    assert cost_usd("google:some-future-model", 100_000, 0) == D("0.20")  # unknown → highest known rate


# --- Ledger + budget against a real database -------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_gatewaytest") as url:
        yield url


def as_role(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{parts.netloc.rsplit('@', 1)[-1]}"))


async def test_telemetry_role_writes_ledger_and_trace_and_budget_reads_them(db_url):
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"), min_size=1, max_size=2)
    try:
        rec = CallRecord("debate.verdict", "google:gemini-2.5-pro", "OK", 1000, 500, 0, 300, D("0.00625"), 1200,
                         tool_calls=[{"tool": "x"}], output={"recommendation": "HOLD"})
        assert await write_call(pool, rec) is True
        row = await pool.fetchrow("SELECT * FROM llm_cost_ledger WHERE trace_id = $1", rec.trace_id)
        assert (row["role"], row["provider"], row["thinking_tokens"], row["total_cost_usd"]) == (
            "debate.verdict", "google", 300, D("0.006250"))

        guard = BudgetGuard(pool=pool, refresh_s=0)
        snap = await guard.snapshot()
        assert snap["cap_usd"] == D("25.00") and snap["spent_usd"] >= D("0.00625")

        # Telemetry can add spend, never remove it, and can neither read traces nor touch trading data
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("INSERT INTO llm_cost_ledger (request_id, agent_name, model_name, total_cost_usd) "
                               "VALUES ('neg', 'x', 'x', -1000)")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.fetch("SELECT * FROM ai_decision_traces")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute("DELETE FROM paper_orders")
    finally:
        await pool.close()


async def test_agent_role_cannot_write_telemetry_or_routes(db_url):
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_agent_ro", "agent_ro_dev_pass"), min_size=1, max_size=2)
    try:
        for sql in ("INSERT INTO llm_cost_ledger (request_id, agent_name, model_name, total_cost_usd) VALUES ('p','p','p',0)",
                    "INSERT INTO ai_model_routes (role, primary_model) VALUES ('debate.verdict', 'ollama:tiny')"):
            with pytest.raises((asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError)):
                await pool.execute(sql)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.fetch("SELECT * FROM ai_decision_traces")
        assert await pool.fetchval("SHOW default_transaction_read_only") == "on"
    finally:
        await pool.close()


async def test_sql_tool_is_read_only_single_statement(db_url, monkeypatch):
    from cryptonite_agents import tools

    monkeypatch.setattr(tools, "DB_URL", as_role(db_url, "cryptonite_agent_ro", "agent_ro_dev_pass"))
    monkeypatch.setattr(tools, "_engine", None)
    try:
        assert tools.query_postgres_readonly("SELECT 1 AS one") == '[{"one": 1}]'
        assert "exactly one" in tools.query_postgres_readonly("SELECT 1; SELECT 2")
        attack = "INSERT INTO llm_cost_ledger (request_id, agent_name, model_name, total_cost_usd) VALUES ('p','p','p',-1000); COMMIT;"
        assert "exactly one" in tools.query_postgres_readonly(attack)
        assert "Error" in tools.query_postgres_readonly(attack.split(";")[0])
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=1)
        try:
            assert await pool.fetchval("SELECT count(*) FROM llm_cost_ledger WHERE request_id = 'p'") == 0
        finally:
            await pool.close()
    finally:
        if tools._engine is not None:
            tools._engine.dispose()
        tools._engine = None


async def test_route_overrides_load_from_the_database(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await pool.execute("INSERT INTO ai_model_routes (role, primary_model) VALUES ('triage', 'gemini-2.5-flash')")
        gw = Gateway(pool=pool)
        assert (await gw.route("triage")).primary_model == "google:gemini-2.5-flash"
        assert (await gw.route("debate.verdict")).primary_model == "google:gemini-2.5-pro"
    finally:
        await pool.execute("DELETE FROM ai_model_routes")
        await pool.close()


async def test_ledger_outage_never_fails_the_call():
    class BrokenPool:
        def acquire(self):
            raise RuntimeError("db down")

    await write_call(BrokenPool(), CallRecord("r", PRIMARY, "OK", trace_id=uuid.uuid4()))


# --- Architecture: one provider import site (blueprint D3) -------------------------------------

def test_only_the_backend_imports_model_sdks():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    backend = root / "packages/agents/cryptonite_agents/gateway/providers/pydantic_ai_backend.py"
    pattern = re.compile(r"^\s*(from|import)\s+(pydantic_ai|google\.genai|google\.antigravity|google import genai)", re.M)
    offenders = [
        str(p.relative_to(root))
        for base in ("packages", "services")
        for p in (root / base).rglob("*.py")
        if p != backend and "__pycache__" not in p.parts and pattern.search(p.read_text(errors="ignore"))
    ]
    assert offenders == []


async def test_an_explicit_model_override_is_pinned_even_for_degrade_roles():
    with override_model(PRIMARY, failing_model()), override_model("test:pinned", failing_model()), \
         override_model(FALLBACK, TestModel(custom_output_text="fallback")):
        with pytest.raises(ModelUnavailable):
            await gateway(FallbackPolicy.DEGRADE).run("r", "x", model_override="test:pinned")



# --- Review regressions: protected routes, failed-call cost, unknown spend, cap parsing ---------

def test_db_rows_cannot_weaken_trade_decision_routes():
    base = DEFAULT_ROUTES["debate.verdict"]
    weakened = route_from_row({"role": "debate.verdict", "primary_model": "ollama:tiny", "fallback_model": "ollama:tiny",
                               "fallback_policy": "DEGRADE", "priority": "optional"}, base)
    assert weakened == base
    certified = route_from_row({"role": "debate.verdict", "primary_model": "gemini-3.1-pro-preview",
                                "certified_primary": True}, base)
    assert certified.primary_model == "google:gemini-3.1-pro-preview"
    assert certified.fallback_policy is FallbackPolicy.FAIL_CLOSED


async def test_failed_calls_are_costed_against_the_budget():
    def fn(messages, info: AgentInfo):
        return ModelResponse(parts=[TextPart("not json")])

    guard = BudgetGuard(default_cap_usd=D("25"))
    gw = Gateway(routes={"r": RoleRoute("r", "google:gemini-2.5-flash")}, budget=guard)
    with override_model("google:gemini-2.5-flash", FunctionModel(fn)):
        with pytest.raises(InvalidModelOutput):
            await gw.run("r", "decide", output_type=Verdict)
    assert (await guard.snapshot())["spent_usd"] > 0  # three attempts' tokens billed at Flash rates


async def test_tool_exceptions_become_gateway_errors():
    def boom(x: str) -> str:
        """Always fails."""
        raise RuntimeError("tool exploded")

    with override_model(PRIMARY, TestModel(call_tools="all")):
        with pytest.raises(ModelUnavailable):
            await gateway().run("r", "go", tools=[ToolSpec("boom", boom, RiskClass.READ, "Always fails.")])


async def test_unknown_spend_blocks_non_core_roles_until_the_ledger_is_read():
    class DownPool:
        def acquire(self):
            raise RuntimeError("db down")

    guard = BudgetGuard(pool=DownPool(), default_cap_usd=D("25"), refresh_s=0)
    for priority, allowed in [(BudgetPriority.OPTIONAL, False), (BudgetPriority.INTERACTIVE, False),
                              (BudgetPriority.DEBATE, True), (BudgetPriority.CRITICAL, True)]:
        route = RoleRoute("r", PRIMARY, priority=priority)
        if allowed:
            await guard.check(route)
        else:
            with pytest.raises(BudgetExceeded, match="SPEND_UNKNOWN"):
                await guard.check(route)


async def test_unpersisted_spend_survives_ledger_refreshes():
    class EmptyLedgerPool:
        def acquire(self):
            class Conn:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def fetch(self, *a):
                    return []

                async def fetchval(self, *a):
                    return "25.00"

            return Conn()

    now = [1_000_000.0]
    guard = BudgetGuard(pool=EmptyLedgerPool(), refresh_s=5, clock=lambda: now[0])
    await guard.record("r", D("24.9"), persisted=False)  # its ledger write failed
    now[0] += 60  # several refreshes later the ledger is still empty
    with pytest.raises(BudgetExceeded):
        await guard.check(RoleRoute("r", PRIMARY, priority=BudgetPriority.INTERACTIVE))


def test_budget_cap_parses_json_strings():
    from cryptonite_agents.gateway.budget import parse_cap

    assert parse_cap('"30.5"', D("25")) == D("30.5")
    assert parse_cap("40", D("25")) == D("40")
    assert parse_cap("garbage", D("25")) == D("25")
    assert parse_cap(None, D("25")) == D("25")


async def test_missing_api_key_is_a_gateway_error(monkeypatch):
    gw = Gateway(routes={"r": RoleRoute("r", "google:gemini-2.5-flash")})
    with pytest.raises(ModelUnavailable):  # GEMINI_API_KEY is unset in tests (conftest)
        await gw.run("r", "x")


async def test_missing_ledger_treats_spend_as_unknown():
    guard = BudgetGuard(spend_unknown=True)
    with pytest.raises(BudgetExceeded, match="SPEND_UNKNOWN"):
        await guard.check(RoleRoute("r", PRIMARY, priority=BudgetPriority.INTERACTIVE))
    await guard.check(RoleRoute("r", PRIMARY, priority=BudgetPriority.CRITICAL))


async def test_distillable_roles_keep_their_prompt_in_the_trace(db_url):
    """A7.4: triage/tagging/summarize traces keep the prompt (for distillation); other roles never do."""
    telemetry = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"),
                                          min_size=1, max_size=2)
    owner = await asyncpg.connect(db_url)
    try:
        routes = {r: RoleRoute(r, PRIMARY) for r in ("triage", "debate.side")}
        gw = Gateway(pool=telemetry, routes=routes, budget=BudgetGuard())
        with override_model(PRIMARY, TestModel(custom_output_text="ok")):
            t = await gw.run("triage", "MARKET BRIEF BTCUSDT.BINANCE ...")
            d = await gw.run("debate.side", "argue the bull case")
        rows = {r["role"]: r["input"] for r in await owner.fetch(
            "SELECT role, input FROM ai_decision_traces WHERE id = ANY($1::uuid[])", [t.trace_id, d.trace_id])}
        assert rows == {"triage": "MARKET BRIEF BTCUSDT.BINANCE ...", "debate.side": None}
    finally:
        await owner.close()
        await telemetry.close()
