"""User price alert CRUD and WebSocket push integration."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.api.alerts")
router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

BINANCE_BASE = "https://api.binance.com"
HEADERS = {"User-Agent": "Cryptonite/0.2.0"}


class CreateAlertRequest(BaseModel):
    instrument_id: str = Field(..., description="e.g. BTCUSDT.BINANCE")
    condition: str = Field(..., description="above or below")
    threshold: float = Field(..., gt=0)


@router.get("")
async def list_alerts(request: Request) -> list[dict[str, Any]]:
    """List all price alerts."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        SELECT id, instrument_id, condition, threshold, is_active, triggered_at, created_at
        FROM user_alerts
        ORDER BY created_at DESC
        """
    )
    return [
        {
            "id": str(r["id"]),
            "instrument_id": r["instrument_id"],
            "condition": r["condition"],
            "threshold": float(r["threshold"]),
            "is_active": r["is_active"],
            "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None,
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@router.post("", status_code=201)
async def create_alert(body: CreateAlertRequest, request: Request) -> dict[str, Any]:
    """Create a new price alert."""
    pool = request.app.state.pool
    if body.condition not in ("above", "below"):
        raise HTTPException(status_code=422, detail="condition must be 'above' or 'below'")

    # Verify instrument exists
    exists = await pool.fetchval(
        "SELECT 1 FROM instruments WHERE instrument_id = $1",
        body.instrument_id,
    )
    if not exists:
        raise HTTPException(
            status_code=404,
            detail=f"Instrument {body.instrument_id} not found. Onboard it first.",
        )

    row = await pool.fetchrow(
        """
        INSERT INTO user_alerts (instrument_id, condition, threshold)
        VALUES ($1, $2, $3)
        RETURNING id, instrument_id, condition, threshold, is_active, triggered_at, created_at
        """,
        body.instrument_id,
        body.condition,
        body.threshold,
    )
    return {
        "id": str(row["id"]),
        "instrument_id": row["instrument_id"],
        "condition": row["condition"],
        "threshold": float(row["threshold"]),
        "is_active": row["is_active"],
        "triggered_at": None,
        "created_at": row["created_at"].isoformat(),
    }


@router.delete("/{alert_id}", status_code=204)
async def delete_alert(alert_id: UUID, request: Request) -> None:
    """Delete a price alert by ID."""
    pool = request.app.state.pool
    deleted = await pool.fetchval(
        "DELETE FROM user_alerts WHERE id = $1 RETURNING id",
        alert_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Alert not found")


async def run_alert_evaluator(app_state) -> None:
    """Background task: evaluate active alerts every 15s against Binance live prices."""
    logger.info("Alert evaluator started")
    while True:
        try:
            await asyncio.sleep(15)
            pool = app_state.pool
            stream_hub = app_state.stream_hub

            active_alerts = await pool.fetch(
                """
                SELECT id, instrument_id, condition, threshold
                FROM user_alerts
                WHERE is_active = TRUE
                """
            )
            if not active_alerts:
                continue

            # Collect unique symbols
            symbols = list({r["instrument_id"].split(".")[0] for r in active_alerts})
            syms_param = json.dumps(symbols)

            try:
                async with httpx.AsyncClient(timeout=6.0, headers=HEADERS) as client:
                    resp = await client.get(
                        f"{BINANCE_BASE}/api/v3/ticker/price",
                        params={"symbols": syms_param},
                    )
                    resp.raise_for_status()
                price_map = {t["symbol"]: float(t["price"]) for t in resp.json()}
            except Exception as exc:
                logger.warning("Alert evaluator: price fetch failed: %s", exc)
                continue

            now = datetime.now(timezone.utc)
            for r in active_alerts:
                sym = r["instrument_id"].split(".")[0]
                current_price = price_map.get(sym)
                if current_price is None:
                    continue

                threshold = float(r["threshold"])
                triggered = (
                    r["condition"] == "above" and current_price >= threshold
                ) or (
                    r["condition"] == "below" and current_price <= threshold
                )

                if triggered:
                    await pool.execute(
                        "UPDATE user_alerts SET is_active = FALSE, triggered_at = $1 WHERE id = $2",
                        now,
                        r["id"],
                    )
                    # Fan out to all WS clients
                    payload = {
                        "type": "alert_triggered",
                        "alert_id": str(r["id"]),
                        "symbol": sym,
                        "condition": r["condition"],
                        "threshold": threshold,
                        "price": current_price,
                    }
                    for client_conn in list(stream_hub.clients.values()):
                        await client_conn.enqueue_lossy_tick(payload)
                    logger.info(
                        "Alert triggered: %s %s %s @ %.4f (threshold %.4f)",
                        sym, r["condition"], r["id"], current_price, threshold,
                    )

        except asyncio.CancelledError:
            logger.info("Alert evaluator stopped")
            break
        except Exception as exc:
            logger.error("Alert evaluator error: %s", exc)
