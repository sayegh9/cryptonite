"""Automated historical gap detection and REST backfill script for Cryptonite.

Fetches missing 1m bars from Binance Public REST API and commits them to the
Analytics Lake with proper date partitioning and validation.
"""

import asyncio
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptonite_schemas.bar import EnrichedBar, RawBinanceKline
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.lake_lock import lake_maintenance_lock_or_exit
from cryptonite_storage.queries import AnalyticsQueryEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cryptonite.backfill")

BINANCE_REST_URL = "https://api.binance.com"


async def fetch_klines_page(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int = 1000,
) -> list[RawBinanceKline]:
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "limit": limit,
    }
    for attempt in range(3):
        try:
            resp = await client.get("/api/v3/klines", params=params)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                logger.warning("Rate limited (429). Sleeping %d seconds...", retry_after)
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            raw_rows = resp.json()
            break
        except Exception as e:
            if attempt == 2:
                logger.error("Failed to fetch klines after 3 attempts: %s", e)
                raise
            await asyncio.sleep(1.0 * (attempt + 1))

    now_ms = int(time.time() * 1000) - 2_000  # same rule as BinanceRestClient: the forming kline is not closed
    results = []
    for r in raw_rows:
        if int(r[6]) >= now_ms:
            continue
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


async def backfill_symbol(
    symbol: str,
    timeframe: str = "1m",
    days: int = 2,
    analytics_dir: str = "data/analytics_lake",
):
    sym = symbol.strip().upper()
    inst_id = to_spot_instrument_id(sym)
    bar_type = to_nautilus_bar_type(inst_id, timeframe)
    writer = AnalyticsLakeWriter(analytics_dir)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 86400 * 1000)

    logger.info(
        "Starting backfill for %s (%s) from %s to %s (~%d days)...",
        sym,
        timeframe,
        datetime.fromtimestamp(start_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        days,
    )

    all_klines: list[RawBinanceKline] = []
    curr_start_ms = start_ms

    async with httpx.AsyncClient(base_url=BINANCE_REST_URL, timeout=15.0) as client:
        # 1. Fetch exchangeInfo for precisions
        info_resp = await client.get("/api/v3/exchangeInfo", params={"symbol": sym})
        info_resp.raise_for_status()
        sym_info = info_resp.json()["symbols"][0]
        filters = {f["filterType"]: f for f in sym_info.get("filters", [])}
        tick_size = filters.get("PRICE_FILTER", {}).get("tickSize", "0.01")
        step_size = filters.get("LOT_SIZE", {}).get("stepSize", "0.00001")

        price_precision = len(tick_size.split(".")[1].rstrip("0")) if "." in tick_size else 2
        size_precision = len(step_size.split(".")[1].rstrip("0")) if "." in step_size else 5

        # 2. Paginate historical klines
        while curr_start_ms < now_ms:
            batch = await fetch_klines_page(
                client=client,
                symbol=sym,
                interval=timeframe,
                start_time_ms=curr_start_ms,
                end_time_ms=now_ms,
                limit=1000,
            )
            if not batch:
                break
            all_klines.extend(batch)
            logger.info("Fetched page of %d bars (total: %d)", len(batch), len(all_klines))

            last_open = batch[-1].open_time_ms
            if last_open <= curr_start_ms:
                break
            curr_start_ms = last_open + 60_000  # advance by 1 minute
            if len(batch) < 1000:
                break

    if not all_klines:
        logger.warning("No bars retrieved for %s", sym)
        return

    # 3. Group by date and write enriched batches
    arrival_ns = time.time_ns()
    enriched = [
        EnrichedBar.from_binance_raw(
            raw=k,
            instrument_id=inst_id,
            bar_type=str(bar_type),
            price_precision=price_precision,
            size_precision=size_precision,
            received_at_ns=arrival_ns,
        )
        for k in all_klines
    ]

    # Group by UTC day so writer writes to the correct date=YYYY-MM-DD partitions
    day_batches: dict[str, list[EnrichedBar]] = {}
    for bar in enriched:
        bar_dt = datetime.fromtimestamp(bar.ts_event_ns / 1_000_000_000, tz=UTC)
        day_key = bar_dt.strftime("%Y-%m-%d")
        day_batches.setdefault(day_key, []).append(bar)

    for day_key, day_bars in sorted(day_batches.items()):
        file_path, checksum = writer.write_batch(
            bars=day_bars,
            venue="BINANCE",
            market_type="SPOT",
            timeframe=timeframe,
            symbol=sym,
        )
        logger.info("Wrote %d bars to partition %s (%s)", len(day_bars), day_key, file_path)

    logger.info("Successfully completed backfill for %s: %d total bars committed.", sym, len(enriched))


async def main():
    analytics_dir = os.getenv("ANALYTICS_LAKE_PATH", "data/analytics_lake")
    symbols = sys.argv[1].split(",") if len(sys.argv) > 1 else ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    # never at the same time as lake compaction or the open-stamped repair
    with lake_maintenance_lock_or_exit(analytics_dir):
        for sym in symbols:
            try:
                await backfill_symbol(symbol=sym, timeframe="1m", days=days, analytics_dir=analytics_dir)
            except Exception as e:
                logger.error("Error backfilling %s: %s", sym, e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
