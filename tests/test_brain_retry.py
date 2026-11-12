"""Retry of a failed Brain turn: the same request again, stored once, and never a second order card for the same idea.

No model and no database: a scripted gateway and the in-memory session store.
"""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from cryptonite_agents.brain import BRAIN, Brain, MemorySessionStore
from cryptonite_agents.brain.agent import retried_turn
from cryptonite_agents.brain.toolset import propose_order, show_price_chart
from cryptonite_agents.gateway import GatewayError
from cryptonite_agents.gateway.gateway import HELD_FOR_APPROVAL_PREFIX

ORDER = {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "allocation_pct": 5, "order_type": "MARKET",
         "stop_loss": 58000, "rationale": "range low"}
ASK = "Buy BTC at the range low"


def ev(kind, data=None, text="", result=None):
    return SimpleNamespace(kind=kind, data=data or {}, text=text, result=result)


def card_events(args=ORDER):
    return [ev("start", {"trace_id": "t1"}),
            ev("tool_call", {"tool_call_id": "c0", "tool": "propose_order", "args": args}),
            ev("tool_result", {"tool_call_id": "c0", "tool": "propose_order", "status": "DENIED",
                               "content": HELD_FOR_APPROVAL_PREFIX + " waiting"}),
            ev("token", text="Done."),
            ev("done", result=SimpleNamespace(output="Done.", model="test", cost_usd=Decimal("0"), trace_id="t1"))]


class Gateway:
    """Each call of ``stream`` plays the next script: an exception is raised, a list of events is yielded."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.prompts: list[str] = []

    async def stream(self, role, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        script = self.scripts.pop(0)
        for event in [script] if isinstance(script, Exception) else script:
            if isinstance(event, Exception):
                raise event
            yield event


def brain_with(*scripts) -> tuple[Brain, Gateway, MemorySessionStore]:
    gateway, store = Gateway(*scripts), MemorySessionStore()
    return Brain(gateway, store, replace(BRAIN, tools=(show_price_chart, propose_order))), gateway, store


async def turn(brain, message, sid=None, retry=False):
    events = [e async for e in brain.turn("op-1", message, sid, retry=retry)]
    session = next((e.data["session_id"] for e in events if e.data.get("session_id")), sid)
    cards = [e.data["widget"] for e in events if e.type == "widget" and e.data["widget"]["kind"] == "order_card"]
    return events, session, cards


def stored(store, role):
    return [r["content"] for r in store.rows if r["role"] == role]


def test_retried_turn_finds_the_failed_request_and_its_waiting_cards():
    card = {"kind": "order_card", "instrument_id": "btcusdt.binance", "side": "buy", "status": "PENDING_APPROVAL"}
    done = {**card, "side": "SELL", "status": "REJECTED"}
    turns = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"},
             {"role": "user", "content": ASK}, {"role": "assistant", "content": "partial", "widgets": [card, done]}]
    assert retried_turn(turns, f" {ASK} ") == (2, {("BTCUSDT.BINANCE", "BUY")})
    assert retried_turn(turns, "something else") is None  # a different text is a new message
    assert retried_turn([], ASK) is None


async def test_a_retry_after_a_failed_turn_stores_the_request_once_and_answers():
    brain, gateway, store = brain_with(GatewayError("connection reset"), card_events())
    failed, sid, cards = await turn(brain, ASK)
    assert failed[-1].type == "error" and cards == [] and stored(store, "user") == [ASK]
    assert failed[-1].data["session_id"] == sid  # the failure names the conversation it opened: Retry continues it

    events, _, [card] = await turn(brain, ASK, sid, retry=True)
    assert events[-1].type == "done" and card["status"] == "PENDING_APPROVAL"
    assert stored(store, "user") == [ASK]  # no second copy of the request
    assert gateway.prompts[-1].count(ASK) == 1  # the model reads it once: as the new message, not also as history


async def test_a_retry_does_not_stage_a_second_card_for_an_idea_the_failed_turn_carded():
    partial = [*card_events()[:3], GatewayError("connection reset")]  # the card is staged, then the stream dies
    brain, _, store = brain_with(partial, card_events(), card_events({**ORDER, "side": "SELL", "stop_loss": 62000}))

    async def held(_instrument):
        return Decimal("1")
    brain._holdings = held

    failed, sid, first = await turn(brain, ASK)
    assert failed[-1].type == "error" and len(first) == 1

    events, _, again = await turn(brain, ASK, sid, retry=True)
    assert again == [] and "No second order card for BTCUSDT.BINANCE BUY" in events[-1].data["content"]
    assert stored(store, "user") == [ASK]
    waiting = [w for r in store.rows for w in r["widgets"] if w.get("kind") == "order_card"]
    assert len(waiting) == 1  # one card in the whole conversation

    # A different idea in a later retry is not blocked by the first card
    _, _, other = await turn(brain, ASK, sid, retry=True)
    assert [c["side"] for c in other] == ["SELL"]


async def test_a_retry_flag_on_a_new_text_is_an_ordinary_message():
    brain, _, store = brain_with(GatewayError("connection reset"), card_events())
    _, sid, _ = await turn(brain, ASK)
    _, _, [card] = await turn(brain, "Buy BTC now instead", sid, retry=True)
    assert stored(store, "user") == [ASK, "Buy BTC now instead"] and card["side"] == "BUY"
