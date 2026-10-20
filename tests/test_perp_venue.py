"""Leverage plan Stage 6: the executor's perp venue steps (cryptonite_execution.perp_venue) against a stateful mock
``/fapi`` server: call order, set-only-when-different, Binance error codes, the stop-or-close rule, reconciliation and
the exchange-side flatten. Nothing reaches Binance."""

import asyncio
import urllib.parse
from decimal import Decimal as D

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptonite_execution.binance_adapter import BinanceFuturesAdapter, HedgeModeError
from cryptonite_execution.perp_venue import (
    Mismatch,
    PerpVenueError,
    flatten_all,
    prepare_entry,
    protect_position,
    reconcile,
)

PEM = ed25519.Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
)
PERP = "BTCUSDT-PERP.BINANCE"


class FakeFapi:
    """A small stateful USD-M account: per-symbol margin type, leverage, position; open orders."""

    def __init__(self, margin="cross", leverage=20, position="0", dual=False, stop_error=None, ignore_leverage=False):
        self.state = {"BTCUSDT": {"marginType": margin, "leverage": str(leverage), "positionAmt": position}}
        self.dual, self.stop_error, self.ignore_leverage = dual, stop_error, ignore_leverage
        self.open_orders = [{"symbol": "BTCUSDT", "clientOrderId": "STOP-1"}]
        self.calls: list[tuple[str, str, dict]] = []

    def _err(self, code: int, msg: str) -> httpx.Response:
        return httpx.Response(400, json={"code": code, "msg": msg})

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        p.pop("timestamp", None), p.pop("signature", None)
        path, method = request.url.path, request.method
        self.calls.append((method, path, p))
        s = self.state.get(p.get("symbol", "BTCUSDT"))
        if path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": self.dual})
        if path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": k, "positionSide": "BOTH", **v} for k, v in self.state.items()
                                             if "symbol" not in p or k == p["symbol"]])
        if path == "/fapi/v1/marginType":
            want = p["marginType"].lower().replace("crossed", "cross")
            if s["marginType"] == want:
                return self._err(-4046, "No need to change margin type.")
            if D(s["positionAmt"]) != 0:
                return self._err(-4048, "Margin type cannot be changed if there exists position.")
            s["marginType"] = want
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        if path == "/fapi/v1/leverage":
            if not self.ignore_leverage:
                s["leverage"] = p["leverage"]
            return httpx.Response(200, json={"symbol": p["symbol"], "leverage": int(p["leverage"])})
        if path == "/fapi/v1/order" and method == "POST":
            if p["type"] == "STOP_MARKET" and self.stop_error:
                return self._err(*self.stop_error)
            if p["type"] == "MARKET":
                qty = D(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
                s["positionAmt"] = str(D(s["positionAmt"]) + qty)
            return httpx.Response(200, json={"orderId": len(self.calls), "status": "NEW", **p})
        if path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=self.open_orders)
        if path == "/fapi/v1/order" and method == "DELETE":
            self.open_orders = [o for o in self.open_orders if o["clientOrderId"] != p["origClientOrderId"]]
            return httpx.Response(200, json={"status": "CANCELED"})
        return httpx.Response(404, json={"code": -1, "msg": f"unexpected {method} {path}"})

    def paths(self) -> list[str]:
        return [f"{m} {path}" for m, path, _ in self.calls if path != "/fapi/v1/positionSide/dual"]


def adapter_for(fake: FakeFapi) -> BinanceFuturesAdapter:
    a = BinanceFuturesAdapter("k", PEM, "https://fut.test")
    a.client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return a


def run(coro):
    return asyncio.run(coro)


def test_prepare_sets_margin_type_then_leverage_and_reads_them_back():
    fake = FakeFapi(margin="cross", leverage=20)
    out = run(prepare_entry(adapter_for(fake), PERP, 5))
    assert out["changed"] == ["margin_type", "leverage"]
    assert fake.paths() == ["GET /fapi/v2/positionRisk", "POST /fapi/v1/marginType", "POST /fapi/v1/leverage",
                            "GET /fapi/v2/positionRisk"]
    assert fake.state["BTCUSDT"] == {"marginType": "isolated", "leverage": "5", "positionAmt": "0"}


def test_prepare_changes_nothing_when_the_venue_already_matches():
    fake = FakeFapi(margin="isolated", leverage=5)
    out = run(prepare_entry(adapter_for(fake), PERP, 5))
    assert out["changed"] == [] and fake.paths() == ["GET /fapi/v2/positionRisk"]


def test_margin_type_with_an_open_position_refuses_the_entry():
    fake = FakeFapi(margin="cross", leverage=5, position="0.01")
    with pytest.raises(PerpVenueError) as e:
        run(prepare_entry(adapter_for(fake), PERP, 5))
    assert e.value.code == "MARGIN_TYPE_LOCKED"
    assert "POST /fapi/v1/marginType" not in fake.paths()  # refused before asking the exchange to change it


def test_a_locked_margin_type_answer_from_the_exchange_refuses_too():
    fake = FakeFapi(margin="cross", leverage=5)
    a = adapter_for(fake)

    async def go():
        await a.ensure_one_way_mode()
        fake.state["BTCUSDT"]["positionAmt"] = "0"
        # the position opens between the read and the change: Binance answers -4048
        orig = fake.handler

        def racing(request):
            if request.url.path == "/fapi/v1/marginType":
                fake.state["BTCUSDT"]["positionAmt"] = "0.02"
            return orig(request)
        a.client = httpx.AsyncClient(transport=httpx.MockTransport(racing))
        return await prepare_entry(a, PERP, 5)

    with pytest.raises(PerpVenueError) as e:
        run(go())
    assert e.value.code == "MARGIN_TYPE_LOCKED"


def test_a_leverage_the_exchange_did_not_take_is_a_mismatch():
    fake = FakeFapi(margin="isolated", leverage=20, ignore_leverage=True)
    with pytest.raises(PerpVenueError) as e:
        run(prepare_entry(adapter_for(fake), PERP, 5))
    assert e.value.code == "VENUE_STATE_MISMATCH"


def test_leverage_above_the_account_cap_is_refused_before_any_call():
    fake = FakeFapi(margin="isolated", leverage=5)
    with pytest.raises(PerpVenueError) as e:
        run(prepare_entry(adapter_for(fake), PERP, 20))
    assert e.value.code == "LEVERAGE_REFUSED" and "POST /fapi/v1/leverage" not in fake.paths()


def test_hedge_mode_is_refused():
    with pytest.raises(HedgeModeError):
        run(prepare_entry(adapter_for(FakeFapi(dual=True)), PERP, 5))


def test_the_stop_is_a_close_position_stop_market_on_the_mark():
    fake = FakeFapi(margin="isolated", leverage=5, position="0.01")
    out = run(protect_position(adapter_for(fake), PERP, "LONG", D("47500"), D("0.01"), "STOP-A"))
    assert out["emergency_close"] is None
    (order,) = [p for m, path, p in fake.calls if path == "/fapi/v1/order"]
    assert (order["type"], order["side"], order["stopPrice"], order["workingType"], order["closePosition"]) == \
        ("STOP_MARKET", "SELL", "47500", "MARK_PRICE", "true")


def test_a_refused_stop_closes_the_position_at_once():
    fake = FakeFapi(margin="isolated", leverage=5, position="-0.01", stop_error=(-2021, "Order would immediately trigger."))
    out = run(protect_position(adapter_for(fake), PERP, "SHORT", D("52500"), D("0.01"), "STOP-B"))
    assert out["stop"] is None and out["reason"].startswith("STOP_WOULD_TRIGGER_IMMEDIATELY")
    close = [p for m, path, p in fake.calls if path == "/fapi/v1/order" and p["type"] == "MARKET"][0]
    assert (close["side"], close["quantity"], close["reduceOnly"], close["newClientOrderId"]) == \
        ("BUY", "0.01", "true", "STOP-B-X")
    assert D(fake.state["BTCUSDT"]["positionAmt"]) == 0


def test_reconcile_reports_quantity_leverage_and_margin_differences():
    rows = [{"symbol": "BTCUSDT", "marginType": "isolated", "leverage": "7", "positionAmt": "0.035"},
            {"symbol": "ETHUSDT", "marginType": "cross", "leverage": "20", "positionAmt": "0"}]
    assert reconcile(rows, {PERP: (D("0.035"), 7, "ISOLATED")}) == []
    assert reconcile(rows, {PERP: (D("0.035"), 5, "ISOLATED")}) == [Mismatch("BTCUSDT", "leverage", "7", "5")]
    assert reconcile(rows, {PERP: (D("0.02"), 7, "ISOLATED")}) == [Mismatch("BTCUSDT", "quantity", "0.035", "0.02")]
    assert reconcile(rows, {}) == [Mismatch("BTCUSDT", "quantity", "0.035", "0")]
    assert reconcile(rows, {"ETHUSDT-PERP.BINANCE": (D("-1"), 3, "ISOLATED")}) == \
        [Mismatch("BTCUSDT", "quantity", "0.035", "0"), Mismatch("ETHUSDT", "quantity", "0", "-1")]


def test_flatten_cancels_orders_then_closes_positions_reduce_only():
    fake = FakeFapi(margin="isolated", leverage=5, position="-0.02")
    closed = run(flatten_all(adapter_for(fake), "KILL"))
    assert [c["symbol"] for c in closed] == ["BTCUSDT"] and fake.open_orders == []
    paths = fake.paths()
    assert paths.index("DELETE /fapi/v1/order") < paths.index("POST /fapi/v1/order")
    close = [p for m, path, p in fake.calls if path == "/fapi/v1/order" and m == "POST"][0]
    assert (close["side"], close["quantity"], close["reduceOnly"]) == ("BUY", "0.02", "true")
    assert D(fake.state["BTCUSDT"]["positionAmt"]) == 0
