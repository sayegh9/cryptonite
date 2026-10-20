"""Stage 1.1 (leverage plan): the USD-M adapter's order flags, leverage / margin type, hedge-mode refusal,
position risk and suffix routing. Every HTTP call goes to an ``httpx.MockTransport``; nothing reaches Binance."""

import asyncio
import urllib.parse
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptonite_execution.binance_adapter import (
    BinanceCompositeAdapter,
    BinanceFuturesAdapter,
    HedgeModeError,
    futures_symbol,
    is_perp_symbol,
)

PEM = ed25519.Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)


def _mocked(adapter, dual: bool = False, responses: dict | None = None):
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        calls.append((request.method, request.url.path, params))
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": dual})
        body = (responses or {}).get(request.url.path)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body if body is not None else {"ok": True})

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return calls


def _orders(calls):
    return [p for m, path, p in calls if path == "/fapi/v1/order" and m == "POST"]


def test_routing_is_by_perp_suffix_not_a_string_guess():
    assert is_perp_symbol("BTCUSDT-PERP.BINANCE") and is_perp_symbol("ethusdt-perp")
    # the old guess sent "BTCUSDT_PERP"-style and any "PERP"-containing name to futures
    assert not is_perp_symbol("BTCUSDT") and not is_perp_symbol("BTCUSDT.BINANCE")
    assert not is_perp_symbol("PERPUSDT")
    assert futures_symbol("BTCUSDT-PERP.BINANCE") == "BTCUSDT"
    c = BinanceCompositeAdapter("k", PEM, "https://spot.test", "https://fut.test")
    assert c.get_adapter("XRPUSDT-PERP.BINANCE") is c.futures
    assert c.get_adapter("XRPUSDT.BINANCE") is c.spot
    assert c.get_adapter("PERPUSDT") is c.spot


def test_stop_market_sends_stop_price_on_mark_and_reduce_only():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a)
    asyncio.run(a.submit_order("BTCUSDT-PERP.BINANCE", "SELL", "STOP_MARKET", Decimal("0.01"), None, "SL-1",
                               reduce_only=True, stop_price=Decimal("58000")))
    (p,) = _orders(calls)
    assert p["symbol"] == "BTCUSDT" and p["type"] == "STOP_MARKET"
    assert p["stopPrice"] == "58000" and p["workingType"] == "MARK_PRICE"
    assert p["reduceOnly"] == "true" and p["quantity"] == "0.01"
    assert "signature" in p and "timestamp" in p


def test_close_position_take_profit_has_no_quantity_and_no_reduce_only():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a)
    asyncio.run(a.submit_order("ETHUSDT-PERP", "BUY", "TAKE_PROFIT_MARKET", None, None, "TP-1",
                               stop_price=Decimal("2000"), close_position=True, reduce_only=True))
    (p,) = _orders(calls)
    assert p["closePosition"] == "true" and "quantity" not in p and "reduceOnly" not in p
    assert p["stopPrice"] == "2000" and p["workingType"] == "MARK_PRICE"


@pytest.mark.parametrize("kwargs", [
    {"order_type": "STOP_MARKET", "stop_price": None},
    {"order_type": "MARKET", "close_position": True, "stop_price": Decimal("1")},
    {"order_type": "MARKET", "quantity": None},
])
def test_invalid_futures_orders_are_refused_before_any_request(kwargs):
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a)
    args = {"quantity": Decimal("1"), **kwargs}
    with pytest.raises(ValueError):
        asyncio.run(a.submit_order("BTCUSDT-PERP", "BUY", args.pop("order_type"), args.pop("quantity"), None, "X", **args))
    assert _orders(calls) == []


def test_hedge_mode_account_refuses_every_order():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a, dual=True)
    with pytest.raises(HedgeModeError):
        asyncio.run(a.submit_order("BTCUSDT-PERP", "BUY", "MARKET", Decimal("0.01"), None, "E-1"))
    assert _orders(calls) == []


def test_one_way_mode_is_checked_once():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a)
    for i in range(3):
        asyncio.run(a.submit_order("BTCUSDT-PERP", "BUY", "MARKET", Decimal("0.01"), None, f"E-{i}"))
    assert sum(1 for _, path, _ in calls if path == "/fapi/v1/positionSide/dual") == 1
    assert len(_orders(calls)) == 3


def test_set_leverage_and_margin_type():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a, responses={"/fapi/v1/marginType": httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})})
    asyncio.run(a.set_leverage("BTCUSDT-PERP.BINANCE", 7))
    res = asyncio.run(a.set_margin_type("BTCUSDT-PERP.BINANCE", "ISOLATED"))
    assert res["code"] == -4046  # already ISOLATED is success
    lev = [p for _, path, p in calls if path == "/fapi/v1/leverage"][0]
    mt = [p for _, path, p in calls if path == "/fapi/v1/marginType"][0]
    assert lev["symbol"] == "BTCUSDT" and lev["leverage"] == "7"
    assert mt["marginType"] == "ISOLATED"
    asyncio.run(a.set_margin_type("BTCUSDT-PERP", "CROSS"))
    assert [p for _, path, p in calls if path == "/fapi/v1/marginType"][-1]["marginType"] == "CROSSED"


@pytest.mark.parametrize("lev", [0, 11, 20, 125])
def test_set_leverage_refuses_above_the_account_cap(lev):
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    calls = _mocked(a)
    with pytest.raises(ValueError):
        asyncio.run(a.set_leverage("BTCUSDT-PERP", lev))
    assert calls == []


def test_position_risk_read():
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    row = {"symbol": "BTCUSDT", "positionAmt": "-0.010", "entryPrice": "60000", "leverage": "5",
           "marginType": "isolated", "liquidationPrice": "71000", "positionSide": "BOTH"}
    calls = _mocked(a, responses={"/fapi/v2/positionRisk": [row]})
    assert asyncio.run(a.get_position_risk("BTCUSDT-PERP.BINANCE")) == [row]
    assert calls[-1][1] == "/fapi/v2/positionRisk" and calls[-1][2]["symbol"] == "BTCUSDT"


def test_composite_refuses_futures_flags_on_spot():
    c = BinanceCompositeAdapter("k", PEM, "https://spot.test", "https://fut.test")
    with pytest.raises(ValueError):
        asyncio.run(c.submit_order("BTCUSDT", "SELL", "MARKET", Decimal("1"), None, "S-1", reduce_only=True))


def test_composite_routes_perp_orders_with_flags_to_futures():
    c = BinanceCompositeAdapter("k", PEM, "https://spot.test", "https://fut.test")
    calls = _mocked(c.futures)
    asyncio.run(c.submit_order("BTCUSDT-PERP.BINANCE", "SELL", "MARKET", Decimal("0.5"), None, "R-1", reduce_only=True))
    (p,) = _orders(calls)
    assert p["reduceOnly"] == "true" and p["symbol"] == "BTCUSDT"
