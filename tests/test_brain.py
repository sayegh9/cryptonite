"""The Brain (blueprint A3.1–A3.3, A3.5): gateway streaming, permission gate on propose_order,
sessions + memory compaction, the SSE endpoint, and the migration 024 grants.

Models are pydantic-ai test doubles (FunctionModel / TestModel); no network, no API key.
"""

import ast
import asyncio
import contextlib
import inspect
import json
import sys
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptonite_agents.brain import (
    BRAIN,
    BRAIN_PLAN,
    BRAIN_ROUTES,
    BRAIN_TOOLS,
    Brain,
    MemorySessionStore,
    PgSessionStore,
    brain_gateway_routes,
    memory,
)
from cryptonite_agents.brain.store import session_cursor
from cryptonite_agents.brain.toolset import propose_order, show_price_chart
from cryptonite_agents.brain.widgets import describe_widget, widget_for_tool
from cryptonite_agents.evals import runner as eval_runner
from cryptonite_agents.evals.runner import load_set
from cryptonite_agents.gateway import (
    BudgetExceeded,
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    ModelUnavailable,
    RiskClass,
    RoleRoute,
)
from cryptonite_agents.gateway import gateway as gateway_module
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.tools import LIVE_MARKET_DATA
from migrated_db import migrated_database
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

AGENT_WORKER_DIR = str(Path(__file__).parent.parent / "services" / "agent_worker")
if AGENT_WORKER_DIR not in sys.path:
    sys.path.insert(0, AGENT_WORKER_DIR)

PLANNER, SUMMARIZER, FALLBACK = "test:brain", "test:summarize", "test:fallback"

GOOD_ORDER = {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "allocation_pct": 5,
              "order_type": "MARKET", "stop_loss": 58000, "rationale": "4H reclaim; invalid below 58k"}


def routes(policy=FallbackPolicy.FAIL_CLOSED) -> dict[str, RoleRoute]:
    return {
        BRAIN_PLAN: RoleRoute(BRAIN_PLAN, PLANNER, FALLBACK, policy, BudgetPriority.INTERACTIVE),
        "summarize": RoleRoute("summarize", SUMMARIZER, priority=BudgetPriority.OPTIONAL),
    }


def gw(**kw) -> Gateway:
    return Gateway(routes=kw.pop("routes", routes()), budget=kw.pop("budget", BudgetGuard()), **kw)


def _tool_returns(messages) -> list[ToolReturnPart]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


def _prompt_of(messages) -> str:
    first = messages[0]
    return "".join(str(getattr(p, "content", "")) for p in first.parts)


class Script:
    """A streaming FunctionModel: first response calls tools, second answers in text chunks."""

    def __init__(self, tool_calls: list[tuple[str, dict]], answer: list[str], delay: float = 0.0) -> None:
        self.tool_calls = tool_calls
        self.answer = answer
        self.delay = delay
        self.prompts: list[str] = []
        self.returns: list[ToolReturnPart] = []

    def model(self) -> FunctionModel:
        async def stream(messages, info: AgentInfo):
            if not _tool_returns(messages) and self.tool_calls:
                self.prompts.append(_prompt_of(messages))
                for i, (name, args) in enumerate(self.tool_calls):
                    yield {i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call-{i}")}
                return
            if not self.tool_calls:
                self.prompts.append(_prompt_of(messages))
            self.returns = _tool_returns(messages)
            for chunk in self.answer:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield chunk

        def sync(messages, info):  # non-streamed runs (evals, summaries)
            if not _tool_returns(messages) and self.tool_calls:
                return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"call-{i}")
                                            for i, (name, args) in enumerate(self.tool_calls)])
            return ModelResponse(parts=[TextPart("".join(self.answer))])

        return FunctionModel(sync, stream_function=stream)


async def collect(agen) -> list:
    return [ev async for ev in agen]


# --- Routes ------------------------------------------------------------------------------------

def test_brain_routes_extend_the_defaults_without_editing_them():
    merged = brain_gateway_routes()
    assert set(BRAIN_ROUTES) == {"brain.plan", "brain.chat"}
    assert merged["brain.plan"].fallback_policy is FallbackPolicy.FAIL_CLOSED
    assert merged["brain.plan"].priority is BudgetPriority.INTERACTIVE
    assert "debate.verdict" in merged and "summarize" in merged


def test_propose_order_is_the_only_risk_adding_brain_tool():
    risks = {t.name: t.risk for t in BRAIN_TOOLS}
    assert risks["propose_order"] is RiskClass.ADD_RISK
    assert [n for n, r in risks.items() if r is not RiskClass.READ] == ["propose_order"]


def test_propose_order_only_returns_a_proposal():
    # Even an approved call cannot place anything: the function just echoes the validated card
    out = json.loads(propose_order.func(**GOOD_ORDER))
    assert out["status"] == "PENDING_APPROVAL" and out["proposal"]["side"] == "BUY"
    tree = ast.parse(Path(sys.modules[propose_order.func.__module__].__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in imported if m.split(".")[0] in ("cryptonite_execution", "httpx", "urllib", "requests")}
    body = inspect.getsource(propose_order.func)
    assert "execution_commands" not in body and "http" not in body.split('"""')[-1]


def test_propose_order_arguments_are_validated_before_the_gate():
    # Out-of-band values make pydantic-ai ask the model to retry instead of "holding" a card that
    # could never render
    from cryptonite_agents.gateway.providers.pydantic_ai_backend import _as_tool

    schema = _as_tool(propose_order, lambda spec: None, []).function_schema.json_schema["properties"]
    assert schema["allocation_pct"]["minimum"] == 1 and schema["allocation_pct"]["maximum"] == 25
    assert schema["side"]["enum"] == ["BUY", "SELL"] and schema["order_type"]["enum"] == ["MARKET", "LIMIT"]


# --- Gateway.stream ----------------------------------------------------------------------------

async def test_stream_yields_tokens_then_done_with_the_start_trace_id():
    script = Script([], ["BTC is ", "range-bound."])
    with override_model(PLANNER, script.model()):
        events = await collect(gw().stream(BRAIN_PLAN, "analyze BTC"))
    kinds = [e.kind for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "".join(e.text for e in events if e.kind == "token") == "BTC is range-bound."
    assert str(events[-1].result.trace_id) == events[0].data["trace_id"]
    assert events[-1].result.output == "BTC is range-bound."


async def test_stream_passes_the_permission_gate_and_reports_tool_status():
    script = Script([("show_price_chart", {"symbol": "BTCUSDT", "timeframe": "4h"}),
                     ("propose_order", GOOD_ORDER)], ["Card staged for your approval."])
    with override_model(PLANNER, script.model()):
        events = await collect(gw().stream(BRAIN_PLAN, "go", tools=(show_price_chart, propose_order)))
    results = {e.data["tool"]: e.data["status"] for e in events if e.kind == "tool_result"}
    assert results == {"show_price_chart": "OK", "propose_order": "DENIED"}
    denied = [r for r in script.returns if r.tool_name == "propose_order"][0]
    assert "ADD_RISK_REQUIRES_APPROVAL" in str(denied.content)
    assert {c["tool"]: c["status"] for c in events[-1].result.tool_calls} == results


async def test_stream_budget_block_never_reaches_the_model():
    guard = BudgetGuard(default_cap_usd=Decimal("0"))
    with override_model(PLANNER, Script([], ["x"]).model()):
        with pytest.raises(BudgetExceeded):
            await collect(gw(budget=guard).stream(BRAIN_PLAN, "hi"))


async def test_stream_writes_ledger_and_trace_record(monkeypatch):
    written = []

    async def fake_write(pool, rec):
        written.append(rec)
        return True

    monkeypatch.setattr(gateway_module, "write_call", fake_write)
    with override_model(PLANNER, Script([("propose_order", GOOD_ORDER)], ["ok"]).model()):
        events = await collect(gw().stream(BRAIN_PLAN, "go", tools=(propose_order,)))
    assert len(written) == 1
    rec = written[0]
    assert (rec.status, str(rec.trace_id)) == ("OK", events[0].data["trace_id"])
    assert rec.tool_calls[0]["tool"] == "propose_order" and rec.tool_calls[0]["status"] == "DENIED"


async def test_closing_the_stream_cancels_the_model_and_records_a_cancelled_call(monkeypatch):
    written = []

    async def fake_write(pool, rec):
        written.append(rec)
        return True

    monkeypatch.setattr(gateway_module, "write_call", fake_write)
    script = Script([], ["first "] + ["more "] * 50, delay=0.05)
    with override_model(PLANNER, script.model()):
        stream = gw().stream(BRAIN_PLAN, "long answer")
        seen = []
        async for ev in stream:
            seen.append(ev.kind)
            if ev.kind == "token":
                break
        await stream.aclose()
    assert seen == ["start", "token"]
    assert [r.status for r in written] == ["CANCELLED"]
    await asyncio.sleep(0.2)
    assert not [t for t in asyncio.all_tasks() if "run_model_stream" in repr(t.get_coro())]


async def test_stream_fails_closed_for_brain_plan():
    def boom(messages, info):
        raise RuntimeError("provider down")

    async def boom_stream(messages, info):
        raise RuntimeError("provider down")
        yield ""  # pragma: no cover

    with override_model(PLANNER, FunctionModel(boom, stream_function=boom_stream)), \
            override_model(FALLBACK, Script([], ["weaker"]).model()):
        with pytest.raises(ModelUnavailable):
            await collect(gw().stream(BRAIN_PLAN, "hi"))
        events = await collect(gw(routes=routes(FallbackPolicy.DEGRADE)).stream(BRAIN_PLAN, "hi"))
    assert events[-1].result.fell_back is True


# --- Widgets ------------------------------------------------------------------------------------

def test_order_card_widget_enforces_the_allocation_band_and_limit_price():
    card = widget_for_tool("propose_order", GOOD_ORDER, call_id="c1", status="DENIED", trace_id="t1")
    assert card["kind"] == "order_card" and card["risk_class"] == "ADD_RISK" and card["trace_id"] == "t1"
    assert widget_for_tool("propose_order", {**GOOD_ORDER, "allocation_pct": 30}, call_id="c2") is None
    assert widget_for_tool("propose_order", {**GOOD_ORDER, "allocation_pct": 0.5}, call_id="c3") is None
    assert widget_for_tool("propose_order", {**GOOD_ORDER, "order_type": "LIMIT"}, call_id="c4") is None
    assert widget_for_tool("propose_order", {**GOOD_ORDER, "instrument_id": "ethusdt"},
                           call_id="c5")["instrument_id"] == "ETHUSDT.BINANCE"
    # sessions saved before the quantity was dropped still render; the size is always the allocation
    old = widget_for_tool("propose_order", {**GOOD_ORDER, "quantity": 0.06}, call_id="c6", status="DENIED")
    assert old["allocation_pct"] == 5 and old["quantity"] == 0.06
    assert "% of the mandate" in describe_widget(old) and "0.06" not in describe_widget(old)


def test_tearsheet_and_debate_widgets():
    ts = widget_for_tool("explain_backtest", {"backtest_id": "bt-1"}, call_id="c",
                         content=json.dumps({"total_return_pct": 12.5, "max_drawdown_pct": 4.2}))
    assert ts["kind"] == "tearsheet" and ts["metrics"]["total_return_pct"] == 12.5
    deb = widget_for_tool("stage_debate_proposal", {"instrument_id": "BTCUSDT.BINANCE", "timeframe": "1h",
                                                    "recommendation": "BUY", "conviction_score": 0.7,
                                                    "reasoning": "x"},
                          call_id="d", content="Success: Proposal staged to Action Inbox (Msg ID: 1-0)")
    assert deb["kind"] == "debate_summary" and deb["status"] == "QUEUED"


# --- The Brain: one conversation ------------------------------------------------------------------

def brain(store=None, holdings=None, **kw) -> Brain:
    persona = replace(BRAIN, tools=(show_price_chart, propose_order))  # no network/DB tools in unit tests
    return Brain(gw(**kw), store or MemorySessionStore(), persona, holdings=holdings)


async def test_analyze_propose_approve_in_one_conversation():
    store = MemorySessionStore()
    turn1 = Script([("show_price_chart", {"symbol": "BTCUSDT", "timeframe": "4h"}), ("propose_order", GOOD_ORDER)],
                   ["BTC reclaimed 60k on 4H. ", "Order card staged; approve it if you agree."])
    with override_model(PLANNER, turn1.model()):
        events = await collect(brain(store).turn("op-1", "Analyze BTC and propose a trade"))

    types = [e.type for e in events]
    assert types[0] == "session" and types[-1] == "done"
    sid, trace_id = events[0].data["session_id"], events[0].data["trace_id"]
    widgets = [e.data["widget"] for e in events if e.type == "widget"]
    assert sorted(w["kind"] for w in widgets) == ["order_card", "price_chart"]  # tools run concurrently
    card = next(w for w in widgets if w["kind"] == "order_card")
    assert card["status"] == "PENDING_APPROVAL" and card["trace_id"] == trace_id  # the card links to its trace
    ends = {e.data["tool"]: e.data["status"] for e in events if e.type == "tool_call" and e.data["phase"] == "end"}
    assert ends["propose_order"] == "DENIED"  # held by the gate: the Brain placed nothing
    assert events[-1].data["trace_id"] == trace_id

    # The operator approves in the UI; the API claims the card from PENDING_APPROVAL, queues the order,
    # then records the command. A late reject (expecting PENDING_APPROVAL) is refused.
    assert await brain(store).note("op-1", sid, "Operator approved order card; paper command cmd-42 queued.",
                                   widget_id=card["id"], widget_patch={"status": "APPROVED", "command_id": "cmd-42"},
                                   expect_status="PENDING_APPROVAL")
    assert not await brain(store).note("op-1", sid, "Operator rejected the card.", widget_id=card["id"],
                                       widget_patch={"status": "REJECTED"}, expect_status="PENDING_APPROVAL")
    stored = await store.messages(sid)
    assert [m["role"] for m in stored] == ["user", "assistant", "system"]
    stored_card = next(w for w in stored[1]["widgets"] if w["id"] == card["id"])
    assert stored_card["status"] == "APPROVED" and stored[1]["trace_id"] == trace_id

    # Next turn (a fresh Brain, as after a reload/restart) sees the whole story
    turn2 = Script([], ["Your BTC paper order is queued as cmd-42."])
    with override_model(PLANNER, turn2.model()):
        events2 = await collect(brain(store).turn("op-1", "What happened to my order?", sid))
    assert events2[-1].type == "done"
    assert "Analyze BTC and propose a trade" in turn2.prompts[0]
    assert "APPROVED" in turn2.prompts[0] and "cmd-42" in turn2.prompts[0]


async def test_a_session_belongs_to_its_operator():
    store = MemorySessionStore()
    with override_model(PLANNER, Script([], ["hi"]).model()):
        events = await collect(brain(store).turn("op-1", "hello"))
        sid = events[0].data["session_id"]
        other = await collect(brain(store).turn("op-2", "read their chat", sid))
    assert [e.type for e in other] == ["error"] and other[0].data["code"] == "SESSION_NOT_FOUND"


async def test_budget_exhaustion_is_an_error_event_not_an_exception():
    with override_model(PLANNER, Script([], ["x"]).model()):
        events = await collect(brain(budget=BudgetGuard(default_cap_usd=Decimal("0"))).turn("op", "hi"))
    assert events[-1].type == "error" and events[-1].data["code"] == "BUDGET"


async def test_stopping_a_turn_keeps_the_partial_answer():
    store = MemorySessionStore()
    script = Script([], ["partial "] + ["tail "] * 50, delay=0.05)
    with override_model(PLANNER, script.model()):
        turn = brain(store).turn("op", "long please")
        sid = None
        async for ev in turn:
            if ev.type == "session":
                sid = ev.data["session_id"]
            if ev.type == "token":
                break
        await turn.aclose()
    stored = await store.messages(sid)
    assert stored[-1]["role"] == "assistant" and "stopped by the operator" in stored[-1]["content"]


async def test_compaction_summarizes_old_turns_and_keeps_recent_ones(monkeypatch):
    store = MemorySessionStore()
    sid = await store.create_session("op", "t")
    for i in range(memory.COMPACT_AFTER + 2):
        await store.append_message(sid, "user" if i % 2 == 0 else "assistant", f"turn-{i}")
    with override_model(SUMMARIZER, TestModel(custom_output_text="Operator studied BTC; turns 0-9 folded.")):
        assert await memory.compact(gw(), store, sid) is True
    summary = await store.latest_summary(sid)
    assert summary["summary"].startswith("Operator studied BTC")
    ctx = await memory.load_context(store, sid)
    assert len(ctx.turns) == memory.KEEP_MESSAGES
    prompt = memory.render_prompt(ctx, "next")
    assert "Operator studied BTC" in prompt and "turn-0" not in prompt and f"turn-{memory.COMPACT_AFTER + 1}" in prompt


# --- SSE endpoint (agent_worker) -----------------------------------------------------------------

def test_brain_stream_endpoint_emits_typed_sse_events():
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from server import app  # noqa: PLC0415
    from test_agent_worker_and_fleet import _generate_hmac_headers  # noqa: PLC0415

    store = MemorySessionStore()
    script = Script([("propose_order", GOOD_ORDER)], ["Card ", "staged."])
    with override_model(PLANNER, script.model()), TestClient(app) as client:
        app.state.brain = brain(store)
        body = json.dumps({"operator_id": "op-1", "message": "analyze BTC and propose"}).encode()
        resp = client.post("/api/v1/brain/stream", content=body,
                           headers=_generate_hmac_headers("POST", "/api/v1/brain/stream", body))
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        frames = [f for f in resp.text.split("\n\n") if f.strip()]
        parsed = [(f.split("\n")[0].removeprefix("event: "), json.loads(f.split("\n")[1].removeprefix("data: ")))
                  for f in frames]
        types = [t for t, _ in parsed]
        assert types[0] == "session" and types[-1] == "done"
        assert {"token", "tool_call", "widget"} <= set(types)
        sid = parsed[0][1]["session_id"]

        path = f"/api/v1/brain/operators/op-1/sessions/{sid}"
        hist = client.get(path, headers=_generate_hmac_headers("GET", path, b""))
        assert hist.status_code == 200
        assert [m["role"] for m in hist.json()["messages"]] == ["user", "assistant"]
        other = f"/api/v1/brain/operators/op-2/sessions/{sid}"
        assert client.get(other, headers=_generate_hmac_headers("GET", other, b"")).status_code == 404

        unsigned = client.post("/api/v1/brain/stream", content=body, headers={"Content-Type": "application/json"})
        assert unsigned.status_code == 401


# --- Conversation history (search, archive, paging) -----------------------------------------------

async def _history(store):
    """Three conversations for op-1 (oldest first) and one for op-2."""
    a = await store.create_session("op-1", "BTC breakout plan")
    await store.append_message(a, "user", "analyze BTC")
    await store.append_message(a, "assistant", "BTC is at the top of its range")
    b = await store.create_session("op-1", "ETH funding")
    await store.append_message(b, "user", "what is 100% of ETH funding_rate?")
    await store.append_message(b, "system", "Operator approved card c1")
    c = await store.create_session("op-1", "XRP dip")
    await store.append_message(c, "user", "  watch\n XRP   for a dip  ")
    other = await store.create_session("op-2", "BTC elsewhere")
    await store.append_message(other, "user", "analyze BTC")
    return a, b, c


async def test_history_lists_newest_first_with_counts_and_previews():
    store = MemorySessionStore()
    a, b, c = await _history(store)
    rows = await store.list_sessions("op-1")
    assert [r["id"] for r in rows] == [c, b, a]
    by_id = {r["id"]: r for r in rows}
    assert by_id[a]["message_count"] == 2
    assert by_id[a]["preview"] == {"role": "assistant", "content": "BTC is at the top of its range"}
    assert by_id[b]["message_count"] == 1  # the system line is not part of the conversation
    assert by_id[b]["preview"]["role"] == "user"
    assert by_id[c]["preview"]["content"] == "watch XRP for a dip"
    assert all(r["archived"] is False for r in rows)


async def test_history_search_matches_titles_and_messages_for_one_operator():
    store = MemorySessionStore()
    a, b, _ = await _history(store)
    assert [r["id"] for r in await store.list_sessions("op-1", query="btc")] == [a]
    assert [r["id"] for r in await store.list_sessions("op-1", query="TOP OF ITS")] == [a]  # message text
    assert [r["id"] for r in await store.list_sessions("op-1", query="100%")] == [b]
    assert await store.list_sessions("op-1", query="nothing like this") == []


async def test_history_pages_with_a_cursor():
    store = MemorySessionStore()
    a, b, c = await _history(store)
    first = await store.list_sessions("op-1", limit=2)
    assert [r["id"] for r in first] == [c, b]
    rest = await store.list_sessions("op-1", limit=2, before=session_cursor(first[-1]))
    assert [r["id"] for r in rest] == [a]
    assert [r["id"] for r in await store.list_sessions("op-1", before="not-a-cursor")] == [c, b, a]


async def test_archive_hides_a_conversation_until_it_gets_a_new_message():
    store = MemorySessionStore()
    a, b, c = await _history(store)
    archived = await store.update_session(a, "op-1", archived=True)
    assert archived["archived"] is True
    assert [r["id"] for r in await store.list_sessions("op-1")] == [c, b]
    assert [r["id"] for r in await store.list_sessions("op-1", archived=True)] == [a]
    assert await store.get_session(a, "op-1") is not None  # still readable, so it can be opened
    await store.append_message(a, "user", "one more question")
    assert [r["id"] for r in await store.list_sessions("op-1")][0] == a
    assert await store.list_sessions("op-1", archived=True) == []


async def test_rename_keeps_the_activity_order_and_the_owner():
    store = MemorySessionStore()
    a, b, c = await _history(store)
    renamed = await store.update_session(a, "op-1", title="Weekly BTC review")
    assert renamed["title"] == "Weekly BTC review"
    assert [r["id"] for r in await store.list_sessions("op-1")] == [c, b, a]
    assert await store.update_session(a, "op-2", title="hijack") is None
    assert (await store.get_session(a, "op-1"))["title"] == "Weekly BTC review"


def test_history_endpoints_page_rename_and_archive():
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from server import app  # noqa: PLC0415
    from test_agent_worker_and_fleet import _generate_hmac_headers  # noqa: PLC0415

    store = MemorySessionStore()
    with TestClient(app) as client:
        app.state.brain = brain(store)
        a, b, c = asyncio.run(_history(store))

        def post(path, payload):
            raw = json.dumps(payload).encode()
            return client.post(path, content=raw, headers=_generate_hmac_headers("POST", path, raw))

        def patch(path, payload):
            raw = json.dumps(payload).encode()
            return client.patch(path, content=raw, headers=_generate_hmac_headers("PATCH", path, raw))

        search = "/api/v1/brain/operators/op-1/sessions/search"
        page = post(search, {"limit": 2}).json()
        assert [s["id"] for s in page["sessions"]] == [c, b] and page["next_cursor"]
        last = post(search, {"limit": 2, "before": page["next_cursor"]}).json()
        assert [s["id"] for s in last["sessions"]] == [a] and last["next_cursor"] is None
        assert [s["id"] for s in post(search, {"query": "eth"}).json()["sessions"]] == [b]

        path = f"/api/v1/brain/operators/op-1/sessions/{a}"
        assert patch(path, {"title": "  Weekly   BTC  "}).json()["session"]["title"] == "Weekly BTC"
        assert patch(path, {"title": "   "}).status_code == 422
        assert patch(path, {}).status_code == 422
        assert patch(path, {"archived": True}).json()["session"]["archived"] is True
        assert [s["id"] for s in post(search, {"archived": True}).json()["sessions"]] == [a]
        assert patch(f"/api/v1/brain/operators/op-2/sessions/{a}", {"archived": False}).status_code == 404
        unsigned = client.post(search, content=b"{}", headers={"Content-Type": "application/json"})
        assert unsigned.status_code == 401


# --- Migration 024 against a real database -----------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_braintest") as url:
        yield url


def as_role(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{parts.netloc.rsplit('@', 1)[-1]}"))


async def test_session_store_runs_as_the_telemetry_role(db_url):
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"),
                                     min_size=1, max_size=2)
    try:
        store = PgSessionStore(pool)
        sid = await store.create_session("op-1", "BTC")
        card = widget_for_tool("propose_order", GOOD_ORDER, call_id="c1", trace_id=str(uuid.uuid4()))
        await store.append_message(sid, "user", "analyze BTC")
        mid = await store.append_message(sid, "assistant", "card staged", [card], card["trace_id"])
        assert await store.update_widget(sid, "c1", {"status": "APPROVED", "command_id": "cmd-1"})
        msgs = await store.messages(sid)
        assert msgs[-1]["id"] == mid and msgs[-1]["widgets"][0]["status"] == "APPROVED"
        await store.save_summary(sid, "summary", mid)
        assert (await store.latest_summary(sid))["covers_message_id"] == mid
        assert await store.get_session(sid, "op-2") is None
        assert [s["id"] for s in await store.list_sessions("op-1")] == [sid]
        with pytest.raises(asyncpg.InsufficientPrivilegeError):  # history is never deleted by the service
            await pool.execute("DELETE FROM ai_messages")
    finally:
        await pool.close()


async def test_history_queries_run_as_the_telemetry_role(db_url):
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"),
                                     min_size=1, max_size=2)
    try:
        store = PgSessionStore(pool)
        op = f"op-hist-{uuid.uuid4().hex[:6]}"
        a = await store.create_session(op, "BTC breakout plan")
        await store.append_message(a, "user", "analyze BTC")
        await store.append_message(a, "assistant", "BTC is at the top of its range")
        b = await store.create_session(op, "ETH 100% funding_rate")
        await store.append_message(b, "user", "what is 100% of ETH funding_rate?")
        await store.append_message(b, "system", "Operator approved card c1")

        rows = await store.list_sessions(op)
        assert [r["id"] for r in rows] == [b, a]
        assert rows[0]["message_count"] == 1 and rows[0]["preview"]["role"] == "user"
        assert rows[1]["preview"] == {"role": "assistant", "content": "BTC is at the top of its range"}

        assert [r["id"] for r in await store.list_sessions(op, query="top of ITS")] == [a]
        assert [r["id"] for r in await store.list_sessions(op, query="100%")] == [b]
        assert await store.list_sessions(op, query="1_0%") == []  # _ and % are literal, not wildcards

        first = await store.list_sessions(op, limit=1)
        assert [r["id"] for r in await store.list_sessions(op, limit=1, before=session_cursor(first[0]))] == [a]

        before = (await store.get_session(a, op))["updated_at"]
        assert (await store.update_session(a, op, title="Weekly BTC"))["title"] == "Weekly BTC"
        assert (await store.get_session(a, op))["updated_at"] == before  # a rename is not activity
        assert (await store.update_session(a, op, archived=True))["archived"] is True
        assert [r["id"] for r in await store.list_sessions(op)] == [b]
        assert [r["id"] for r in await store.list_sessions(op, archived=True)] == [a]
        assert await store.update_session(a, "someone-else", archived=False) is None
        await store.append_message(a, "user", "back to this one")
        assert [r["id"] for r in await store.list_sessions(op)] == [a, b]
    finally:
        await pool.close()


async def test_agent_role_cannot_touch_sessions_and_api_reads_traces(db_url):
    agent = await asyncpg.create_pool(as_role(db_url, "cryptonite_agent_ro", "agent_ro_dev_pass"), min_size=1, max_size=1)
    api = await asyncpg.create_pool(as_role(db_url, "cryptonite_api", "api_dev_pass"), min_size=1, max_size=1)
    try:
        for table in ("ai_sessions", "ai_messages", "ai_session_summaries", "ai_decision_traces"):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await agent.fetch(f"SELECT * FROM {table} LIMIT 1")
        await api.fetch("SELECT id, tool_calls FROM ai_decision_traces LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute("DELETE FROM ai_decision_traces")
    finally:
        await agent.close()
        await api.close()


async def test_streamed_turn_writes_the_trace_the_order_card_links_to(db_url):
    pool = await asyncpg.create_pool(as_role(db_url, "cryptonite_ai_telemetry", "ai_telemetry_dev_pass"),
                                     min_size=1, max_size=2)
    api = await asyncpg.create_pool(as_role(db_url, "cryptonite_api", "api_dev_pass"), min_size=1, max_size=1)
    try:
        b = Brain(Gateway(pool=pool, routes=routes(), budget=BudgetGuard(pool=pool, refresh_s=0)),
                  PgSessionStore(pool), replace(BRAIN, tools=(propose_order,)))
        with override_model(PLANNER, Script([("propose_order", GOOD_ORDER)], ["staged"]).model()):
            events = await collect(b.turn("op-db", "propose BTC"))
        card = next(e.data["widget"] for e in events if e.type == "widget")
        row = await api.fetchrow("SELECT role, session_id, status, tool_calls FROM ai_decision_traces WHERE id = $1",
                                 uuid.UUID(card["trace_id"]))
        assert (row["role"], row["status"], row["session_id"]) == (BRAIN_PLAN, "OK", events[0].data["session_id"])
        assert json.loads(row["tool_calls"])[0]["status"] == "DENIED"
    finally:
        with contextlib.suppress(Exception):
            await api.close()
        await pool.close()


# --- Eval set brain_v1 ---------------------------------------------------------------------------

def test_brain_eval_set_is_well_formed():
    cases = load_set("brain_v1")
    names = {t.name for t in BRAIN_TOOLS}
    assert len(cases) >= 10 and all(c["persona"] == "brain" and c["kind"] == "tool_chain" for c in cases)
    for c in cases:
        assert set(c.get("must_call", []) + c.get("must_request", []) + c.get("must_not_call", [])) <= names
    assert eval_runner.PERSONAS["brain"] is BRAIN


async def test_brain_eval_counts_a_held_proposal_as_requested(monkeypatch):
    case = next(c for c in load_set("brain_v1") if c["id"] == "brain-3")
    live = replace(LIVE_MARKET_DATA, func=lambda symbols="BTCUSDT": '[{"symbol": "ETHUSDT", "last_price": 2500}]')
    monkeypatch.setitem(eval_runner.PERSONAS, "brain", replace(BRAIN, tools=(live, propose_order)))
    script = Script([("get_live_market_data", {"symbols": "ETHUSDT"}), ("propose_order", GOOD_ORDER)], ["staged"])
    with override_model(PLANNER, script.model()):
        res = await eval_runner.run_case(Gateway(routes=routes(), budget=BudgetGuard()), case, PLANNER)
    assert res.checks == {"called_required_tools": True, "answered": True, "requested_approval_tools": True}
    assert res.passed


# --- A SELL card needs something to sell: spot cannot be shorted ----------------------------------------------

SELL_ORDER = {**GOOD_ORDER, "side": "SELL"}


async def _sell_turn(holdings, order=SELL_ORDER):
    turn = Script([("propose_order", order)], ["Shorting the breakdown below 1.40."])
    with override_model(PLANNER, turn.model()):
        return await collect(brain(MemorySessionStore(), holdings=holdings).turn("op-1", "Short XRP"))


def _cards(events):
    return [e.data["widget"] for e in events if e.type == "widget" and e.data["widget"]["kind"] == "order_card"]


async def test_a_sell_card_is_refused_when_nothing_is_held_and_the_reply_says_why():
    async def nothing(instrument):
        return Decimal("0")

    events = await _sell_turn(nothing)
    assert _cards(events) == []
    assert events[-1].type == "done" and "spot cannot be shorted" in events[-1].data["content"]


async def test_a_sell_card_is_refused_when_the_holding_cannot_be_read():
    async def broken(instrument):
        raise RuntimeError("database unavailable")

    events = await _sell_turn(broken)
    assert _cards(events) == []  # fails closed


async def test_dust_is_not_a_holding():
    async def dust(instrument):
        return Decimal("0.0000000001")  # below MIN_HOLDING_QTY

    assert _cards(await _sell_turn(dust)) == []


async def test_a_sell_card_is_made_for_a_real_holding():
    async def held(instrument):
        return Decimal("0.5")

    events = await _sell_turn(held)
    cards = _cards(events)
    assert len(cards) == 1 and cards[0]["side"] == "SELL"
    assert "spot cannot be shorted" not in events[-1].data["content"]


async def test_a_buy_card_needs_no_holding():
    async def nothing(instrument):
        return Decimal("0")

    with override_model(PLANNER, Script([("propose_order", GOOD_ORDER)], ["ok"]).model()):
        events = await collect(brain(MemorySessionStore(), holdings=nothing).turn("op-1", "Buy BTC"))
    assert [c["side"] for c in _cards(events)] == ["BUY"]

