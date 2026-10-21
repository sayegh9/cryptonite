"""POST /paper/orders forwards perpetual order attributes into the MANUAL_ORDER command payload."""

from unittest.mock import MagicMock

import pytest
from cryptonite_api.routes import paper
from pydantic import ValidationError


async def submit(monkeypatch, **fields):
    captured = {}

    async def fake_enqueue(**kwargs):
        captured.update(kwargs)
        return {"command_id": "cmd-1"}

    monkeypatch.setattr(paper, "_enqueue_command", fake_enqueue)
    req = paper.ManualOrderRequest(instrument_id="BTCUSDT-PERP.BINANCE", side="SELL", quantity=0.04, **fields)
    await paper.submit_manual_order(req, request=MagicMock(), auth={}, csrf={})
    return captured["payload"]


async def test_perp_fields_reach_command_payload(monkeypatch):
    payload = await submit(monkeypatch, leverage=10, margin_mode="ISOLATED", reduce_only=True)
    assert (payload["leverage"], payload["margin_mode"], payload["reduce_only"]) == (10, "ISOLATED", True)
    assert payload["instrument_id"] == "BTCUSDT-PERP.BINANCE"


async def test_spot_style_order_sends_neutral_defaults(monkeypatch):
    payload = await submit(monkeypatch)
    # None/False let the dispatcher apply its own defaults (leverage 1, CROSS) and keep spot valid
    assert (payload["leverage"], payload["margin_mode"], payload["reduce_only"]) == (None, None, False)


@pytest.mark.parametrize(
    "fields",
    [{"leverage": 0}, {"leverage": 11}, {"leverage": 21}, {"margin_mode": "PORTFOLIO"}, {"stop_loss": 0}],
)
def test_out_of_range_perp_fields_are_rejected_at_the_api(fields):
    with pytest.raises(ValidationError):
        paper.ManualOrderRequest(instrument_id="BTCUSDT-PERP.BINANCE", side="BUY", quantity=0.01, **fields)


async def test_an_attached_stop_reaches_the_payload(monkeypatch):
    """Leverage plan 4.5: the dispatcher checks the stop and rests it on the mark once the entry fills."""
    payload = await submit(monkeypatch, leverage=5, margin_mode="ISOLATED", stop_loss=63000)
    assert payload["stop_loss"] == "63000.0"


@pytest.mark.parametrize("fields", [{"instrument_id": "BTCUSDT.BINANCE"}, {"reduce_only": True}])
async def test_a_stop_is_only_for_a_perp_entry(monkeypatch, fields):
    async def fake_enqueue(**kwargs):
        raise AssertionError("must not queue")

    monkeypatch.setattr(paper, "_enqueue_command", fake_enqueue)
    req = paper.ManualOrderRequest(**{"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "SELL", "quantity": 0.04,
                                      "stop_loss": 63000, **fields})
    with pytest.raises(paper.HTTPException) as e:
        await paper.submit_manual_order(req, request=MagicMock(), auth={}, csrf={})
    assert e.value.status_code == 422
