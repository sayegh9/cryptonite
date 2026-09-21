"""Instruments metadata route."""

import math
import logging
import os
from typing import Any

import httpx

from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from pydantic import BaseModel
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

@router.post("/onboard")
async def onboard_instrument(req: OnboardRequest, request: Request, background_tasks: BackgroundTasks):
    pool = request.app.state.pool
    redis = getattr(request.app.state.stream_hub, 'redis', None) if hasattr(request.app.state, 'stream_hub') else None
    
    background_tasks.add_task(run_onboarding, req.symbol, pool, redis)
    return {"status": "ok", "message": f"Onboarding {req.symbol}..."}
