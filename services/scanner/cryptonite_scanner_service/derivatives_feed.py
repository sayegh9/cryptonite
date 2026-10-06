"""Perpetual futures feed: mark price, funding rate, and open interest from Binance USD-M.

- Mark stream (``<symbol>@markPrice@1s``): latest tick cached at ``perp:mark:{symbol}`` (15s TTL)
  and published on ``channel:perp_mark_ticks``.
- Funding settlements are detected when a symbol's next funding time ``T`` rolls forward. The
  rate and mark from the last tick before the rollover are written to ``funding_rates``,
  deduplicated by (instrument_id, funding_time_ns).
- Funding history backfill: every hour, /fapi/v1/fundingRate for the last 30 days is inserted
  with source BINANCE_REST, so settlements missed while the stream was down are filled in.
  Rows already present (from either source) are left untouched.
- Open interest is polled from REST (Binance has no OI stream) into ``perp:oi:{symbol}``.
- ``derivatives_feed:heartbeat`` (90s TTL) is rewritten every 30 s by the event loop itself, whether or
  not Binance is reachable: process liveness for the container healthcheck (mark freshness is its own
  signal).

All timestamps come from exchange event times (``E``, ``T``, OI ``time``), never the wall clock.

Run: ``python -m cryptonite_scanner_service.derivatives_feed`` (env: DATABASE_URL, REDIS_URL,
PERP_SYMBOLS=BTCUSDT,ETHUSDT).
"""

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import redis.asyncio as aioredis
from cryptonite_schemas.identifiers import to_perp_instrument_id
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger("cryptonite.scanner.derivatives_feed")

# Market-data streams live under /market; the legacy /ws and /stream paths accept connections but send nothing
BINANCE_FUTURES_WS_URL = "wss://fstream.binance.com/market"
BINANCE_FUTURES_REST_URL = "https://fapi.binance.com"

MARK_KEY = "perp:mark:{symbol}"
OI_KEY = "perp:oi:{symbol}"
MARK_CHANNEL = "channel:perp_mark_ticks"
MARK_TTL_S = 15
OI_POLL_INTERVAL_S = 30
OI_TTL_S = 3 * OI_POLL_INTERVAL_S
FUNDING_BACKFILL_INTERVAL_S = 3_600
FUNDING_BACKFILL_LOOKBACK_DAYS = 30
FUNDING_PAGE_LIMIT = 1_000  # Binance maximum
HEARTBEAT_KEY = "derivatives_feed:heartbeat"
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_TTL_S = 3 * HEARTBEAT_INTERVAL_S
STALE_STREAM_S = 30  # @1s stream: no frame for this long means a dead connection
FUNDING_INTERVAL_HOURS = {1, 2, 4, 8}

MS_TO_NS = 1_000_000
HOUR_NS = 3_600 * 1_000_000_000

INSERT_FUNDING_SQL = """
INSERT INTO funding_rates (instrument_id, funding_time_ns, funding_rate, mark_price, funding_interval_hours, source)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (instrument_id, funding_time_ns) DO NOTHING
"""


@dataclass(frozen=True)
class MarkTick:
    symbol: str
    ts_event_ns: int
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    next_funding_time_ns: int

    @property
    def instrument_id(self) -> str:
        return to_perp_instrument_id(self.symbol)

    def to_json(self) -> str:
        return json.dumps({
            "type": "perp.mark",
            "v": 1,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "ts_event_ns": self.ts_event_ns,
            "mark_price": str(self.mark_price),
            "index_price": str(self.index_price),
            "funding_rate": str(self.funding_rate),
            "next_funding_time_ns": self.next_funding_time_ns,
        })


@dataclass(frozen=True)
class FundingSettlement:
    instrument_id: str
    funding_time_ns: int
    funding_rate: Decimal
    mark_price: Decimal
    funding_interval_hours: int | None


def parse_mark_frame(raw: str | bytes) -> list[MarkTick]:
    """Parse a markPriceUpdate frame: single event, ``!markPrice@arr`` list, or combined-stream wrapper."""
    payload: Any = json.loads(raw)
    if isinstance(payload, dict) and "data" in payload and "stream" in payload:
        payload = payload["data"]
    events = payload if isinstance(payload, list) else [payload]
    ticks = []
    for evt in events:
        if not isinstance(evt, dict) or evt.get("e") != "markPriceUpdate":
            continue
        ticks.append(MarkTick(
            symbol=evt["s"].upper(),
            ts_event_ns=int(evt["E"]) * MS_TO_NS,
            mark_price=Decimal(evt["p"]),
            index_price=Decimal(evt["i"]),
            funding_rate=Decimal(evt["r"]),
            next_funding_time_ns=int(evt["T"]) * MS_TO_NS,
        ))
    return ticks


def mark_stream_url(symbols: list[str], base_url: str = BINANCE_FUTURES_WS_URL) -> str:
    streams = "/".join(f"{s.lower()}@markPrice@1s" for s in symbols)
    return f"{base_url}/stream?streams={streams}"


class FundingSettlementTracker:
    """Detects funding settlements from rollovers of the next-funding-time field ``T``."""

    def __init__(self) -> None:
        self._last_tick: dict[str, MarkTick] = {}
        self._last_settlement_ns: dict[str, int] = {}

    def is_stale(self, tick: MarkTick) -> bool:
        prev = self._last_tick.get(tick.symbol)
        return prev is not None and tick.ts_event_ns < prev.ts_event_ns

    def observe(self, tick: MarkTick) -> FundingSettlement | None:
        if self.is_stale(tick):
            return None  # out-of-order frame; keep the newer state
        prev = self._last_tick.get(tick.symbol)
        self._last_tick[tick.symbol] = tick
        if prev is None or tick.next_funding_time_ns <= prev.next_funding_time_ns:
            return None

        settled_ns = prev.next_funding_time_ns
        interval_hours = None
        last_settled = self._last_settlement_ns.get(tick.symbol)
        if last_settled is not None and (settled_ns - last_settled) % HOUR_NS == 0:
            hours = (settled_ns - last_settled) // HOUR_NS
            interval_hours = hours if hours in FUNDING_INTERVAL_HOURS else None
        self._last_settlement_ns[tick.symbol] = settled_ns
        return FundingSettlement(
            instrument_id=prev.instrument_id,
            funding_time_ns=settled_ns,
            funding_rate=prev.funding_rate,
            mark_price=prev.mark_price,
            funding_interval_hours=interval_hours,
        )


async def store_funding_settlements(
    pool: asyncpg.Pool, settlements: list[FundingSettlement], source: str = "STREAM_SETTLEMENT"
) -> int:
    """Insert settlements idempotently; returns the number of new rows."""
    inserted = 0
    async with pool.acquire() as conn:
        for s in settlements:
            status = await conn.execute(
                INSERT_FUNDING_SQL,
                s.instrument_id, s.funding_time_ns, s.funding_rate, s.mark_price, s.funding_interval_hours, source,
            )
            inserted += int(status.rsplit(" ", 1)[-1])
    return inserted


def parse_funding_history(symbol: str, rows: list[dict[str, Any]]) -> list[FundingSettlement]:
    """Turn /fapi/v1/fundingRate rows into settlements, oldest first.

    The interval is the gap to the neighbouring settlement (previous, else next) when it is
    1/2/4/8 hours; an empty ``markPrice`` (older history) becomes None.
    """
    ordered = sorted(rows, key=lambda r: int(r["fundingTime"]))
    times = [int(r["fundingTime"]) * MS_TO_NS for r in ordered]
    out = []
    for i, row in enumerate(ordered):
        gap = times[i] - times[i - 1] if i > 0 else (times[i + 1] - times[i] if i + 1 < len(times) else 0)
        hours = gap // HOUR_NS if gap > 0 and gap % HOUR_NS == 0 else None
        out.append(FundingSettlement(
            instrument_id=to_perp_instrument_id(symbol),
            funding_time_ns=times[i],
            funding_rate=Decimal(row["fundingRate"]),
            mark_price=Decimal(row["markPrice"]) if row.get("markPrice") else None,
            funding_interval_hours=hours if hours in FUNDING_INTERVAL_HOURS else None,
        ))
    return out


class DerivativesFeed:
    def __init__(
        self,
        redis: aioredis.Redis,
        pg_pool: asyncpg.Pool,
        symbols: list[str],
        http_client: httpx.AsyncClient | None = None,
        connect: Callable[..., Any] = ws_connect,
        ws_base_url: str = BINANCE_FUTURES_WS_URL,
        rest_base_url: str = BINANCE_FUTURES_REST_URL,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
    ) -> None:
        if not symbols:
            raise ValueError("at least one perpetual symbol is required")
        self.redis = redis
        self.pool = pg_pool
        self.symbols = [s.upper() for s in symbols]
        self.http = http_client
        self._connect = connect
        self.ws_url = mark_stream_url(self.symbols, ws_base_url)
        self.rest_base_url = rest_base_url
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.tracker = FundingSettlementTracker()

    async def handle_frame(self, raw: str | bytes) -> list[FundingSettlement]:
        ticks = parse_mark_frame(raw)
        if not ticks:
            return []
        settlements = []
        async with self.redis.pipeline(transaction=False) as pipe:
            for tick in ticks:
                if self.tracker.is_stale(tick):
                    continue
                body = tick.to_json()
                pipe.set(MARK_KEY.format(symbol=tick.symbol), body, ex=MARK_TTL_S)
                pipe.publish(MARK_CHANNEL, body)
                if (settlement := self.tracker.observe(tick)) is not None:
                    settlements.append(settlement)
            await pipe.execute()
        if settlements:
            inserted = await store_funding_settlements(self.pool, settlements)
            logger.info("Recorded %d funding settlement(s) (%d new)", len(settlements), inserted)
        return settlements

    def _backoff_delay(self, attempt: int) -> float:
        return min(self.backoff_cap_s, self.backoff_base_s * 2**attempt) * random.uniform(0.5, 1.0)

    async def run_mark_stream(self, stop: asyncio.Event) -> None:
        """Consume the mark stream until ``stop`` is set, reconnecting with jittered exponential backoff."""
        attempt = 0
        while not stop.is_set():
            try:
                async with self._connect(self.ws_url, open_timeout=10, ping_interval=20) as ws:
                    logger.info("Connected to %s", self.ws_url)
                    while not stop.is_set():
                        async with asyncio.timeout(STALE_STREAM_S):
                            raw = await ws.recv()
                        await self.handle_frame(raw)
                        attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any stream failure means reconnect
                if stop.is_set():
                    break
                delay = self._backoff_delay(attempt)
                attempt += 1
                logger.warning("Mark stream error (%s: %s); reconnecting in %.1fs", type(exc).__name__, exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def poll_open_interest_once(self) -> None:
        if self.http is None:
            raise RuntimeError("http_client is required for open interest polling")
        for symbol in self.symbols:
            try:
                resp = await self.http.get(f"{self.rest_base_url}/fapi/v1/openInterest", params={"symbol": symbol})
                resp.raise_for_status()
                data = resp.json()
                body = json.dumps({
                    "type": "perp.open_interest",
                    "v": 1,
                    "instrument_id": to_perp_instrument_id(symbol),
                    "symbol": symbol,
                    "open_interest": str(Decimal(data["openInterest"])),
                    "ts_event_ns": int(data["time"]) * MS_TO_NS,
                })
                await self.redis.set(OI_KEY.format(symbol=symbol), body, ex=OI_TTL_S)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                logger.warning("Open interest poll failed for %s: %s", symbol, exc)

    async def backfill_funding_once(self, now_ms: int, lookback_days: int = FUNDING_BACKFILL_LOOKBACK_DAYS) -> int:
        """Insert REST funding history for the lookback window; returns the number of new rows."""
        if self.http is None:
            raise RuntimeError("http_client is required for funding backfill")
        inserted = 0
        for symbol in self.symbols:
            start_ms = now_ms - lookback_days * 24 * 3_600_000
            try:
                history: list[dict[str, Any]] = []
                while True:
                    resp = await self.http.get(
                        f"{self.rest_base_url}/fapi/v1/fundingRate",
                        params={"symbol": symbol, "startTime": start_ms, "endTime": now_ms, "limit": FUNDING_PAGE_LIMIT},
                    )
                    resp.raise_for_status()
                    rows = resp.json()
                    history.extend(rows)
                    if len(rows) < FUNDING_PAGE_LIMIT:
                        break
                    start_ms = max(int(r["fundingTime"]) for r in rows) + 1
                # Parse the whole window at once so every row has a neighbour to infer its interval
                if history:
                    inserted += await store_funding_settlements(
                        self.pool, parse_funding_history(symbol, history), source="BINANCE_REST"
                    )
            except (httpx.HTTPError, KeyError, ValueError, ArithmeticError) as exc:
                logger.warning("Funding backfill failed for %s: %s", symbol, exc)
        if inserted:
            logger.info("Funding backfill inserted %d missing settlement(s)", inserted)
        return inserted

    async def run_funding_backfill(self, stop: asyncio.Event, interval_s: float = FUNDING_BACKFILL_INTERVAL_S) -> None:
        while not stop.is_set():
            try:
                await self.backfill_funding_once(now_ms=int(time.time() * 1000))
            except Exception as exc:  # noqa: BLE001 - DB outages must not kill the loop
                logger.warning("Funding backfill error: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass

    async def run_heartbeat(self, stop: asyncio.Event, interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        while not stop.is_set():
            try:
                await self.redis.set(HEARTBEAT_KEY, json.dumps({"symbols": self.symbols}), ex=HEARTBEAT_TTL_S)
            except Exception as e:  # Redis down: the healthcheck reports it; keep the feed running
                logger.warning("Could not write %s: %s", HEARTBEAT_KEY, e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass

    async def run_oi_poller(self, stop: asyncio.Event, interval_s: float = OI_POLL_INTERVAL_S) -> None:
        while not stop.is_set():
            await self.poll_open_interest_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    symbols = [s.strip() for s in os.getenv("PERP_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    pool = await asyncpg.create_pool(
        os.getenv("DATABASE_URL", "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"),
        min_size=1, max_size=2,
    )
    redis = aioredis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"))
    stop = asyncio.Event()
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            feed = DerivativesFeed(redis, pool, symbols, http_client=http)
            await asyncio.gather(feed.run_mark_stream(stop), feed.run_oi_poller(stop), feed.run_funding_backfill(stop),
                                 feed.run_heartbeat(stop))
    finally:
        stop.set()
        await redis.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
