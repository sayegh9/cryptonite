"""Command-line interface for Cryptonite data ingestion and catalog management."""

import asyncio
import logging
import os
import time

import click
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_schemas.identifiers import (
    to_nautilus_bar_type,
    to_nautilus_instrument_id,
    to_spot_instrument_id,
)
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.coordinator import TwoStoreCoordinator
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.queries import AnalyticsQueryEngine
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from cryptonite_ingest.client import BinanceRestClient
from cryptonite_ingest.recorder import IngestRecorder
from cryptonite_ingest.server import HealthServer

load_dotenv()

console = Console()

DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
)
DEFAULT_NAUTILUS_DIR = os.getenv("NAUTILUS_CATALOG_PATH", "data/nautilus_catalog")
DEFAULT_ANALYTICS_DIR = os.getenv("ANALYTICS_LAKE_PATH", "data/analytics_lake")


@click.group()
def cli():
    """Cryptonite Ingest & Catalog CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@cli.command("sync-symbols")
@click.option("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT", help="Comma-separated symbols")
@click.option("--catalog-dir", default=DEFAULT_NAUTILUS_DIR, help="Nautilus catalog directory")
@click.option("--db-url", default=DEFAULT_DB_URL, help="Database URL")
def sync_symbols_cmd(symbols: str, catalog_dir: str, db_url: str):
    """Sync Binance Spot symbol metadata to DB and Nautilus catalog."""
    async def _run():
        sym_list = [s.strip().upper() for s in symbols.split(",")]
        recorder = IngestRecorder(
            symbols=sym_list,
            timeframes=["1m"],
            nautilus_catalog_path=catalog_dir,
            analytics_lake_path=DEFAULT_ANALYTICS_DIR,
            database_url=db_url,
        )
        try:
            if recorder.database_url:
                import asyncpg
                recorder.pool = await asyncpg.create_pool(recorder.database_url, min_size=1, max_size=3)
            await recorder.sync_instruments()
            console.print(f"[green]Successfully synced {len(sym_list)} symbols to {catalog_dir} and database.[/green]")
        finally:
            if recorder.pool:
                await recorder.pool.close()

    asyncio.run(_run())


@cli.command("backfill")
@click.option("--symbol", default="BTCUSDT", help="Symbol to backfill (e.g. BTCUSDT)")
@click.option("--timeframe", default="1h", help="Timeframe (1m, 1h, 1d)")
@click.option("--days", default=2, type=int, help="Number of historical days to backfill")
@click.option("--catalog-dir", default=DEFAULT_NAUTILUS_DIR, help="Nautilus catalog directory")
@click.option("--analytics-dir", default=DEFAULT_ANALYTICS_DIR, help="Analytics lake directory")
@click.option("--db-url", default=DEFAULT_DB_URL, help="Database URL")
def backfill_cmd(symbol: str, timeframe: str, days: int, catalog_dir: str, analytics_dir: str, db_url: str):
    """Download historical bars, convert to exact decimal models, and execute two-store commit."""
    async def _run():
        sym = symbol.strip().upper()
        inst_id = to_spot_instrument_id(sym)
        bar_type = to_nautilus_bar_type(inst_id, timeframe)

        console.print(f"[cyan]Starting backfill for {inst_id} ({timeframe}) for past {days} days...[/cyan]")
        
        # 1. Fetch symbol metadata
        async with BinanceRestClient() as client:
            all_meta = await client.get_exchange_info(sym)
            if not all_meta:
                console.print(f"[red]Could not find symbol {sym} on Binance[/red]")
                return
            meta = all_meta[0]

            # 2. Write CurrencyPair instrument first!
            nautilus_mgr = NautilusCatalogManager(catalog_dir)
            analytics_writer = AnalyticsLakeWriter(analytics_dir)

            import asyncpg
            pool = None
            try:
                pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
            except Exception as e:
                console.print(f"[yellow]Running backfill without Postgres database: {e}[/yellow]")

            coordinator = TwoStoreCoordinator(
                nautilus_manager=nautilus_mgr,
                analytics_writer=analytics_writer,
                pg_pool=pool,
            )

            # Sync instrument
            from nautilus_trader.model import Currency, CurrencyPair, Price, Quantity
            n_inst_id = to_nautilus_instrument_id(inst_id)
            instrument = CurrencyPair(
                instrument_id=n_inst_id,
                raw_symbol=n_inst_id.symbol,
                base_currency=Currency.from_str(meta["base_asset"]),
                quote_currency=Currency.from_str(meta["quote_asset"]),
                price_precision=meta["price_precision"],
                size_precision=meta["size_precision"],
                price_increment=Price(meta["price_increment"], meta["price_precision"]),
                size_increment=Quantity(meta["size_increment"], meta["size_precision"]),
                ts_event=time.time_ns(),
                ts_init=time.time_ns(),
            )
            nautilus_mgr.write_instruments([instrument])

            # 3. Calculate start time
            now_ms = int(time.time() * 1000)
            start_ms = now_ms - (days * 86400 * 1000)

            # 4. Fetch klines
            raw_klines = await client.fetch_klines(
                symbol=sym,
                interval=timeframe,
                start_time_ms=start_ms,
                end_time_ms=now_ms,
                limit=1000,
            )
            raw_klines = [k for k in raw_klines if k.is_closed]  # drop the still-forming last bar
            console.print(f"Fetched {len(raw_klines)} closed historical bars from Binance.")

            if not raw_klines:
                return

            arrival_ns = time.time_ns()
            enriched_bars = [
                EnrichedBar.from_binance_raw(
                    raw=k,
                    instrument_id=inst_id,
                    bar_type=str(bar_type),
                    price_precision=meta["price_precision"],
                    size_precision=meta["size_precision"],
                    received_at_ns=arrival_ns,
                )
                for k in raw_klines
            ]

            # 5. Commit through coordinator
            batch_id = await coordinator.commit_batch(
                bars=enriched_bars,
                bar_type=bar_type,
                venue="BINANCE",
                market_type="SPOT",
                symbol=sym,
                timeframe=timeframe,
            )
            console.print(f"[green]Committed batch {batch_id} with {len(enriched_bars)} bars to both catalogs.[/green]")

            if pool:
                await pool.close()

    asyncio.run(_run())


@cli.command("record")
@click.option("--symbols", default="BTCUSDT,ETHUSDT", help="Comma-separated symbols")
@click.option("--timeframes", default="1m", help="Comma-separated timeframes")
@click.option("--duration", default=0, type=int, help="Duration in seconds (0 for indefinite)")
@click.option("--catalog-dir", default=DEFAULT_NAUTILUS_DIR, help="Nautilus catalog directory")
@click.option("--analytics-dir", default=DEFAULT_ANALYTICS_DIR, help="Analytics lake directory")
@click.option("--db-url", default=DEFAULT_DB_URL, help="Database URL")
@click.option("--port", default=8080, type=int, help="Health server port")
def record_cmd(symbols: str, timeframes: str, duration: int, catalog_dir: str, analytics_dir: str, db_url: str, port: int):
    """Run live WebSocket recorder with dual-store commit and health endpoints."""
    async def _run():
        sym_list = [s.strip().upper() for s in symbols.split(",")]
        tf_list = [t.strip().lower() for t in timeframes.split(",")]

        recorder = IngestRecorder(
            symbols=sym_list,
            timeframes=tf_list,
            nautilus_catalog_path=catalog_dir,
            analytics_lake_path=analytics_dir,
            database_url=db_url,
        )

        health_server = HealthServer(recorder=recorder, port=port)
        await health_server.start()

        console.print(f"[green]Starting live recorder for {sym_list} ({tf_list}) on port {port}...[/green]")

        recorder_task = asyncio.create_task(recorder.start())

        if duration > 0:
            console.print(f"Running for {duration} seconds...")
            await asyncio.sleep(duration)
            await recorder.stop()
            await health_server.stop()
        else:
            try:
                await recorder_task
            except (KeyboardInterrupt, asyncio.CancelledError):
                await recorder.stop()
                await health_server.stop()

    asyncio.run(_run())


@cli.command("query")
@click.option("--symbol", default="BTCUSDT", help="Symbol to query")
@click.option("--timeframe", default="1h", help="Timeframe")
@click.option("--analytics-dir", default=DEFAULT_ANALYTICS_DIR, help="Analytics lake directory")
def query_cmd(symbol: str, timeframe: str, analytics_dir: str):
    """Query Enriched Analytics Lake using DuckDB and display summary."""
    engine = AnalyticsQueryEngine(analytics_dir)
    count = engine.count_bars(symbol, timeframe)
    min_ts, max_ts = engine.get_timestamp_range(symbol, timeframe)
    gaps = engine.detect_gaps(symbol, timeframe)

    table = Table(title=f"Analytics Lake Status: {symbol} ({timeframe})")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Total Bars", str(count))
    if min_ts and max_ts:
        import datetime
        dt_start = datetime.datetime.fromtimestamp(min_ts / 1_000_000_000, tz=datetime.UTC)
        dt_end = datetime.datetime.fromtimestamp(max_ts / 1_000_000_000, tz=datetime.UTC)
        table.add_row("First Bar UTC", str(dt_start))
        table.add_row("Last Bar UTC", str(dt_end))
    table.add_row("Gaps Detected", str(len(gaps)))

    console.print(table)


if __name__ == "__main__":
    cli()
