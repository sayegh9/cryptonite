"""WS-2D: Brain routing hygiene (plan row 2D).

- ``brain.plan`` carries an output cap and a thinking budget, and stays FAIL_CLOSED.
- When Pro is unavailable before the operator sees any output, the turn is answered once on ``brain.chat`` with the
  read-only tools only, and labelled as degraded. Every other failure keeps today's behaviour.

Models are pydantic-ai test doubles swapped in with ``override_model``: no network, no paid model call.
"""

import contextlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from cryptonite_agents.brain import (
    BRAIN_CHAT,
    BRAIN_PLAN,
    Brain,
    MemorySessionStore,
    brain_gateway_routes,
)
from cryptonite_agents.brain import agent as agent_mod
from cryptonite_agents.brain.toolset import BRAIN_TOOLS, propose_order
from cryptonite_agents.gateway import (
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    RiskClass,
)
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import FLASH, FLASH_LITE, PRO
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

WRITE_TOOLS = {"propose_order", "stage_debate_proposal", "queue_strategy_research"}
PINNED = "test:pinned-plan"


class ScriptedModel:
    """One model double. Each call is recorded with the tools and instructions it was offered. It answers in text
    chunks, optionally after one tool call, and can fail with ``error`` after those."""

    def __init__(self, answer=(), *, error: Exception | None = None, call_tool: tuple[str, dict] | None = None):
        self.answer = list(answer)
        self.error = error
        self.call_tool = call_tool
        self.calls = 0
        self.tool_names: list[str] = []
        self.instructions: list[str | None] = []

    def model(self) -> FunctionModel:
        async def stream(messages, info: AgentInfo):
            self.calls += 1
            self.tool_names = sorted(t.name for t in info.function_tools)
            self.instructions.append(info.instructions)
            if self.call_tool and not _has_tool_return(messages):
                name, args = self.call_tool
                yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id="call-0")}
                return
            for chunk in self.answer:
                yield chunk
            if self.error is not None:
                raise self.error

        def sync(messages, info):  # the Brain always streams a turn; a plain run here would be a test bug
            raise AssertionError("the Brain streams its turns")

        return FunctionModel(sync, stream_function=stream)


def _has_tool_return(messages) -> bool:
    return any(isinstance(p, ToolReturnPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts)


@contextlib.contextmanager
def models(plan: ScriptedModel, chat: ScriptedModel, backup: ScriptedModel):
    """The production routes, with Pro, Flash and Flash-Lite swapped for the doubles."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(override_model(PRO, plan.model()))
        stack.enter_context(override_model(FLASH, chat.model()))
        stack.enter_context(override_model(FLASH_LITE, backup.model()))
        yield


def make_brain(routes=None) -> tuple[Brain, MemorySessionStore]:
    store = MemorySessionStore()
    gateway = Gateway(routes=routes if routes is not None else brain_gateway_routes(), budget=BudgetGuard())
    return Brain(gateway, store), store


async def collect(agen) -> list:
    return [ev async for ev in agen]


def types_of(events) -> list[str]:
    return [e.type for e in events]


# --- Routes and the read-only toolset (no model) ----------------------------------------------------


def test_brain_plan_caps_output_and_thinking_and_stays_fail_closed():
    route = brain_gateway_routes()[BRAIN_PLAN]
    assert (route.max_output_tokens, route.thinking_budget) == (8000, 4096)
    assert route.fallback_policy is FallbackPolicy.FAIL_CLOSED and route.fallback_model is None
    assert route.primary_model == PRO and route.priority is BudgetPriority.INTERACTIVE


def test_brain_chat_route_has_output_and_thinking_caps_and_keeps_its_degrade_path():
    route = brain_gateway_routes()[BRAIN_CHAT]
    assert (route.max_output_tokens, route.thinking_budget) == (4000, 1024)
    assert (route.primary_model, route.fallback_model, route.fallback_policy) == (
        FLASH, FLASH_LITE, FallbackPolicy.DEGRADE)


def test_readonly_tools_are_computed_from_the_specs_and_exclude_writes():
    names = {t.name for t in agent_mod.BRAIN_READONLY_TOOLS}
    assert WRITE_TOOLS <= {t.name for t in BRAIN_TOOLS}  # the exclusions bite: these tools are in the Brain's set
    assert names.isdisjoint(WRITE_TOOLS)
    assert "get_live_market_data" in names
    assert names == {t.name for t in BRAIN_TOOLS if t.risk is RiskClass.READ and not t.side_effects}


def test_the_read_only_check_refuses_writes_and_side_effect_tools():
    with pytest.raises(RuntimeError, match="propose_order"):
        agent_mod._assert_read_only((propose_order,))
    side_effect = [t for t in BRAIN_TOOLS if t.name in {"stage_debate_proposal", "queue_strategy_research"}]
    assert len(side_effect) == 2
    for spec in side_effect:
        with pytest.raises(RuntimeError, match=spec.name):
            agent_mod._assert_read_only((spec,))
    agent_mod._assert_read_only(agent_mod.BRAIN_READONLY_TOOLS)  # the real degraded set passes


def test_the_degraded_note_says_what_is_not_possible():
    note = " ".join(agent_mod.DEGRADED_NOTE.split())
    assert note.startswith("DEGRADED MODE: the main model is unavailable.")
    assert "cannot propose orders, stage debates or queue research in this turn" in note
    assert agent_mod.DEGRADED_PREFIX == "_[degraded: answered by the backup model, read-only]_\n\n"


# --- The turn: retry only before the operator saw any output ----------------------------------------


async def test_pro_outage_before_any_output_is_answered_read_only_on_brain_chat():
    brain, store = make_brain()
    plan = ScriptedModel(error=RuntimeError("provider down"))
    chat = ScriptedModel(["BTC is ", "range-bound."])
    backup = ScriptedModel(["never used"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "What is BTC doing?"))

    assert (plan.calls, chat.calls, backup.calls) == (1, 1, 0)
    assert chat.tool_names == sorted(t.name for t in agent_mod.BRAIN_READONLY_TOOLS)
    assert "propose_order" not in chat.tool_names
    assert chat.instructions == [agent_mod.BRAIN_INSTRUCTIONS + agent_mod.DEGRADED_NOTE]
    assert "".join(e.data["text"] for e in events if e.type == "token") == "BTC is range-bound."
    done = events[-1]
    assert done.type == "done" and done.data["degraded"] is True
    assert done.data["content"] == agent_mod.DEGRADED_PREFIX + "BTC is range-bound."
    session_trace = [e.data["trace_id"] for e in events if e.type == "session"][-1]
    assert done.data["trace_id"] == session_trace  # the answer links to the trace that produced it
    stored = await store.messages(events[0].data["session_id"])
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[1]["content"] == done.data["content"]


async def test_a_normal_turn_is_not_degraded():
    brain, _ = make_brain()
    plan, chat, backup = ScriptedModel(["Flat."]), ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "Anything?"))
    assert events[-1].type == "done" and events[-1].data["degraded"] is False
    assert not events[-1].data["content"].startswith(agent_mod.DEGRADED_PREFIX)
    assert (plan.calls, chat.calls) == (1, 0)


async def test_budget_block_on_the_plan_is_an_error_and_is_not_retried():
    routes = brain_gateway_routes()
    routes[BRAIN_PLAN] = replace(routes[BRAIN_PLAN], daily_cap_usd=Decimal("0"))
    brain, _ = make_brain(routes)
    plan, chat, backup = ScriptedModel(["x"]), ScriptedModel(["x"]), ScriptedModel(["x"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "hi"))
    assert types_of(events) == ["error"] and events[0].data["code"] == "BUDGET"
    assert "ROLE_DAILY_CAP_REACHED" in events[0].data["message"]
    assert (plan.calls, chat.calls, backup.calls) == (0, 0, 0)


async def test_invalid_model_output_keeps_todays_error_and_is_not_retried():
    brain, store = make_brain()
    plan = ScriptedModel(error=UnexpectedModelBehavior("Exceeded maximum output retries (2)"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "Buy BTC"))
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"
    assert "model output invalid" in events[-1].data["message"]
    assert (chat.calls, backup.calls) == (0, 0)
    assert [m["role"] for m in await store.messages(events[0].data["session_id"])] == ["user"]


async def test_failure_after_a_token_is_not_retried():
    brain, store = make_brain()
    plan = ScriptedModel(["partial "], error=RuntimeError("provider down"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "analyze"))
    assert types_of(events) == ["session", "token", "error"]
    assert events[-1].data["code"] == "MODEL_UNAVAILABLE"
    assert (chat.calls, backup.calls) == (0, 0)
    stored = await store.messages(events[0].data["session_id"])
    assert stored[-1]["content"].startswith("partial") and "answer incomplete" in stored[-1]["content"]
    assert not stored[-1]["content"].startswith(agent_mod.DEGRADED_PREFIX)


async def test_failure_after_a_widget_is_not_retried():
    brain, _ = make_brain()
    plan = ScriptedModel(call_tool=("show_price_chart", {"symbol": "BTCUSDT", "timeframe": "4h"}),
                         error=RuntimeError("provider down"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "chart BTC"))
    assert "widget" in types_of(events)
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"
    assert (chat.calls, backup.calls) == (0, 0)


async def test_a_tool_call_with_no_widget_is_not_retried(monkeypatch):
    # queue_strategy_research is READ risk with side effects. With no API token it returns an error before any request
    # (research/tools.py), so nothing reaches the network. The call still happened, so the operator saw output.
    monkeypatch.delenv("CRYPTONITE_AGENT_API_TOKEN", raising=False)
    brain, store = make_brain()
    research = ("queue_strategy_research", {"idea": "mean reversion", "instrument_id": "BTCUSDT.BINANCE",
                                            "timeframe": "1h"})
    plan = ScriptedModel(call_tool=research, error=RuntimeError("provider down"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "research BTC mean reversion"))
    assert any(e.type == "tool_call" and e.data.get("tool") == "queue_strategy_research" for e in events)
    assert "widget" not in types_of(events)
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"
    assert (chat.calls, backup.calls) == (0, 0)  # the backup model was never invoked
    assert [m["role"] for m in await store.messages(events[0].data["session_id"])] == ["user"]


async def test_a_degraded_retry_that_also_fails_is_model_unavailable():
    brain, store = make_brain()
    plan = ScriptedModel(error=RuntimeError("provider down"))
    chat = ScriptedModel(error=RuntimeError("chat down"))
    backup = ScriptedModel(error=RuntimeError("backup down"))
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "What is BTC doing?"))
    assert (plan.calls, chat.calls, backup.calls) == (1, 1, 1)  # brain.chat's own DEGRADE fallback was tried too
    assert "done" not in types_of(events)
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"
    assert [m["role"] for m in await store.messages(events[0].data["session_id"])] == ["user"]


async def test_a_write_tool_handed_to_the_degraded_turn_is_refused_before_any_model_call(monkeypatch):
    monkeypatch.setattr(agent_mod, "BRAIN_READONLY_TOOLS", (*agent_mod.BRAIN_READONLY_TOOLS, propose_order))
    brain, _ = make_brain()
    plan = ScriptedModel(error=RuntimeError("provider down"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with models(plan, chat, backup), pytest.raises(RuntimeError, match="propose_order"):
        await collect(brain.turn("op-1", "buy BTC"))
    assert (chat.calls, backup.calls) == (0, 0)


async def test_a_pinned_model_is_not_swapped_for_the_backup():
    brain, store = make_brain()
    pinned = ScriptedModel(error=RuntimeError("provider down"))
    chat, backup = ScriptedModel(["never"]), ScriptedModel(["never"])
    with override_model(PINNED, pinned.model()), models(ScriptedModel(["never"]), chat, backup):
        events = await collect(brain.turn("op-1", "analyze", model_override=PINNED))
    assert pinned.calls == 1 and (chat.calls, backup.calls) == (0, 0)
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"


async def test_a_degraded_answer_stopped_by_the_operator_keeps_its_label():
    brain, store = make_brain()
    plan = ScriptedModel(error=RuntimeError("provider down"))
    chat = ScriptedModel(["first ", "second ", "third "])
    backup = ScriptedModel(["never"])
    with models(plan, chat, backup):
        turn = brain.turn("op-1", "long answer please")
        sid = None
        async for ev in turn:
            if ev.type == "session":
                sid = ev.data["session_id"]
            if ev.type == "token":
                break
        await turn.aclose()
    stored = await store.messages(sid)
    assert stored[-1]["role"] == "assistant"
    assert stored[-1]["content"].startswith(agent_mod.DEGRADED_PREFIX)
    assert "stopped by the operator" in stored[-1]["content"]


async def test_a_degraded_answer_that_fails_midway_keeps_its_label_and_is_not_retried_again():
    brain, store = make_brain()
    plan = ScriptedModel(error=RuntimeError("provider down"))
    chat = ScriptedModel(["partial "], error=RuntimeError("chat cut"))
    backup = ScriptedModel(["never"])
    with models(plan, chat, backup):
        events = await collect(brain.turn("op-1", "analyze"))
    assert (plan.calls, chat.calls, backup.calls) == (1, 1, 0)  # a token was out: no fallback, no second retry
    assert events[-1].type == "error" and events[-1].data["code"] == "MODEL_UNAVAILABLE"
    stored = await store.messages(events[0].data["session_id"])
    assert stored[-1]["content"].startswith(agent_mod.DEGRADED_PREFIX) and "answer incomplete" in stored[-1]["content"]
