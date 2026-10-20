"""Stage 1.6 (leverage plan): perp orders default to ISOLATED (mandate perps are ISOLATED only, ADR 0009 D5), the
margin mode is locked while a position is open, and paper perps pay the USD-M taker fee (0.05%); spot keeps 0.10%."""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_orchestrator.server import SimpleQuoteCache, fee_rate_for
from cryptonite_schemas.mandate import BREACH_CODES, AutonomyLevel, Mandate, MandateReject
from test_orchestrator_perp_wiring import command_outcomes, make_engine, run_commands
from test_paper_risk_guardrails import (
    PERP,
    make_fresh_dispatcher,
    make_perp_dispatcher,
    perp_intent,
)

D = Decimal


def perp_mandate() -> Mandate:
    return Mandate(id=uuid.UUID(int=7), name="perps", capital_budget_usd=D("10000"), instruments=["BTCUSDT-PERP"],
                   max_position_pct=D("25"), allow_perps=True, max_leverage=5, autonomy_level=AutonomyLevel.L3)


def perp_order(**kw) -> MandateOrder:
    return MandateOrder(instrument_id=PERP, side="SELL", quantity=D("0.01"), ref_price=D("50000"),
                        order_type="LIMIT", leverage=3, **kw)


def test_default_margin_mode_is_isolated_without_a_position():
    d = make_perp_dispatcher()
    intent = perp_intent("SELL", "0.01")
    assert d.evaluate_intent(intent)[0] and intent.margin_mode == "ISOLATED"


def test_default_follows_the_open_positions_mode():
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.01"), D("50000"), leverage=2, margin_mode="CROSS", instance_id="INST-001")
    intent = perp_intent("BUY", "0.01", leverage=2)
    assert d.evaluate_intent(intent)[0] and intent.margin_mode == "CROSS"


def test_margin_mode_is_locked_while_a_position_is_open():
    d = make_perp_dispatcher()
    d.set_perp_position(PERP, D("0.01"), D("50000"), leverage=2, margin_mode="ISOLATED", instance_id="INST-001")
    ok, reason, _ = d.evaluate_intent(perp_intent("BUY", "0.01", leverage=2, margin_mode="CROSS"))
    assert not ok and reason.startswith("MARGIN_MODE_LOCKED")
    # a reduce-only exit is never refused for it
    assert d.evaluate_intent(perp_intent("SELL", "0.01", reduce_only=True, margin_mode="CROSS"))[0]


def test_mandate_check_refuses_cross_and_it_is_not_a_breach():
    m = perp_mandate()
    assert check_mandate(m, perp_order(margin_mode="CROSS"), MandateExposure(), approval_ok=True) \
        is MandateReject.MARGIN_MODE_NOT_ALLOWED
    assert MandateReject.MARGIN_MODE_NOT_ALLOWED not in BREACH_CODES
    assert check_mandate(m, perp_order(margin_mode="ISOLATED"), MandateExposure(), approval_ok=True) is None
    assert check_mandate(m, perp_order(), MandateExposure(), approval_ok=True) is None  # None = the ISOLATED default


def test_perp_fill_fee_is_the_usdm_rate_and_spot_is_unchanged():
    d = make_perp_dispatcher()
    assert d.perp_taker_fee == D("0.0005") and d.taker_fee == D("0.001")
    assert fee_rate_for(d, PERP) == D("0.0005")
    assert fee_rate_for(d, "BTCUSDT.BINANCE") == D("0.001")
    spot, _ = make_fresh_dispatcher()
    intent = perp_intent("BUY", "0.01", "SPOT-1")
    intent.instrument_id = "BTCUSDT.BINANCE"
    # spot reserve: 0.01 * 50,010 * (1 + 2% + 0.5%) * (1 + 0.1%) as before
    assert spot.evaluate_intent(intent)[2] == D("0.01") * D("50010") * D("1.025") * D("1.001")


async def test_orchestrator_books_the_usdm_fee_on_a_perp_fill():
    engine = make_engine()
    engine.bridge = MagicMock()
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)
    conn, _ = await run_commands(engine, [
        {"client_order_id": "F-1", "instrument_id": "BTCUSDT-PERP", "side": "SELL", "quantity": "0.04", "leverage": 5},
    ], quotes)
    assert command_outcomes(conn) == [("COMPLETED", None)]
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert D(evt["commission"]) == D("0.04") * D("50000") * D("0.0005")
    assert evt["position"]["margin_mode"] == "ISOLATED"
