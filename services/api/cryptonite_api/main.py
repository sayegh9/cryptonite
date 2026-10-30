"""FastAPI application entry point for Cryptonite API and Stream Gateway."""

import asyncio
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
    agents,
    ai,
    alerts,
    auth,
    backtests,
    bars,
    brain,
    control,
    instruments,
    market,
    paper,
    profiles,
    research,
    scanners,
    settings,
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
    
    # Start background alert evaluator
    from cryptonite_api.routes.alerts import run_alert_evaluator
    app.state.alert_task = asyncio.create_task(run_alert_evaluator(app.state))
    
    app.state.auth_manager = OperatorAuthManager(
        db_pool=app.state.pool,
        redis_client=app.state.stream_hub.redis,
    )
    from cryptonite_api.auth_policy import run_auth_policy
    app.state.auth_policy_task = asyncio.create_task(run_auth_policy(app.state))

    # Ensure default admin operator exists in dev/demo environments
    env = os.getenv("ENVIRONMENT", "DEV").upper()
    if env not in ("PROD", "PRODUCTION"):
        try:
            async with app.state.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id FROM operator_users WHERE username = 'admin'")
                if not row:
                    logger.info("Auto-seeding default admin operator user...")
                    import pyotp
                    password_hash = app.state.auth_manager.hash_password("admin123")
                    totp_secret = pyotp.random_base32()
                    ciphertext, nonce, auth_tag = app.state.auth_manager.encrypt_totp_secret(totp_secret)
                    await conn.execute(
                        """
                        INSERT INTO operator_users (
                            username, password_hash, totp_secret_ciphertext, totp_secret_nonce,
                            totp_secret_auth_tag, totp_key_version, role, is_active
                        ) VALUES ($1, $2, $3, $4, $5, 'v1', 'admin', true)
                        ON CONFLICT (username) DO NOTHING;
                        """,
                        "admin", password_hash, ciphertext, nonce, auth_tag,
                    )
                    logger.info("Default admin operator user auto-seeded successfully.")
        except Exception as e:
            logger.warning(f"Could not auto-seed admin operator user: {e}")

    # Seed built-in scanner presets if database is fresh
    try:
        from cryptonite_api.routes.scanners import ensure_presets_seeded
        await ensure_presets_seeded(app.state.pool)
    except Exception as e:
        logger.warning(f"Could not seed scanner presets: {e}")

    yield

    logger.info("Shutting down API Gateway resources...")
    # Stop alert evaluator
    if hasattr(app.state, 'alert_task') and app.state.alert_task:
        app.state.alert_task.cancel()
        try:
            await app.state.alert_task
        except asyncio.CancelledError:
            pass
    if hasattr(app.state, 'auth_policy_task') and app.state.auth_policy_task:
        app.state.auth_policy_task.cancel()
        try:
            await app.state.auth_policy_task
        except asyncio.CancelledError:
            pass

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
    app.include_router(settings.router)
    app.include_router(control.router)
    app.include_router(bars.router)
    app.include_router(instruments.router)
    app.include_router(scanners.router)
    app.include_router(signals.router)
    app.include_router(strategies.router)
    app.include_router(backtests.router)
    app.include_router(paper.router)
    app.include_router(ws.router)
    
    from cryptonite_api.routes import strategist
    app.include_router(strategist.router)
    app.include_router(market.router)
    app.include_router(alerts.router)
    app.include_router(agents.router)
    app.include_router(brain.router)
    app.include_router(ai.router)
    app.include_router(profiles.router)
    app.include_router(research.router)

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
