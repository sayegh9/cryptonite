import base64
import logging
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

logger = logging.getLogger(__name__)

class _BinanceBaseAdapter:
    """Base class for Binance REST adapters with Ed25519 signing."""
    def __init__(self, api_key: str, private_key_pem: bytes, base_url: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._private_key: ed25519.Ed25519PrivateKey = serialization.load_pem_private_key(
            private_key_pem, password=None
        )
        self.client = httpx.AsyncClient(timeout=10.0)

    def _sign_payload(self, query_string: str) -> str:
        sig = self._private_key.sign(query_string.encode("utf-8"))
        return base64.b64encode(sig).decode("utf-8")

    def _build_signed_query(self, params: dict[str, Any]) -> str:
        params["timestamp"] = int(time.time() * 1000)
        query_string = urllib.parse.urlencode(params)
        signature = self._sign_payload(query_string)
        return f"{query_string}&signature={urllib.parse.quote(signature)}"

    async def _request(self, method: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}?{self._build_signed_query(params)}"
        headers = {"X-MBX-APIKEY": self.api_key}
        try:
            response = await self.client.request(method, url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            # The code must survive: callers match on it (-2013 no such order, -4046 margin type unchanged)
            try:
                error_data = e.response.json()
            except Exception:
                raise RuntimeError(f"Binance HTTP Error: {e.response.status_code}") from e
            msg = error_data.get("msg", str(e)) if isinstance(error_data, dict) else str(e)
            code = error_data.get("code", 0) if isinstance(error_data, dict) else 0
            raise RuntimeError(f"Binance API Error [{code}]: {msg}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Binance Network/Timeout Error: {e}")

    async def close(self) -> None:
        await self.client.aclose()


class BinanceSpotAdapter(_BinanceBaseAdapter):
    """Binance Spot specific REST adapter (1200 weight/min)."""
    
    async def submit_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, price: Decimal | None, client_order_id: str) -> dict[str, Any]:
        params = {"symbol": symbol, "side": side, "type": order_type, "newClientOrderId": client_order_id, "quantity": str(quantity)}
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT") and price:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        return await self._request("POST", "/api/v3/order", params)

    async def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        try:
            return await self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})
        except RuntimeError as e:
            if "Order does not exist" in str(e) or "[-2013]" in str(e):
                return None
            raise

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v3/openOrders", {})


# USD-M order types that trigger on a stop price; they work on the mark price, never the last trade
FUTURES_STOP_TYPES = ("STOP_MARKET", "TAKE_PROFIT_MARKET")
FUTURES_MARGIN_TYPES = {"ISOLATED": "ISOLATED", "CROSS": "CROSSED", "CROSSED": "CROSSED"}
# Account leverage cap (ADR 0009 D2): the adapter refuses to set more, whatever the exchange allows
MAX_FUTURES_LEVERAGE = 10


def futures_symbol(instrument: str) -> str:
    """BTCUSDT-PERP.BINANCE / BTCUSDT-PERP / BTCUSDT -> BTCUSDT (the symbol Binance USD-M expects)."""
    s = instrument.strip().upper().split(".", 1)[0]
    return s[:-5] if s.endswith("-PERP") else s


def is_perp_symbol(instrument: str) -> bool:
    """Routing rule: a USD-M perpetual carries the ``-PERP`` suffix (``BTCUSDT-PERP[.BINANCE]``); anything else is spot."""
    return instrument.strip().upper().split(".", 1)[0].endswith("-PERP")


class HedgeModeError(RuntimeError):
    """The futures account is in hedge (dual-side) position mode; Cryptonite trades one-way mode only."""


class BinanceFuturesAdapter(_BinanceBaseAdapter):
    """Binance USD-M Futures specific REST adapter (2400 weight/min).

    One-way position mode only: the first order checks ``/fapi/v1/positionSide/dual`` and every order is
    refused while the account is in hedge mode (``HedgeModeError``).
    """

    def __init__(self, api_key: str, private_key_pem: bytes, base_url: str):
        super().__init__(api_key, private_key_pem, base_url)
        self._one_way_checked = False

    async def get_position_mode(self) -> bool:
        """True when the account is in hedge (dual-side) mode."""
        res = await self._request("GET", "/fapi/v1/positionSide/dual", {})
        return bool(res.get("dualSidePosition"))

    async def ensure_one_way_mode(self) -> None:
        if self._one_way_checked:
            return
        if await self.get_position_mode():
            raise HedgeModeError("futures account is in hedge position mode; switch it to one-way mode")
        self._one_way_checked = True

    async def submit_order(
        self, symbol: str, side: str, order_type: str, quantity: Decimal | None, price: Decimal | None,
        client_order_id: str, *, reduce_only: bool = False, stop_price: Decimal | None = None,
        close_position: bool = False, working_type: str = "MARK_PRICE",
    ) -> dict[str, Any]:
        """Place a USD-M order. ``STOP_MARKET`` / ``TAKE_PROFIT_MARKET`` need ``stop_price`` and trigger on
        ``working_type`` (the mark price by default). ``close_position`` closes the whole position when the stop
        triggers (no quantity, no reduce-only: Binance refuses both with it)."""
        await self.ensure_one_way_mode()
        order_type = order_type.upper()
        params: dict[str, Any] = {"symbol": futures_symbol(symbol), "side": side.upper(), "type": order_type,
                                  "newClientOrderId": client_order_id}
        if order_type in FUTURES_STOP_TYPES:
            if stop_price is None or stop_price <= 0:
                raise ValueError(f"{order_type} needs a positive stop_price")
            params["stopPrice"] = str(stop_price)
            params["workingType"] = working_type
        if close_position:
            if order_type not in FUTURES_STOP_TYPES:
                raise ValueError("close_position is only valid on STOP_MARKET / TAKE_PROFIT_MARKET")
            params["closePosition"] = "true"
        else:
            if quantity is None or quantity <= 0:
                raise ValueError("quantity must be > 0 unless close_position is set")
            params["quantity"] = str(quantity)
            if reduce_only:
                params["reduceOnly"] = "true"
        if order_type in ("LIMIT", "STOP", "TAKE_PROFIT") and price:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        return await self._request("POST", "/fapi/v1/order", params)

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        if not 1 <= int(leverage) <= MAX_FUTURES_LEVERAGE:
            raise ValueError(f"leverage {leverage} outside 1..{MAX_FUTURES_LEVERAGE}")
        return await self._request("POST", "/fapi/v1/leverage", {"symbol": futures_symbol(symbol), "leverage": int(leverage)})

    async def set_margin_type(self, symbol: str, margin_mode: str) -> dict[str, Any]:
        """ISOLATED or CROSS (CROSSED on the wire). Binance's -4046 "No need to change margin type" is success."""
        margin_type = FUTURES_MARGIN_TYPES.get(margin_mode.upper())
        if margin_type is None:
            raise ValueError(f"unknown margin mode {margin_mode}")
        try:
            return await self._request("POST", "/fapi/v1/marginType", {"symbol": futures_symbol(symbol), "marginType": margin_type})
        except RuntimeError as e:
            if "[-4046]" in str(e):
                return {"code": -4046, "msg": "No need to change margin type."}
            raise

    async def get_position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open positions as the exchange sees them (leverage, margin type, entry, liquidation price), for reconciliation."""
        params = {"symbol": futures_symbol(symbol)} if symbol else {}
        return await self._request("GET", "/fapi/v2/positionRisk", params)

    async def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        try:
            return await self._request("GET", "/fapi/v1/order", {"symbol": futures_symbol(symbol), "origClientOrderId": client_order_id})
        except RuntimeError as e:
            if "Order does not exist" in str(e) or "[-2013]" in str(e):
                return None
            raise

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", "/fapi/v1/order", {"symbol": futures_symbol(symbol), "origClientOrderId": client_order_id})

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/openOrders", {})
        
    async def get_leverage_brackets(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": futures_symbol(symbol)} if symbol else {}
        return await self._request("GET", "/fapi/v1/leverageBracket", params)


class BinanceCompositeAdapter:
    """Facade router segregating Spot and Futures execution requests."""
    def __init__(self, api_key: str, private_key_pem: bytes, spot_url: str, futures_url: str):
        self.spot = BinanceSpotAdapter(api_key, private_key_pem, spot_url)
        self.futures = BinanceFuturesAdapter(api_key, private_key_pem, futures_url)

    def _is_futures(self, symbol: str) -> bool:
        # Routed by the instrument suffix: BTCUSDT-PERP[.BINANCE] is USD-M, BTCUSDT[.BINANCE] is spot
        return is_perp_symbol(symbol)

    def get_adapter(self, symbol: str) -> _BinanceBaseAdapter:
        return self.futures if self._is_futures(symbol) else self.spot

    async def submit_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, price: Decimal | None,
                           client_order_id: str, **futures_flags: Any) -> dict[str, Any]:
        """``futures_flags`` (reduce_only, stop_price, close_position, working_type) are USD-M only; spot refuses them."""
        if self._is_futures(symbol):
            return await self.futures.submit_order(symbol, side, order_type, quantity, price, client_order_id, **futures_flags)
        if any(futures_flags.values()):
            raise ValueError(f"futures-only order flags on spot symbol {symbol}: {sorted(k for k, v in futures_flags.items() if v)}")
        return await self.spot.submit_order(symbol, side, order_type, quantity, price, client_order_id)

    async def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        return await self.get_adapter(symbol).query_order(symbol, client_order_id)

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return await self.get_adapter(symbol).cancel_order(symbol, client_order_id)

    async def get_open_orders(self) -> list[dict[str, Any]]:
        spot_orders = await self.spot.get_open_orders()
        futures_orders = await self.futures.get_open_orders()
        return spot_orders + futures_orders

    async def close(self) -> None:
        await self.spot.close()
        await self.futures.close()
