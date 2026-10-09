"""The Brain: one planner agent with a tool set, sessions and memory (blueprint §4.2, A3.2).

``Brain.turn`` runs one operator message through ``Gateway.stream`` and yields typed events:

- ``session``    {session_id, trace_id}           first event
- ``token``      {text}                           streamed answer text
- ``tool_call``  {id, tool, args, phase, status}  phase "start" when called, "end" with the gate's status
- ``widget``     {widget}                         price_chart | tearsheet | order_card | debate_summary
- ``done``       {session_id, message_id, trace_id, model, cost_usd, content}
- ``error``      {code, message}

The Brain can never place an order: ``propose_order`` is ADD_RISK, the gate holds it, and the card
goes to the operator, whose approval uses the existing paper manual-order API.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from cryptonite_agents.brain import memory
from cryptonite_agents.brain.routes import BRAIN_PLAN
from cryptonite_agents.brain.store import SessionStore
from cryptonite_agents.brain.toolset import BRAIN_TOOLS
from cryptonite_agents.brain.widgets import widget_for_tool
from cryptonite_agents.gateway import BudgetExceeded, CallContext, Gateway, GatewayError
from cryptonite_agents.persona import Persona

logger = logging.getLogger("cryptonite.agents.brain")

BRAIN_INSTRUCTIONS = """\
You are the Brain of Cryptonite, a crypto trading terminal. You work for one operator, in PAPER trading.
You plan, look things up with your tools, and answer in the operator's language: short, dense, no hype.
State uncertainty plainly and say which data you used.

How to work:
1. Market questions: call get_live_market_data for 24h ticker prices; call get_klines to fetch historical open/high/low/close candles for any symbol (BTCUSDT, SOLUSDT, WLDUSDT, etc.); call show_price_chart only for fed symbols (BTCUSDT, ETHUSDT) when discussing price action; get_scanner_context for the scanner's recent signals on an instrument.
2. Research: query_postgres_readonly (read-only SQL on agent_safe_* views) and query_parquet_duckdb
   (aggregate historical bars; never SELECT * without LIMIT).
3. Backtests: explain_backtest to read a tearsheet; compare_live_vs_backtest for live drift.
4. A full multi-agent debate: stage_debate_proposal (it queues a debate whose decision still needs approval).
5. Orders: you cannot place orders. When the operator wants a trade, or your analysis supports one, call
   propose_order with instrument, side, allocation_pct (1 to 25 percent of the mandate's budget), order type,
   your suggested stop and a one-line rationale. The call is held for approval ("Tool call denied:
   ADD_RISK_REQUIRES_APPROVAL") and the operator sees an order card with Approve / Reject buttons. That is the
   expected result: do not retry and do not claim the order was placed. Tell the operator the card is waiting
   for their approval. An approved order is sized by allocation_pct, and the position is protected by the exit
   monitor's deterministic hard and trailing stops, not by your stop price: say so, and if your stop is much
   tighter than a few percent, say the position will not be stopped there. Never propose more than one card
   for the same idea.
6. The conversation summary and recent turns are context. System lines report what the operator did
   (for example an approved card and its paper command id).
"""

BRAIN = Persona(name="brain", role=BRAIN_PLAN, instructions=BRAIN_INSTRUCTIONS, tools=BRAIN_TOOLS)

CANCELLED_NOTE = "\n\n_[stopped by the operator]_"


@dataclass
class BrainEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class SessionNotFound(Exception):
    pass


class Brain:
    def __init__(self, gateway: Gateway, store: SessionStore, persona: Persona = BRAIN) -> None:
        self.gateway = gateway
        self.store = store
        self.persona = persona

    async def open_session(self, operator_id: str, session_id: str | None, first_message: str) -> str:
        if session_id:
            if await self.store.get_session(session_id, operator_id) is None:
                raise SessionNotFound(session_id)
            return session_id
        title = " ".join(first_message.split())[:80] or "New conversation"
        return await self.store.create_session(operator_id, title)

    async def turn(self, operator_id: str, message: str, session_id: str | None = None,
                   *, model_override: str | None = None) -> AsyncIterator[BrainEvent]:
        try:
            sid = await self.open_session(operator_id, session_id, message)
        except SessionNotFound:
            yield BrainEvent("error", {"code": "SESSION_NOT_FOUND", "message": "Unknown session"})
            return

        ctx = await memory.load_context(self.store, sid)
        await self.store.append_message(sid, "user", message)
        prompt = memory.render_prompt(ctx, message)

        text: list[str] = []
        widgets: list[dict[str, Any]] = []
        calls: dict[str, dict[str, Any]] = {}
        trace_id: str | None = None
        finished = False
        call_ctx = CallContext(role=self.persona.role, session_id=sid)
        stream = self.gateway.stream(
            self.persona.role, prompt, instructions=self.persona.instructions,
            output_type=self.persona.output_type, tools=self.persona.tools,
            model_override=model_override, context=call_ctx,
        )
        try:
            async with contextlib.aclosing(stream) as events:
                async for ev in events:
                    if ev.kind == "start":
                        trace_id = ev.data["trace_id"]
                        yield BrainEvent("session", {"session_id": sid, "trace_id": trace_id})
                    elif ev.kind == "token":
                        text.append(ev.text)
                        yield BrainEvent("token", {"text": ev.text})
                    elif ev.kind == "tool_call":
                        call_id = ev.data.get("tool_call_id") or ev.data["tool"]
                        calls[call_id] = ev.data
                        yield BrainEvent("tool_call", {"id": call_id, "tool": ev.data["tool"],
                                                       "args": ev.data.get("args") or {}, "phase": "start",
                                                       "status": "RUNNING"})
                    elif ev.kind == "tool_result":
                        call_id = ev.data.get("tool_call_id") or ev.data["tool"]
                        status = ev.data.get("status", "OK")
                        yield BrainEvent("tool_call", {"id": call_id, "tool": ev.data["tool"], "phase": "end",
                                                       "status": status})
                        args = (calls.get(call_id) or {}).get("args") or {}
                        widget = widget_for_tool(ev.data["tool"], args, call_id=call_id, status=status,
                                                 content=ev.data.get("content"), trace_id=trace_id)
                        if widget is not None:
                            widgets.append(widget)
                            yield BrainEvent("widget", {"widget": widget})
                    elif ev.kind == "done" and ev.result is not None:
                        res = ev.result
                        content = "".join(text).strip() or str(res.output)
                        msg_id = await self.store.append_message(sid, "assistant", content, widgets, str(res.trace_id))
                        finished = True
                        yield BrainEvent("done", {
                            "session_id": sid, "message_id": msg_id, "trace_id": str(res.trace_id),
                            "model": res.model, "cost_usd": str(res.cost_usd), "content": content,
                        })
        except BudgetExceeded as e:
            yield BrainEvent("error", {"code": "BUDGET", "message": f"LLM budget: {e.reason}"})
            return
        except GatewayError as e:
            logger.error("Brain turn failed for session %s: %s", sid, e)
            await self._keep_partial(sid, text, widgets, trace_id, note="\n\n_[the model failed; answer incomplete]_")
            yield BrainEvent("error", {"code": "MODEL_UNAVAILABLE", "message": str(e)})
            return
        except (GeneratorExit, asyncio.CancelledError):
            if not finished:  # the operator pressed stop: keep what they already saw
                await asyncio.shield(self._keep_partial(sid, text, widgets, trace_id, note=CANCELLED_NOTE))
            raise

        try:
            await memory.compact(self.gateway, self.store, sid)
        except Exception as e:  # a missed compaction only means a longer prompt next turn
            logger.warning("Session %s compaction skipped: %s", sid, e)

    async def _keep_partial(self, sid: str, text: list[str], widgets: list[dict[str, Any]],
                            trace_id: str | None, note: str) -> None:
        if not text and not widgets:
            return
        try:
            await self.store.append_message(sid, "assistant", "".join(text).strip() + note, widgets, trace_id)
        except Exception as e:
            logger.warning("Could not store the partial answer for %s: %s", sid, e)

    async def note(self, operator_id: str, session_id: str, content: str,
                   widget_id: str | None = None, widget_patch: dict[str, Any] | None = None,
                   expect_status: str | None = None) -> bool:
        """Record what the operator did (e.g. approved a card) so later turns know it. With
        ``expect_status`` the widget is only patched from that status; returns False (and records
        nothing) when it was not."""
        if await self.store.get_session(session_id, operator_id) is None:
            raise SessionNotFound(session_id)
        if widget_id and widget_patch:
            if not await self.store.update_widget(session_id, widget_id, widget_patch, expect_status) and expect_status:
                return False
        await self.store.append_message(session_id, "system", content)
        return True
