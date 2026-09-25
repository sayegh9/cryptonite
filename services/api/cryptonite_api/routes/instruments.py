"""Instruments metadata route."""

import math
import logging
import os
from typing import Any

import httpx

from cryptonite_api.routes.auth import OPERATOR_WRITE, require_operator, require_csrf
from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
import asyncpg

from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter

from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_storage.coordinator import TwoStoreCoordinator
from nautilus_trader.model import BarType

from cryptonite_schemas.identifiers import to_spot_instrument_id
from cryptonite_schemas.bar import RawBinanceKline

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])
logger = logging.getLogger(__name__)

class OnboardRequest(BaseModel):
    symbol: str

@router.get("")
async def get_instruments(request: Request) -> list[dict[str, Any]]:
    """List active trading instruments with precision and metadata."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT instrument_id, raw_symbol, venue, market_type, base_currency,
               quote_currency, price_precision, size_precision, price_increment,
               size_increment, min_notional, status, is_active
        FROM instruments
        ORDER BY raw_symbol ASC;
        """
    )

    return [
        {
            "instrument_id": r["instrument_id"],
            "symbol": r["raw_symbol"],
            "venue": r["venue"],
            "market_type": r["market_type"],
            "base_currency": r["base_currency"],
            "quote_currency": r["quote_currency"],
            "price_precision": r["price_precision"],
            "size_precision": r["size_precision"],
            "price_increment": str(r["price_increment"]),
            "size_increment": str(r["size_increment"]),
            "min_notional": str(r["min_notional"]) if r["min_notional"] is not None else None,
            "status": r["status"],
            "is_active": r["is_active"],
        }
        for r in rows
    ]


async def run_onboarding(symbol: str, pool: asyncpg.Pool, redis):
    try:
        sym = symbol.upper()
        # 1. Fetch Exchange Info
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.binance.com/api/v3/exchangeInfo?symbol={sym}",
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            resp.raise_for_status()
            data = resp.json()

        if "symbols" not in data or not data["symbols"]:
            logger.error(f"Symbol {sym} not found on Binance")
            return

        info = data["symbols"][0]
        base = info["baseAsset"]
        quote = info["quoteAsset"]
        inst_id = f"{sym}.BINANCE"

        price_filter = next((f for f in info["filters"] if f["filterType"] == "PRICE_FILTER"), None)
        tick_size = float(price_filter["tickSize"]) if price_filter else 0.01
        price_prec = max(0, -int(math.log10(tick_size))) if tick_size > 0 else 2

        lot_filter = next((f for f in info["filters"] if f["filterType"] == "LOT_SIZE"), None)
        step_size = float(lot_filter["stepSize"]) if lot_filter else 0.01
        size_prec = max(0, -int(math.log10(step_size))) if step_size > 0 else 2

        notional_filter = next((f for f in info["filters"] if f["filterType"] == "NOTIONAL"), None)
        min_notional = float(notional_filter["minNotional"]) if notional_filter else 10.0

        # 2. Insert into DB
        await pool.execute(
            """
            INSERT INTO instruments (
                instrument_id, raw_symbol, venue, market_type,
                base_currency, quote_currency,
                price_precision, size_precision,
                price_increment, size_increment,
                min_notional, status, is_active
            ) VALUES ($1, $2, 'BINANCE', 'SPOT', $3, $4, $5, $6, $7, $8, $9, 'TRADING', true)
            ON CONFLICT (instrument_id) DO NOTHING
            """,
            inst_id, sym, base, quote, price_prec, size_prec, tick_size, step_size, min_notional
        )

        # 3. Analytics Lake backfill (last 1000 1m bars)
        analytics_dir = os.environ.get("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")

        async with httpx.AsyncClient(timeout=15.0) as client:
            klines_resp = await client.get(
                f"https://api.binance.com/api/v3/klines",
                params={"symbol": sym, "interval": "1m", "limit": 1000},
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            klines_resp.raise_for_status()
            klines_data = klines_resp.json()

        writer = AnalyticsLakeWriter(analytics_dir)
        nautilus_dir = os.environ.get("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")
        naut_mgr = NautilusCatalogManager(nautilus_dir)
        coord = TwoStoreCoordinator(naut_mgr, writer, pool)

        bars = []
        from cryptonite_schemas.bar import EnrichedBar
        from decimal import Decimal
        for k in klines_data:
            open_d = Decimal(str(k[1]))
            high_d = Decimal(str(k[2]))
            low_d = Decimal(str(k[3]))
            close_d = Decimal(str(k[4]))
            vol_d = Decimal(str(k[5]))
            bars.append(EnrichedBar(
                instrument_id=inst_id,
                bar_type=f"{inst_id}-1-MINUTE-LAST-EXTERNAL",
                ts_event_ns=int(k[0]) * 1_000_000,
                ts_init_ns=int(k[0]) * 1_000_000,
                received_at_ns=int(k[6]) * 1_000_000,
                price_precision=price_prec,
                size_precision=size_prec,
                open=open_d,
                high=high_d,
                low=low_d,
                close=close_d,
                volume=vol_d,
                quote_volume=Decimal(str(k[7])),
                taker_buy_base_volume=Decimal(str(k[9])),
                taker_buy_quote_volume=Decimal(str(k[10])),
                trades_count=int(k[8]),
                open_raw=str(int(open_d * (10**price_prec))),
                high_raw=str(int(high_d * (10**price_prec))),
                low_raw=str(int(low_d * (10**price_prec))),
                close_raw=str(int(close_d * (10**price_prec))),
                volume_raw=str(int(vol_d * (10**size_prec)))
            ))

        if bars:
            bt = BarType.from_str(f"{inst_id}-1-MINUTE-LAST-EXTERNAL")
            await coord.commit_batch(
                bars=bars,
                bar_type=bt,
                venue="BINANCE",
                market_type="SPOT",
                symbol=sym,
                timeframe="1m"
            )

        # 4. Notify Redis
        if redis:
            await redis.publish("CHANNEL_INSTRUMENTS_UPDATED", sym)

        logger.info(f"Successfully onboarded {sym}")

    except Exception as e:
        logger.error(f"Error onboarding {symbol}: {e}")

class BackfillRequest(BaseModel):
    instrument_id: str
    timeframe: str
    lookback_days: int = Field(ge=1, le=365)

async def run_backfill(instrument_id: str, timeframe: str, lookback_days: int, pool: asyncpg.Pool):
    try:
        if not instrument_id.endswith(".BINANCE"):
            logger.error("Only BINANCE instruments supported for backfill")
            return
            
        sym = instrument_id.split(".")[0]
        
        # Get precisions from DB
        row = await pool.fetchrow(
            "SELECT price_precision, size_precision FROM instruments WHERE instrument_id = $1", 
            instrument_id
        )
        if not row:
            logger.error(f"Instrument {instrument_id} not found in DB")
            return
            
        price_prec = row["price_precision"]
        size_prec = row["size_precision"]
        
        analytics_dir = os.environ.get("ANALYTICS_LAKE_PATH", "/app/data/analytics_lake")
        writer = AnalyticsLakeWriter(analytics_dir)
        nautilus_dir = os.environ.get("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")
        naut_mgr = NautilusCatalogManager(nautilus_dir)
        coord = TwoStoreCoordinator(naut_mgr, writer, pool)
        
        import time
        from cryptonite_schemas.bar import EnrichedBar
        from decimal import Decimal
        
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (lookback_days * 24 * 3600 * 1000)
        
        # Add 30% for warmup padding
        warmup_ms = max((lookback_days * 24 * 3600 * 1000) * 0.3, 4 * 3600 * 1000)
        start_ms = int(start_ms - warmup_ms)
        
        timeframe_map = {
            "1m": ("1-MINUTE", 60 * 1000),
            "5m": ("5-MINUTE", 5 * 60 * 1000),
            "15m": ("15-MINUTE", 15 * 60 * 1000),
            "30m": ("30-MINUTE", 30 * 60 * 1000),
            "1h": ("1-HOUR", 60 * 60 * 1000),
            "4h": ("4-HOUR", 4 * 60 * 60 * 1000),
            "1d": ("1-DAY", 24 * 60 * 60 * 1000),
        }
        
        if timeframe not in timeframe_map:
            logger.error(f"Unsupported timeframe {timeframe}")
            return
            
        bar_suffix, step_ms = timeframe_map[timeframe]
        bt_str = f"{instrument_id}-{bar_suffix}-LAST-EXTERNAL"
        bt = BarType.from_str(bt_str)
        
        current_start = start_ms
        total_bars = 0
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            while current_start < now_ms:
                # Fetch up to 1000 bars
                klines_resp = await client.get(
                    f"https://api.binance.com/api/v3/klines",
                    params={"symbol": sym, "interval": timeframe, "limit": 1000, "startTime": current_start, "endTime": now_ms},
                    headers={'User-Agent': 'Mozilla/5.0'},
                )
                if klines_resp.status_code != 200:
                    logger.error(f"Binance API error: {klines_resp.text}")
                    break
                    
                klines_data = klines_resp.json()
                if not klines_data:
                    break
                    
                bars = []
                for k in klines_data:
                    open_d = Decimal(str(k[1]))
                    high_d = Decimal(str(k[2]))
                    low_d = Decimal(str(k[3]))
                    close_d = Decimal(str(k[4]))
                    vol_d = Decimal(str(k[5]))
                    
                    bars.append(EnrichedBar(
                        instrument_id=instrument_id,
                        bar_type=bt_str,
                        ts_event_ns=int(k[0]) * 1_000_000,
                        ts_init_ns=int(k[0]) * 1_000_000,
                        received_at_ns=int(k[6]) * 1_000_000,
                        price_precision=price_prec,
                        size_precision=size_prec,
                        open=open_d,
                        high=high_d,
                        low=low_d,
                        close=close_d,
                        volume=vol_d,
                        quote_volume=Decimal(str(k[7])),
                        taker_buy_base_volume=Decimal(str(k[9])),
                        taker_buy_quote_volume=Decimal(str(k[10])),
                        trades_count=int(k[8]),
                        open_raw=str(int(open_d * (10**price_prec))),
                        high_raw=str(int(high_d * (10**price_prec))),
                        low_raw=str(int(low_d * (10**price_prec))),
                        close_raw=str(int(close_d * (10**price_prec))),
                        volume_raw=str(int(vol_d * (10**size_prec)))
                    ))
                
                if bars:
                    await coord.commit_batch(
                        bars=bars,
                        bar_type=bt,
                        venue="BINANCE",
                        market_type="SPOT",
                        symbol=sym,
                        timeframe=timeframe
                    )
                    total_bars += len(bars)
                
                last_time = int(klines_data[-1][0])
                if last_time >= now_ms or len(klines_data) < 1000:
                    break
                    
                current_start = last_time + 1
                await import_asyncio().sleep(0.1) # Rate limit protection

        logger.info(f"Successfully backfilled {total_bars} bars for {instrument_id} {timeframe}")

    except Exception as e:
        logger.error(f"Error backfilling {instrument_id}: {e}", exc_info=True)

def import_asyncio():
    import asyncio
    return asyncio

@router.post("/onboard", dependencies=OPERATOR_WRITE)
async def onboard_instrument(req: OnboardRequest, request: Request, background_tasks: BackgroundTasks):
    pool = request.app.state.pool
    redis = getattr(request.app.state.stream_hub, 'redis', None) if hasattr(request.app.state, 'stream_hub') else None
    
    background_tasks.add_task(run_onboarding, req.symbol, pool, redis)
    return {"status": "ok", "message": f"Onboarding {req.symbol}..."}

@router.post("/backfill")
async def backfill_instrument(req: BackfillRequest, request: Request, background_tasks: BackgroundTasks, _op=Depends(require_operator), _csrf=Depends(require_csrf)):
    pool = request.app.state.pool
    background_tasks.add_task(run_backfill, req.instrument_id, req.timeframe, req.lookback_days, pool)
    return {"status": "ok", "message": f"Backfilling {req.instrument_id} ({req.timeframe}) for {req.lookback_days} days..."}
