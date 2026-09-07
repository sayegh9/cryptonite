"""Binance Public REST API Client with rate limiting and retry handling."""

import asyncio
import logging
from decimal import Decimal
from typing import Any

import httpx
from cryptonite_schemas.bar import RawBinanceKline

logger = logging.getLogger("cryptonite.client")

BINANCE_SPOT_BASE_URL = "https://api.binance.com"


class BinanceRestClient:
    """Async client for Binance public REST endpoints."""

    def __init__(self, base_url: str = BINANCE_SPOT_BASE_URL, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._rate_delay = 0.05  # minimum delay between requests to stay well within limits

    async def __aenter__(self) -> "BinanceRestClient":
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def get_exchange_info(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Fetch Binance Spot exchangeInfo and parse filters."""
        client = await self._get_client()
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        
        resp = await client.get("/api/v3/exchangeInfo", params=params)
        resp.raise_for_status()
        data = resp.json()
        
        symbols_info = data.get("symbols", [])
        parsed_results = []
        for s in symbols_info:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))

            tick_size = price_filter.get("tickSize", "0.01")
            step_size = lot_filter.get("stepSize", "0.00001")
            min_notional = notional_filter.get("minNotional", "5.0")

            # Calculate precisions from step and tick sizes
            price_prec = self._calc_precision(tick_size)
            size_prec = self._calc_precision(step_size)

            parsed_results.append({
                "symbol": s["symbol"],
                "base_asset": s["baseAsset"],
                "quote_asset": s["quoteAsset"],
                "status": s["status"],
                "is_trading": s["status"] == "TRADING",
                "price_precision": price_prec,
                "size_precision": size_prec,
                "price_increment": Decimal(tick_size),
                "size_increment": Decimal(step_size),
                "min_notional": Decimal(min_notional) if min_notional else None,
                "min_price": Decimal(price_filter.get("minPrice", "0.01")),
                "max_price": Decimal(price_filter.get("maxPrice", "1000000")),
                "min_qty": Decimal(lot_filter.get("minQty", "0.00001")),
                "max_qty": Decimal(lot_filter.get("maxQty", "9000")),
            })
        return parsed_results

    async def fetch_klines(
        self,
        symbol: str,
        interval: str = "1m",
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> list[RawBinanceKline]:
        """Fetch historical klines from Binance REST API."""
        client = await self._get_client()
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms

        await asyncio.sleep(self._rate_delay)
        for attempt in range(3):
            try:
                resp = await client.get("/api/v3/klines", params=params)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    logger.warning("Rate limited (429). Sleeping %d seconds", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                raw_rows = resp.json()
                break
            except (httpx.HTTPError, httpx.TimeoutException):
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))

        results = []
        for r in raw_rows:
            # Binance kline array schema:
            # 0: Open time, 1: Open, 2: High, 3: Low, 4: Close, 5: Volume,
            # 6: Close time, 7: Quote asset volume, 8: Number of trades,
            # 9: Taker buy base asset volume, 10: Taker buy quote asset volume, 11: Ignore
            results.append(
                RawBinanceKline(
                    open_time_ms=int(r[0]),
                    open=str(r[1]),
                    high=str(r[2]),
                    low=str(r[3]),
                    close=str(r[4]),
                    volume=str(r[5]),
                    close_time_ms=int(r[6]),
                    quote_volume=str(r[7]),
                    trades_count=int(r[8]),
                    taker_buy_base_volume=str(r[9]),
                    taker_buy_quote_volume=str(r[10]),
                    is_closed=True,
                )
            )
        return results

    def _calc_precision(self, val_str: str) -> int:
        if "." in val_str:
            dec_part = val_str.split(".")[1].rstrip("0")
            return len(dec_part) if dec_part else 0
        return 0
