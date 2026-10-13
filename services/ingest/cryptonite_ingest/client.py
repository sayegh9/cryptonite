"""Binance Public REST API Client with rate limiting and retry handling."""

import asyncio
import logging
import time
from datetime import UTC
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from cryptonite_schemas.bar import RawBinanceKline

logger = logging.getLogger("cryptonite.client")

BINANCE_SPOT_BASE_URL = "https://api.binance.com"


CLOSED_MARGIN_MS = 2_000

# --- Rate-limit policy (F19 round 2: fail-closed retries, bounded pacing) ---
# Binance Spot reports X-MBX-USED-WEIGHT-1M per IP, window resets every 60s; the public-endpoint
# IP cap is 6000 weight/min (1 /klines call at limit=1000 costs weight 5).
_RETRY_AFTER_DEFAULT_S = 5      # fallback wait when Retry-After is absent on a 429
_RETRY_AFTER_MAX_S = 30         # per-wait cap; a server-requested wait above this fails closed
_RETRY_TOTAL_BUDGET_S = 60.0    # total sleep budget for one fetch_klines() retry sequence
_HIGH_WEIGHT_THRESHOLD = 1000   # early-signal pacing gate (well below the 6000/min cap)
_CRITICAL_WEIGHT_THRESHOLD = 3000
_HIGH_WEIGHT_PACE_S = 1.0
_CRITICAL_WEIGHT_PACE_S = 3.0
_WEIGHT_WINDOW_S = 60.0         # Binance weight reset window


class BinanceRestClient:
    """Async client for Binance public REST endpoints."""

    def __init__(self, base_url: str = BINANCE_SPOT_BASE_URL, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._rate_delay = 0.05  # minimum delay between requests to stay well within limits
        self.last_used_weight_1m: int = 0
        self._weight_at: float = 0.0  # time of the last weight reading (1-minute window expiry)

    def _update_weight(self, resp: httpx.Response) -> None:
        """Track X-MBX-USED-WEIGHT-1M (value + reading time for the 1-minute window)."""
        weight_header = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if weight_header is None:
            return
        try:
            self.last_used_weight_1m = int(weight_header.strip())
        except ValueError:
            logger.warning("Malformed X-MBX-USED-WEIGHT-1M header: %r", weight_header)
            return
        self._weight_at = time.time()

    def _current_weight(self, now: float | None = None) -> int:
        """Weight currently within Binance's 1-minute window; a stale reading counts as 0.

        Binance resets ``X-MBX-USED-WEIGHT-1M`` every 60s. Keeping the reading time means a client
        that has been idle past the window re-arms at a clean slate instead of throttling on a
        weight figure the server no longer counts.
        """
        now = time.time() if now is None else now
        if now - self._weight_at >= _WEIGHT_WINDOW_S:
            return 0
        return self.last_used_weight_1m

    def _parse_retry_after(self, resp: httpx.Response) -> int | None:
        """Seconds from the Retry-After header, or None when absent/malformed.

        Handles both RFC 7231 forms: delay-seconds and HTTP-date. An HTTP-date already in the past
        yields 1s (the suggested moment has passed); callers apply their own default for None, and a
        fail-closed check applies the safe cap before any sleep.
        """
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(1, int(raw))
        except ValueError:
            pass
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            dt = None
        if dt is None:
            logger.warning("Malformed Retry-After header: %r", raw)
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(1, int(dt.timestamp() - time.time()))

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
        self._update_weight(resp)
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

        # Weight pacing gate — bounded, never a stall. It only uses a reading that is still inside
        # Binance's 1-minute window (stale readings reset), fires at most one fixed sleep, and the
        # request still goes out: recovery proceeds at ~1-3s/call worst case, never halts. The
        # authoritative protection is the fail-closed 429/Retry-After budget and the immediate 418
        # raise inside the loop; this gate merely slows replay near the cap.
        current_weight = self._current_weight()
        if current_weight >= _CRITICAL_WEIGHT_THRESHOLD:
            pace = _CRITICAL_WEIGHT_PACE_S
            logger.error(
                "Binance REST weight critical (%d). Pacing %0.1fs before request.",
                current_weight,
                pace,
            )
        elif current_weight >= _HIGH_WEIGHT_THRESHOLD:
            pace = _HIGH_WEIGHT_PACE_S
            logger.warning(
                "Binance REST weight high (%d). Pacing %0.1fs before request.",
                current_weight,
                pace,
            )
        else:
            pace = 0.0
        if pace:
            await asyncio.sleep(pace)

        raw_rows: list[Any] | None = None
        last_error: Exception | None = None
        total_retry_slept = 0.0

        for attempt in range(3):
            try:
                resp = await client.get("/api/v3/klines", params=params)
            except httpx.TransportError as exc:
                # Transport-level failure only (connect/read/timeout): transient, retry with backoff.
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(1.0 * (attempt + 1))
                continue

            self._update_weight(resp)

            if resp.status_code == 429:
                wait = self._parse_retry_after(resp)
                if wait is None:
                    wait = _RETRY_AFTER_DEFAULT_S
                # Fail closed: never sleep beyond the per-wait cap, never blow the total sleep budget
                # for one fetch_klines() call. A wait the server asks longer than we can safely honor
                # raises immediately instead of hammering before the ban/limit expires.
                if wait > _RETRY_AFTER_MAX_S or total_retry_slept + wait > _RETRY_TOTAL_BUDGET_S:
                    raise httpx.HTTPStatusError(
                        f"Rate limited (429); server requested a {wait}s wait, exceeding the safe "
                        f"retry budget (cap {_RETRY_AFTER_MAX_S}s, total {_RETRY_TOTAL_BUDGET_S:g}s); "
                        "failing closed without further retries.",
                        request=resp.request,
                        response=resp,
                    )
                logger.warning(
                    "Rate limited (429) on attempt %d/3. Sleeping %d seconds", attempt + 1, wait
                )
                await asyncio.sleep(wait)
                total_retry_slept += float(wait)
                last_error = httpx.HTTPStatusError(
                    "Rate limited (429)", request=resp.request, response=resp
                )
                continue

            if resp.status_code == 418:
                # IP ban: fail fast. Retrying into a ban prolongs it and wastes requests; surface the
                # error (with the server's suggested wait, when given) to the caller/operator instead.
                wait = self._parse_retry_after(resp)
                detail = f"; Retry-After suggests {wait}s" if wait is not None else ""
                raise httpx.HTTPStatusError(
                    f"IP banned (418); raising immediately without retrying{detail}",
                    request=resp.request,
                    response=resp,
                )

            if resp.status_code >= 400:
                if 500 <= resp.status_code < 600:
                    # Transient server error: a short bounded backoff is worthwhile.
                    last_error = httpx.HTTPStatusError(
                        f"Binance REST returned status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    if attempt == 2:
                        break
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                # Deterministic 4xx (400/404/415/451...): permanent, never retry.
                raise httpx.HTTPStatusError(
                    f"Binance REST returned status {resp.status_code} (not retried)",
                    request=resp.request,
                    response=resp,
                )

            raw_rows = resp.json()
            break

        if raw_rows is None:
            if last_error:
                raise last_error
            raise httpx.HTTPError("Failed to fetch klines after 3 attempts")

        # The last kline of a range ending "now" is still forming; never report it as closed. The margin
        # covers this host's clock running ahead of Binance's (a partial bar would block the real one).
        now_ms = int(time.time() * 1000) - CLOSED_MARGIN_MS
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
                    is_closed=int(r[6]) < now_ms,
                )
            )
        return results

    def _calc_precision(self, val_str: str) -> int:
        if "." in val_str:
            dec_part = val_str.split(".")[1].rstrip("0")
            return len(dec_part) if dec_part else 0
        return 0
