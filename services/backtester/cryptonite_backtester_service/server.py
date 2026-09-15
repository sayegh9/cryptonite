"""FastAPI HTTP service for Backtester daemon health and readiness probes."""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import uvicorn
from cryptonite_backtester_service.worker import BacktestWorker
from fastapi import FastAPI, Response, status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cryptonite.backtester.server")

worker: BacktestWorker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global worker
    worker = BacktestWorker()
    await worker.start()
    logger.info("Backtester worker started on HTTP service initialization")
    try:
        yield
    finally:
        if worker:
            await worker.stop()
            logger.info("Backtester worker stopped on HTTP service shutdown")


app = FastAPI(
    title="Cryptonite Backtest Worker Service",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def readiness_check(response: Response) -> dict[str, Any]:
    global worker
    if not worker or not worker._running or not worker.pool:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready", "reasons": ["worker_or_db_not_running"]}

    # Check catalog path readability
    if not worker.catalog_path.exists():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready", "reasons": ["catalog_path_unreadable"]}

    try:
        async with worker.pool.acquire() as conn:
            failed_outbox = await conn.fetchval(
                "SELECT count(*) FROM outbox_events WHERE status = 'FAILED' AND producer = 'cryptonite-backtester'"
            )
            integrity_failed = await conn.fetchval(
                "SELECT count(*) FROM backtest_runs WHERE status = 'INTEGRITY_FAILED'"
            )

        reasons = []
        if failed_outbox > 0:
            reasons.append(f"dead_letter_outbox_events_present ({failed_outbox})")
        if integrity_failed > 0:
            reasons.append(f"integrity_incidents_present ({integrity_failed})")

        if reasons:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "unready",
                "reasons": reasons,
                "failed_outbox_count": failed_outbox,
                "integrity_incident_count": integrity_failed,
            }

        return {
            "status": "ready",
            "worker_id": worker.worker_id,
            "completed_count": worker.completed_count,
            "failed_count": worker.failed_count,
        }
    except Exception as e:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unready", "reasons": [f"db_query_error: {e}"]}


def main() -> None:
    port = int(os.getenv("PORT", "8083"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
