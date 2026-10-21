"""Stages 2.3 and 2.4 (leverage plan): the dispatcher's perp entry checks (STOP_REQUIRED, RISK_AT_STOP_EXCEEDED,
LIQUIDATION_BUFFER_TOO_THIN, LEVERAGE_ABOVE_BRACKET, the 10x account maximum), and the orchestrator sizing perp
entries in either direction with plan_leveraged_order; the mandate gate and the dispatcher check them with the same
valuation, so every system-sized entry they refuse is refused before it is placed, never half-way."""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptonite_orchestrator.server import SimpleQuoteCache, command_poller
from cryptonite_schemas.mandate import Mandate
from test_ai_mandates import FakeConn, mandate_row
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import (
    PERP,
    make_fresh_dispatcher,
    make_perp_dispatcher,
    perp_intent,
)

D = Decimal


# ------------------------------------------------------------------------------------------ dispatcher (2.3)

def test_ai_entry_without_a_stop_is_refused():
    d = make_perp_dispatcher()
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.01", leverage=2, require_stop=True))
    assert not ok and reason.startswith("STOP_REQUIRED")
    assert d.evaluate_intent(perp_intent("BUY", "0.01", leverage=2, require_stop=True, stop_loss=D("48000")))[0]


def test_stop_inside_half_the_liquidation_distance():
    d = make_perp_dispatcher()
    # 10x: liquidation ~9.6% away; a 5% stop + 0.3% slippage is more than half of it
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.04", leverage=10, stop_loss=D("47500")))
    assert not ok and reason.startswith("LIQUIDATION_BUFFER_TOO_THIN")
    assert d.evaluate_intent(perp_intent("BUY", "0.04", leverage=5, stop_loss=D("47500")))[0]


def test_risk_at_stop_against_the_mandate_budget_and_the_manual_default():
    d = make_perp_dispatcher()
    # 0.04 BTC (2,000 notional), 5% stop: loss 2,000 x 5.4% = 108
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.04", leverage=5, stop_loss=D("47500"), risk_budget_usd=D("100")))
    assert not ok and reason.startswith("RISK_AT_STOP_EXCEEDED")
    assert d.evaluate_intent(perp_intent("BUY", "0.04", leverage=5, stop_loss=D("47500"), risk_budget_usd=D("110")))[0]
    # manual with a stop: 2% of the 10,000 equity = 200
    assert d.evaluate_intent(perp_intent("BUY", "0.04", leverage=5, stop_loss=D("47500")))[0]


def test_manual_entry_without_a_stop_is_unchanged():
    d = make_perp_dispatcher()
    assert d.evaluate_intent(perp_intent("BUY", "0.04", leverage=10))[:2] == (True, "APPROVED")


def test_leverage_above_the_bracket_for_the_notional():
    d, _ = make_fresh_dispatcher(starting_cash=D("100000000"))
    d.update_mark_price(PERP, D("50000"))
    # 400 BTC at 50,000 = 20m notional: the BTC tier from 20m allows 5x
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "400", leverage=10))
    assert not ok and reason.startswith("LEVERAGE_ABOVE_BRACKET")
    assert d.evaluate_intent(perp_intent("BUY", "400", leverage=5))[0]


def test_stop_on_the_wrong_side_is_refused():
    d = make_perp_dispatcher()
    ok, reason, _ = d.evaluate_intent(perp_intent("SELL", "0.01", leverage=2, stop_loss=D("48000")))
    assert not ok and reason.startswith("STOP_WRONG_SIDE")


def test_account_maximum_is_10x():
    d = make_perp_dispatcher()
    assert d.max_perp_leverage == 10
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.001", leverage=11))
    assert not ok and reason.startswith("LEVERAGE_OUT_OF_RANGE")


def test_spot_is_untouched_by_the_perp_checks():
    d, _ = make_fresh_dispatcher()
    spot = perp_intent("BUY", "0.01", "S-1")
    spot.instrument_id = "BTCUSDT.BINANCE"
    assert d.evaluate_intent(spot)[:2] == (True, "APPROVED")


# --------------------------------------------------------------------------------- orchestrator sizing (2.4)

def perp_mandate(**kw) -> Mandate:
    base = dict(id=uuid.UUID(int=48), name="perp-l1", capital_budget_usd=D("10000"), instruments=["BTCUSDT-PERP"],
                max_position_pct=D("25"), allow_perps=True, max_leverage=10)
    base.update(kw)
    return Mandate(**base)


async def run_perp_command(payload, m: Mandate, *, approved=True):
    engine = make_engine(make_perp_dispatcher())
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)
    cid = uuid.uuid4()
    body = {"instrument_id": "BTCUSDT-PERP", "mandate_id": str(m.id), "approval_id": str(uuid.uuid4()), **payload}
    row = {"command_id": cid, "command_type": "SUBMIT_ORDER", "target_instance_id": "AI_DEBATE_WORKER",
           "requester_id": "operator:u1", "mandate_id": str(m.id), "payload": json.dumps(body)}
    approval = {"mandate_id": m.id, "action_class": "ADD_RISK", "status": "APPROVED", "command_id": cid} if approved else None
    conn = FakeConn(row, mandate_row(m), (), approval)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quotes)
    return conn, engine


async def test_worked_example_long_is_sized_to_the_risk_ceiling_and_gets_its_stop():
    m = perp_mandate()
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "stop_loss": "47500",
                                           "setup_grade": "A"}, m)
    assert conn.outcome() == ("COMPLETED", None)
    d = engine.dispatcher
    pos = d.perp_positions[PERP]
    assert (pos.leverage, pos.margin_mode, pos.instance_id) == (7, "ISOLATED", m.instance_id)
    assert pos.signed_qty == D("0.035")  # $250 margin x 7 = $1,750 at the 50,000 mark
    (stop,) = d.resting_stops(PERP).values()
    assert (stop["side"], stop["intent"].stop_price, stop["quantity"]) == ("SELL", D("47500"), D("0.035"))


async def test_a_short_entry_is_sized_too():
    m = perp_mandate()
    conn, engine = await run_perp_command({"side": "SELL", "allocation_pct": 2.5, "stop_loss": "52500",
                                           "setup_grade": "A"}, m)
    assert conn.outcome() == ("COMPLETED", None)
    pos = engine.dispatcher.perp_positions[PERP]
    assert pos.signed_qty == D("-0.035") and pos.leverage == 7
    assert list(engine.dispatcher.resting_stops(PERP).values())[0]["side"] == "BUY"


async def test_grade_and_requested_leverage_bind():
    m = perp_mandate()
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "stop_loss": "49000",
                                           "setup_grade": "B"}, m)
    assert conn.outcome() == ("COMPLETED", None) and engine.dispatcher.perp_positions[PERP].leverage == 5
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "stop_loss": "49000",
                                           "setup_grade": "A", "leverage": 3}, m)
    assert engine.dispatcher.perp_positions[PERP].leverage == 3
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "stop_loss": "49000"}, m)
    assert engine.dispatcher.perp_positions[PERP].leverage == 2  # ungraded: grade C's ceiling


async def test_mandate_maximum_binds():
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "stop_loss": "49000",
                                           "setup_grade": "A"}, perp_mandate(max_leverage=3))
    assert engine.dispatcher.perp_positions[PERP].leverage == 3


async def test_entry_without_a_stop_is_never_placed():
    conn, engine = await run_perp_command({"side": "BUY", "allocation_pct": 2.5, "setup_grade": "A"}, perp_mandate())
    status, reason = conn.outcome()
    assert status == "REJECTED" and reason.startswith("STOP_REQUIRED")
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.perp_positions == {}


async def test_explicit_oversized_ai_order_is_refused_by_the_gate_not_the_dispatcher():
    conn, engine = await run_perp_command({"side": "BUY", "quantity": "0.04", "leverage": 10, "stop_loss": "47500",
                                           "setup_grade": "A"}, perp_mandate())
    assert conn.outcome() == ("REJECTED", "LIQUIDATION_BUFFER_TOO_THIN")
    assert engine.dispatcher.open_orders == {}


async def test_explicit_quantity_ai_entry_is_held_to_its_grade_ceiling():
    # not re-sized, but never above the grade's ceiling (ungraded = grade C, 2x)
    conn, engine = await run_perp_command({"side": "BUY", "quantity": "0.004", "leverage": 5, "stop_loss": "49000"},
                                          perp_mandate())
    status, reason = conn.outcome()
    assert status == "REJECTED" and reason.startswith("LEVERAGE_ABOVE_GRADE_CEILING")
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.perp_positions == {}


@pytest.mark.parametrize("alloc", ["0.5", "2.5", "10", "25"])
@pytest.mark.parametrize("stop_pct", ["0.005", "0.02", "0.05", "0.09", "0.2"])
@pytest.mark.parametrize("grade,lev", [("A", None), ("B", 10), ("C", 1), ("A", 6)])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_the_gate_and_the_dispatcher_accept_every_sizing_the_plan_makes(alloc, stop_pct, grade, lev, side):
    """Sizing and checking value the order alike: a system-sized entry is placed, or refused by the plan itself
    with its own reason before anything reaches the gate or the dispatcher; never refused afterwards."""
    stop = D("50000") * (1 - D(stop_pct)) if side == "BUY" else D("50000") * (1 + D(stop_pct))
    payload = {"side": side, "allocation_pct": alloc, "stop_loss": str(stop), "setup_grade": grade}
    if lev:
        payload["leverage"] = lev
    m = perp_mandate(max_risk_per_trade_pct=D("2"))
    conn, engine = await run_perp_command(payload, m)
    status, reason = conn.outcome()
    if status == "COMPLETED":
        pos = engine.dispatcher.perp_positions[PERP]
        assert 1 <= pos.leverage <= 10
        assert abs(pos.signed_qty) * D("50000") <= D("2500")  # the 25% position limit and 25% of equity
        return
    # refused only by the plan (nothing placed), never by the mandate gate or the dispatcher
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.perp_positions == {}
    assert "perp sizing" in reason, reason
    assert reason.split(":")[0] in ("RISK_AT_STOP_EXCEEDED", "LIQUIDATION_BUFFER_TOO_THIN", "EXPOSURE_LIMIT_REACHED",
                                    "ORDER_BELOW_MIN_NOTIONAL"), reason
