"""Approval text for perp entries: ``render.perp_lines`` and the lines ``render.approval_text`` adds for them.

Pure rendering: no DB, no network, no Telegram. The clock is fixed so the countdown is deterministic.
"""

import datetime
import uuid

from cryptonite_notifier import render

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)
SUMMARY = "LONG BTCUSDT-PERP.BINANCE 0.02 @ 61000.0"
ROW = {"title": "Brain BTCUSDT-PERP.BINANCE (L1)", "body": ""}
REASONS = ["4h, 1h and 15m aligned", "reward 2.0x the risk", "liquidity unknown"]


def _perp_plan(**overrides):
    plan = {
        "side": "BUY",
        "leverage": 3,
        "binding_ceiling": "mandate maximum",
        "liquidation_price": "40312.5",
        "loss_at_stop_usd": "41.25",
        "loss_at_stop_pct": "0.4125",
    }
    plan.update(overrides)
    return plan


def _long_payload(**overrides):
    payload = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "side": "BUY",
        "perp_side": "LONG",
        "stop_loss": 57000.0,
        "take_profit": 66000.0,
        "setup_grade": "B",
        "grade": {"grade": "B", "reasons": list(REASONS)},
        "grade_reasons": list(REASONS),
        "perp_plan": _perp_plan(),
    }
    payload.update(overrides)
    return payload


def _approval(action_class="ADD_RISK", payload=..., summary=SUMMARY):
    approval = {
        "id": uuid.uuid4(),
        "action_class": action_class,
        "timeout_policy": "DENY",
        "expires_at": NOW + datetime.timedelta(minutes=10),
        "summary": summary,
        "trace_id": None,
    }
    if payload is not ...:
        approval["payload"] = payload
    return approval


def test_a_perp_long_entry_shows_side_stop_grade_leverage_liquidation_and_loss():
    text = render.approval_text(ROW, _approval(payload=_long_payload()), NOW)

    assert SUMMARY in text
    for expected in (
        "Perp LONG",
        "ISOLATED",
        "stop 57000.0",
        "target 66000.0",
        "Grade B",
        "liquidity unknown",
        "3x (binding: mandate maximum)",
        "Liquidation 40,312.50",
        "loss at stop $41.25 (0.41% of budget)",
        "needs your TOTP code",
    ):
        assert expected in text, expected


def test_a_perp_short_entry_is_labelled_short():
    payload = _long_payload(perp_side="SHORT", side="SELL")
    payload["perp_plan"] = _perp_plan(side="SELL")

    text = render.approval_text(
        ROW, _approval(payload=payload, summary="SHORT BTCUSDT-PERP.BINANCE 0.02 @ 61000.0"), NOW
    )

    assert "Perp SHORT" in text
    assert "Perp LONG" not in text


def test_a_missing_perp_side_falls_back_to_the_plan_side():
    payload = _long_payload()
    del payload["perp_side"]
    payload["perp_plan"] = _perp_plan(side="SELL")

    lines = render.perp_lines(payload)

    assert lines[0].startswith("Perp SHORT ·")
    assert "Perp SHORT" in render.approval_text(ROW, _approval(payload=payload), NOW)


def test_a_spot_entry_and_an_approval_without_payload_get_no_perp_lines():
    spot_payload = {"instrument_id": "BTCUSDT.BINANCE", "side": "BUY", "quantity": "0.02"}

    assert render.perp_lines(spot_payload) == []
    spot_text = render.approval_text(
        ROW, _approval(payload=spot_payload, summary="BUY BTCUSDT.BINANCE 0.02"), NOW
    )
    assert "Perp " not in spot_text
    assert "BUY BTCUSDT.BINANCE 0.02" in spot_text

    no_payload_text = render.approval_text(ROW, _approval(summary="BUY BTCUSDT.BINANCE 0.02"), NOW)
    assert render.perp_lines({}) == []
    assert "Perp " not in no_payload_text
    assert "BUY BTCUSDT.BINANCE 0.02" in no_payload_text


def test_missing_liquidation_and_loss_percent_render_as_none_and_are_left_out():
    payload = _long_payload()
    payload["perp_plan"] = _perp_plan(liquidation_price=None, loss_at_stop_pct=None)

    text = render.approval_text(ROW, _approval(payload=payload), NOW)

    assert "Liquidation none" in text
    assert "loss at stop $41.25" in text
    assert "% of budget" not in text


def test_a_reduce_risk_approval_with_no_perp_plan_gets_the_one_tap_line_only():
    payload = {
        "instrument_id": "BTCUSDT-PERP.BINANCE",
        "side": "SELL",
        "perp_side": "LONG",
        "reduce_only": True,
    }

    assert render.perp_lines(payload) == []
    text = render.approval_text(
        ROW,
        _approval(
            action_class="REDUCE_RISK", payload=payload, summary="SELL BTCUSDT-PERP.BINANCE 0.02"
        ),
        NOW,
    )

    assert "Risk reducing: one tap." in text
    assert "Perp " not in text
    assert "TOTP" not in text
