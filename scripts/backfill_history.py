"""LE-1: backfill market history into the Nautilus catalog AND the analytics lake, gaps only.

For each symbol and timeframe, asks the catalog which intervals of the last ``--days`` are missing,
fetches only those from Binance REST (closed klines only, 1000 per page) and commits every page
through the production ``TwoStoreCoordinator`` (cadence check, catalog + lake). It runs without the
checkpoint database on purpose: the ingest checkpoint guard skips anything older than the live
checkpoint, which is exactly the history this fills.

Stop the live recorder first so only one process writes the catalog; its startup recovery fills the
minutes it missed:

    docker stop cryptonite_ingest
    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/backfill_history.py --days 120
    docker start cryptonite_ingest

Re-running is safe: a second pass only fetches what is still missing (exchange outages stay as gaps).
"""

import argparse
import asyncio
import logging
import time
from datetime import UTC, datetime

from cryptonite_ingest.client import BinanceRestClient
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_spot_instrument_id
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cryptonite.backfill_history")

TF_MS = {"1m": 60_000, "1h": 3_600_000}
LIVE_MARGIN_MS = 10 * 60_000  # never touch the last minutes: the live recorder owns them


def _fmt(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def missing_runs(intervals: list[tuple[int, int]], start_ns: int, end_ns: int, step_ns: int) -> list[list[int]]:
    """Bar close-stamps on the grid in [start, end] that no catalog file covers, as contiguous runs.

    The catalog's own interval API cannot be used for this: live ingest writes one file per bar, a
    single point [t, t], so it reports a "gap" between every two consecutive bars."""
    covered: set[int] = set()
    for a, b in intervals:
        t = max(a, start_ns)
        t += (-t) % step_ns
        while t <= min(b, end_ns):
            covered.add(t)
            t += step_ns
    runs: list[list[int]] = []
    t = start_ns + (-start_ns) % step_ns
    while t <= end_ns:
        if t not in covered:
            if runs and runs[-1][-1] == t - step_ns:
                runs[-1].append(t)
            else:
                runs.append([t])
        t += step_ns
    return runs


async def backfill(rest: BinanceRestClient, coordinator: TwoStoreCoordinator, catalog: NautilusCatalogManager,
                   symbol: str, timeframe: str, days: int, precisions: dict[str, int]) -> int:
    inst_id = to_spot_instrument_id(symbol)
    bar_type = to_nautilus_bar_type(inst_id, timeframe)
    # an empty file from a killed writer claims its interval; move it aside so the gap shows
    if quarantined := catalog.quarantine_empty_bar_files(str(bar_type)):
        logger.warning("%s %s: quarantined %d empty catalog file(s): %s", symbol, timeframe, len(quarantined), quarantined)
    step = TF_MS[timeframe]
    end_ms = (int(time.time() * 1000) - LIVE_MARGIN_MS) // step * step
    start_ms = end_ms - days * 86_400_000
    runs = missing_runs(catalog.get_intervals("bars", str(bar_type)), start_ms * 1_000_000, end_ms * 1_000_000,
                        step * 1_000_000)
    logger.info("%s %s: %d missing bars in %d run(s) over the last %d d", symbol, timeframe,
                sum(map(len, runs)), len(runs), days)
    written = 0
    for run in runs:
        wanted = set(run)
        # a bar is stamped at its close; Binance pages by open time
        cursor, last_open = run[0] // 1_000_000 - step, run[-1] // 1_000_000 - step
        while cursor <= last_open:
            page = await rest.fetch_klines(symbol, timeframe, start_time_ms=cursor, end_time_ms=last_open, limit=1000)
            klines = [k for k in page if k.is_closed]
            if not klines:
                break
            bars = [b for b in (EnrichedBar.from_binance_raw(raw=k, instrument_id=inst_id, bar_type=str(bar_type),
                                                             received_at_ns=time.time_ns(), **precisions) for k in klines)
                    if b.ts_event_ns in wanted]  # never rewrite a bar the catalog already has
            if bars:
                try:
                    await coordinator.commit_batch(bars=bars, bar_type=bar_type, venue="BINANCE", market_type="SPOT",
                                                   symbol=symbol, timeframe=timeframe)
                    written += len(bars)
                except Exception as e:  # e.g. a cadence break inside the page (exchange outage)
                    logger.warning("  %d bars from %s rejected: %s", len(bars), _fmt(bars[0].ts_event_ns), str(e)[:200])
            next_cursor = klines[-1].open_time_ms + step
            if next_cursor <= cursor:
                break
            cursor = next_cursor
    logger.info("%s %s: %d bars written", symbol, timeframe, written)
    return written


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    p.add_argument("--timeframes", default="1h,1m")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--catalog", default="data/nautilus_catalog")
    p.add_argument("--lake", default="data/analytics_lake")
    args = p.parse_args()

    catalog = NautilusCatalogManager(args.catalog)
    coordinator = TwoStoreCoordinator(nautilus_manager=catalog, analytics_writer=AnalyticsLakeWriter(args.lake),
                                      pg_pool=None)
    total = 0
    async with BinanceRestClient() as rest:
        for symbol in [s.strip().upper() for s in args.symbols.split(",")]:
            meta = (await rest.get_exchange_info(symbol))[0]
            precisions = {"price_precision": meta["price_precision"], "size_precision": meta["size_precision"]}
            for tf in [t.strip() for t in args.timeframes.split(",")]:
                total += await backfill(rest, coordinator, catalog, symbol, tf, args.days, precisions)
    logger.info("backfill done: %d bars written", total)


if __name__ == "__main__":
    asyncio.run(main())
