"""Stage 3.3 (leverage plan): an entry card carries the system's setup grade, set by the Brain from the grader alone
(never from the model), next to the model's confidence; a failed or slow grader leaves the card ungraded."""

from dataclasses import replace

from cryptonite_agents.brain import agent as agent_mod
from cryptonite_agents.brain.agent import BRAIN, Brain
from cryptonite_agents.brain.store import MemorySessionStore
from cryptonite_agents.brain.toolset import propose_order, show_price_chart
from cryptonite_agents.brain.widgets import widget_for_tool
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from test_brain import GOOD_ORDER, PLANNER, Script, collect, gw
from test_brain_lessons import _cards, _scripted_card, _ScriptedGateway  # noqa: F401

ORDER = {**GOOD_ORDER, "take_profit": 66000, "confidence": 0.6}
GRADED = {"grade": "B", "reasons": ["2 of 3 timeframes aligned (4h, 1h)"], "inputs": {"side": "LONG"},
          "version": "setup-grade-v1"}


def _brain(grader=None) -> Brain:
    persona = replace(BRAIN, tools=(show_price_chart, propose_order))
    return Brain(gw(), MemorySessionStore(), persona, grader=grader)


async def _turn(brain, order):
    with override_model(PLANNER, Script([("propose_order", order)], ["Here is the card."]).model()):
        return _cards(await collect(brain.turn("op-1", "Trade it")))


async def test_an_entry_card_carries_the_system_grade_and_the_models_confidence():
    seen = []

    async def grader(card):
        seen.append(card)
        return GRADED

    [card] = await _turn(_brain(grader), ORDER)
    assert (card["setup_grade"], card["grade_reasons"], card["grade_version"]) == (
        "B", GRADED["reasons"], "setup-grade-v1")
    assert card["grade_inputs"] == {"side": "LONG"}
    assert card["confidence"] == 0.6  # stored next to it, never used for leverage
    assert seen[0]["stop_loss"] == 58000 and seen[0]["take_profit"] == 66000


async def test_a_card_without_a_target_is_not_graded():
    async def grader(card):
        return GRADED

    [card] = await _turn(_brain(grader), GOOD_ORDER)  # no take_profit
    assert card["setup_grade"] is None


async def test_a_failing_or_slow_grader_leaves_the_card_ungraded(monkeypatch):
    async def boom(card):
        raise RuntimeError("binance down")

    [card] = await _turn(_brain(boom), ORDER)
    assert card["setup_grade"] is None and card["grade_reasons"] == []

    import asyncio

    monkeypatch.setattr(agent_mod, "GRADE_TIMEOUT_S", 0.05)

    async def slow(card):
        await asyncio.sleep(1)
        return GRADED

    [card] = await _turn(_brain(slow), ORDER)
    assert card["setup_grade"] is None


def test_a_model_supplied_grade_never_survives():
    card = widget_for_tool("propose_order", {**ORDER, "setup_grade": "A", "grade_reasons": ["trust me"],
                                             "grade_inputs": {"x": 1}}, call_id="c1")
    assert card["setup_grade"] is None and card["grade_reasons"] == [] and card["grade_inputs"] is None


async def test_no_trade_leveraged_entry_is_not_staged_while_spot_and_grade_c_perp_are():
    perp_short = {**ORDER, "instrument_id": "XRPUSDT-PERP.BINANCE", "side": "SELL",
                  "stop_loss": 0.45, "take_profit": 0.40}
    margin_long = {**ORDER, "instrument_id": "XRPUSDT-MARGIN.BINANCE", "side": "BUY",
                   "stop_loss": 0.40, "take_profit": 0.45}
    no_trade = {"grade": "NO_TRADE", "reasons": ["reward 1.07x the risk, below 1.25x"],
                "inputs": {"side": "SHORT", "entry": 0.4241}, "version": "setup-grade-v2"}
    grade_c = {"grade": "C", "reasons": ["reward 2.00x the risk"],
               "inputs": {"side": "SHORT", "entry": 0.4335}, "version": "setup-grade-v2"}

    async def grade_no_trade(card):
        return no_trade

    async def grade_c_fn(card):
        return grade_c

    async def no_debate(card):
        return {"against": True, "run_id": "12345678", "created_at": "2026-11-15T18:00Z", "recommendation": "HOLD"}

    async def bad_feed(inst):
        return "SYMBOL_NOT_INGESTED"

    # 1. Perp NO_TRADE is suppressed (no card emitted, no orphan debate/feed notes), reply states NO_TRADE and entry
    b_no_trade = Brain(gw(), MemorySessionStore(), replace(BRAIN, tools=(show_price_chart, propose_order)),
                       grader=grade_no_trade, debates=no_debate, feed=bad_feed)
    with override_model(PLANNER, Script([("propose_order", perp_short)], ["Staging short."]).model()):
        evs = await collect(b_no_trade.turn("op-1", "Short XRP perp"))
    assert _cards(evs) == []
    done = [e.data for e in evs if e.type == "done"][-1]
    assert "was not staged: setup grade is NO_TRADE" in done["content"]
    assert "at 0.4241" in done["content"]
    assert "0x leverage ceiling" in done["content"]
    assert "against the debate verdict" not in done["content"]
    assert "not in the live data feed" not in done["content"]

    # 2. Margin NO_TRADE is also suppressed
    with override_model(PLANNER, Script([("propose_order", margin_long)], ["Staging margin."]).model()):
        evs_margin = await collect(b_no_trade.turn("op-2", "Long XRP margin"))
    assert _cards(evs_margin) == []

    # 3. Perp Grade C IS still staged
    b_grade_c = Brain(gw(), MemorySessionStore(), replace(BRAIN, tools=(show_price_chart, propose_order)),
                      grader=grade_c_fn)
    with override_model(PLANNER, Script([("propose_order", perp_short)], ["Staging short."]).model()):
        assert len(_cards(await collect(b_grade_c.turn("op-3", "Short XRP perp")))) == 1

    # 4. Spot NO_TRADE remains staged (half cap contract)
    with override_model(PLANNER, Script([("propose_order", ORDER)], ["Staging spot."]).model()):
        [spot_card] = _cards(await collect(b_no_trade.turn("op-4", "Buy BTC spot")))
    assert spot_card["setup_grade"] == "NO_TRADE"
