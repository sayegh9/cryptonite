"""Phase 5 API test harness: the real FastAPI app on a throwaway migrated database, with the real
OperatorAuthManager (Redis sessions, AES-encrypted TOTP secrets) and an operator who has TOTP.

Requests go through ``httpx.ASGITransport`` on the test's own event loop, so the app shares the
test's asyncpg pool.
"""

import json
import os
import uuid
from dataclasses import dataclass

import asyncpg
import httpx
import pyotp
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.main import create_app
from cryptonite_api.routes.auth import COOKIE_NAME
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse
from migrated_db import seed_paper_account

RealAsyncClient = httpx.AsyncClient  # captured before any test monkeypatches httpx.AsyncClient
ACCOUNT = "PAPER-ACCOUNT-001"


class Hub:
    def __init__(self, redis):
        self.redis = redis


@dataclass
class Harness:
    app: object
    pool: asyncpg.Pool
    redis: aioredis.Redis
    auth: OperatorAuthManager
    operator_id: uuid.UUID
    totp: pyotp.TOTP
    csrf: str = ""
    sid: str = ""
    username: str = ""

    def client(self) -> httpx.AsyncClient:
        c = RealAsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://api.test")
        if self.sid:
            c.cookies.set(COOKIE_NAME, self.sid)
        return c

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    async def mandate(self, level: int = 1, *, instruments=("BTCUSDT.BINANCE",), budget: int = 10000, name: str | None = None,
                      status: str = "ACTIVE", active_hours: str | None = None):
        mid = await self.pool.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status, active_hours_utc)
               VALUES ($1, $2, $3, 25, $4, $5, $6) RETURNING id""",
            name or f"m-{uuid.uuid4().hex[:6]}", budget, list(instruments), level, status, active_hours)
        async with self.pool.acquire() as conn:
            m = await load_mandate(conn, mid)
            await ensure_mandate_instance(conn, m, ACCOUNT)
        return m


async def make_harness(db_url: str, *, heartbeat: bool = True) -> Harness:
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=6)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    auth = OperatorAuthManager(db_pool=pool, redis_client=redis, totp_aes_key=os.urandom(32))
    async with pool.acquire() as conn:
        await seed_paper_account(conn)
        secret = pyotp.random_base32()
        ct, nonce, tag = auth.encrypt_totp_secret(secret)
        uid = await conn.fetchval(
            """INSERT INTO operator_users (username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            f"op-{uuid.uuid4().hex[:6]}", auth.hash_password("pw"), ct, nonce, tag)
        username = await conn.fetchval("SELECT username FROM operator_users WHERE id = $1", uid)
    app = create_app()
    app.state.pool = pool
    app.state.auth_manager = auth
    app.state.stream_hub = Hub(redis)
    app.state.redis = redis
    h = Harness(app, pool, redis, auth, uid, pyotp.TOTP(secret))
    h.sid, h.csrf = await auth.create_session(uid, username, role="operator")
    h.username = username
    if heartbeat:
        await redis.set("exit_monitor:heartbeat", json.dumps({"stop_loss_pct": 5.0, "trail_activate_pct": 3.0, "trail_pct": 2.0}), ex=300)
    await publish_test_universe(redis)
    return h


async def publish_test_universe(redis, symbols=("BTCUSDT", "ETHUSDT"), timeframes=("1m", "1h"), perps=("BTCUSDT", "ETHUSDT")):
    """What ingest publishes in the dev stack (D4); mandate and instance creation fail closed without it."""
    universe = IngestUniverse.build(list(symbols), list(timeframes), list(perps))
    await redis.set(INGEST_UNIVERSE_KEY, universe.model_dump_json(), ex=300)


async def close_harness(h: Harness) -> None:
    await h.redis.delete("exit_monitor:heartbeat", "notifier:heartbeat", INGEST_UNIVERSE_KEY)
    await h.redis.aclose()
    await h.pool.close()
