"""FastAPI application entry point for Cryptonite API and Stream Gateway."""

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg
from cryptonite_storage.queries import AnalyticsQueryEngine
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.routes import (
    auth,
    backtests,
    bars,
    control,
    instruments,
    paper,
    scanners,
    signals,
    strategies,
    ws,
)
from cryptonite_api.stream_gateway import ALLOWED_ORIGINS, StreamHub

logger = logging.getLogger("cryptonite.api")

DEFAULT_DB_URL = "postgresql://cryptonite_api:api_dev_pass@127.0.0.1:5432/cryptonite"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_LAKE_PATH = "data/analytics_lake"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager initializing DB pool, DuckDB engine, and StreamHub."""
    db_url = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    redis_url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    lake_path = Path(os.getenv("ANALYTICS_LAKE_PATH", DEFAULT_LAKE_PATH)).resolve()

    logger.info("Initializing API Gateway resources...")
    app.state.pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    app.state.query_engine = AnalyticsQueryEngine(lake_path)
    app.state.stream_hub = StreamHub(redis_url=redis_url)
    await app.state.stream_hub.start()
    app.state.auth_manager = OperatorAuthManager(
        db_pool=app.state.pool,
        redis_client=app.state.stream_hub.redis,
    )

    yield

    logger.info("Shutting down API Gateway resources...")
    await app.state.stream_hub.stop()
    if app.state.pool:
        await app.state.pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Cryptonite Market Intelligence API",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Strict CORS allow-list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(auth.router)
    app.include_router(control.router)
    app.include_router(bars.router)
    app.include_router(instruments.router)
    app.include_router(scanners.router)
    app.include_router(signals.router)
    app.include_router(strategies.router)
    app.include_router(backtests.router)
    app.include_router(paper.router)
    app.include_router(ws.router)

    start_time = time.monotonic()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "uptime_s": time.monotonic() - start_time,
            "clients_count": len(app.state.stream_hub.clients) if hasattr(app.state, "stream_hub") else 0,
        }

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        db_ok = False
        redis_ok = False
        if hasattr(app.state, "pool") and app.state.pool:
            try:
                db_ok = (await app.state.pool.fetchval("SELECT 1")) == 1
            except Exception:
                db_ok = False

        if hasattr(app.state, "stream_hub") and app.state.stream_hub.redis:
            try:
                redis_ok = bool(await app.state.stream_hub.redis.ping())
            except Exception:
                redis_ok = False

        is_ready = db_ok and redis_ok
        return {
            "status": "ready" if is_ready else "unready",
            "checks": {"postgres": db_ok, "redis": redis_ok},
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("cryptonite_api.main:app", host=host, port=port, log_level="info")
