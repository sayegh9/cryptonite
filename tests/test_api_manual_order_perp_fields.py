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
    [{"leverage": 0}, {"leverage": 21}, {"margin_mode": "PORTFOLIO"}],
)
def test_out_of_range_perp_fields_are_rejected_at_the_api(fields):
    with pytest.raises(ValidationError):
        paper.ManualOrderRequest(instrument_id="BTCUSDT-PERP.BINANCE", side="BUY", quantity=0.01, **fields)
