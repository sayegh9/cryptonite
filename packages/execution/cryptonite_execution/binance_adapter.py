import asyncio
import base64
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
import urllib.parse

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

    def _build_signed_query(self, params: Dict[str, Any]) -> str:
        params["timestamp"] = int(time.time() * 1000)
        query_string = urllib.parse.urlencode(params)
        signature = self._sign_payload(query_string)
        return f"{query_string}&signature={urllib.parse.quote(signature)}"

    async def _request(self, method: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}?{self._build_signed_query(params)}"
        headers = {"X-MBX-APIKEY": self.api_key}
        try:
            response = await self.client.request(method, url, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            try:
                error_data = e.response.json()
                msg = error_data.get("msg", str(e))
                code = error_data.get("code", 0)
                raise RuntimeError(f"Binance API Error [{code}]: {msg}")
            except Exception:
                raise RuntimeError(f"Binance HTTP Error: {e.response.status_code}")
        except httpx.RequestError as e:
            raise RuntimeError(f"Binance Network/Timeout Error: {e}")

    async def close(self) -> None:
        await self.client.aclose()


class BinanceSpotAdapter(_BinanceBaseAdapter):
    """Binance Spot specific REST adapter (1200 weight/min)."""
    
    async def submit_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, price: Optional[Decimal], client_order_id: str) -> Dict[str, Any]:
        params = {"symbol": symbol, "side": side, "type": order_type, "newClientOrderId": client_order_id, "quantity": str(quantity)}
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT") and price:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        return await self._request("POST", "/api/v3/order", params)

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})
        except RuntimeError as e:
            if "Order does not exist" in str(e) or "[-2013]" in str(e): return None
            raise

    async def cancel_order(self, symbol: str, client_order_id: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/api/v3/openOrders", {})


class BinanceFuturesAdapter(_BinanceBaseAdapter):
    """Binance USD-M Futures specific REST adapter (2400 weight/min)."""
    
    async def submit_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, price: Optional[Decimal], client_order_id: str) -> Dict[str, Any]:
        params = {"symbol": symbol, "side": side, "type": order_type, "newClientOrderId": client_order_id, "quantity": str(quantity)}
        if order_type in ("LIMIT", "STOP", "TAKE_PROFIT") and price:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        return await self._request("POST", "/fapi/v1/order", params)

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id})
        except RuntimeError as e:
            if "Order does not exist" in str(e) or "[-2013]" in str(e): return None
            raise

    async def cancel_order(self, symbol: str, client_order_id: str) -> Dict[str, Any]:
        return await self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/fapi/v1/openOrders", {})
        
    async def get_leverage_brackets(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/fapi/v1/leverageBracket", params)


class BinanceCompositeAdapter:
    """Facade router segregating Spot and Futures execution requests."""
    def __init__(self, api_key: str, private_key_pem: bytes, spot_url: str, futures_url: str):
        self.spot = BinanceSpotAdapter(api_key, private_key_pem, spot_url)
        self.futures = BinanceFuturesAdapter(api_key, private_key_pem, futures_url)

    def _is_futures(self, symbol: str) -> bool:
        # Standardize matching logic. We can pass instrument suffixes like .PERP or assume standard binance futures parsing.
        return "PERP" in symbol or symbol.endswith("USDT") and "_" in symbol

    def get_adapter(self, symbol: str) -> _BinanceBaseAdapter:
        return self.futures if self._is_futures(symbol) else self.spot

    async def submit_order(self, symbol: str, side: str, order_type: str, quantity: Decimal, price: Optional[Decimal], client_order_id: str) -> Dict[str, Any]:
        return await self.get_adapter(symbol).submit_order(symbol, side, order_type, quantity, price, client_order_id)

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        return await self.get_adapter(symbol).query_order(symbol, client_order_id)

    async def cancel_order(self, symbol: str, client_order_id: str) -> Dict[str, Any]:
        return await self.get_adapter(symbol).cancel_order(symbol, client_order_id)

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        spot_orders = await self.spot.get_open_orders()
        futures_orders = await self.futures.get_open_orders()
        return spot_orders + futures_orders

    async def close(self) -> None:
        await self.spot.close()
        await self.futures.close()
