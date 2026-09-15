"""Instruments metadata route."""

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])


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
