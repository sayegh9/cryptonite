"""Remediation ops backend: enforced limits (R1.8), heartbeat alerts (R7.5), the TOTP key rule (R7.10)
and migration 033 (R7.4). No model, no dev DB: fakes for the API/Redis, a throwaway DB for the migration."""

import datetime
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from cryptonite_api import auth as api_auth
from cryptonite_api.main import create_app
from cryptonite_api.routes.auth import COOKIE_NAME
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_orchestrator import ops_monitor
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
CSRF = "csrf-test-token"
SID = "sid-operator"


class FakeRedis:
    def __init__(self, kv=None):
        self.kv = dict(kv or {})
        self.ex = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self.kv[key] = value
        self.ex[key] = ex
        return True

    publish = AsyncMock()


# --- R1.8: orchestrator publishes what its dispatcher enforces ---------------------------------------

def test_effective_limits_come_from_the_dispatcher():
    engine = PaperTradingEngine(account_id="PAPER-ACCOUNT-001", trader_id="T-001", engine_run_id="RUN-test",
                                pg_pool=None, redis_client=None, starting_cash=Decimal("100000"))
    now = datetime.datetime(2026, 10, 5, 1, 0, tzinfo=datetime.UTC)
    res = ops_monitor.effective_limits(engine.dispatcher, now)
    assert res["as_of"] == "2026-10-05T01:00:00Z"
    assert res["paper"] == {"max_daily_loss_pct": "5", "min_order_equity_pct": "1", "max_order_equity_pct": "25",
                  "taker_fee_bps": "10", "account_backstop_drawdown_pct": "20", "cross_mandate_exposure_pct": "25"}
    d = SimpleNamespace(max_daily_loss_pct=Decimal("0.035"), min_order_equity_pct=Decimal("0.01"),
                        max_position_concentration_pct=Decimal("0.2"), taker_fee=Decimal("0.00075"))
    paper = ops_monitor.effective_limits(d, now)["paper"]
    assert (paper["max_daily_loss_pct"], paper["max_order_equity_pct"], paper["taker_fee_bps"]) == ("3.5", "20", "7.5")


async def test_publish_sets_the_key_with_a_ttl():
    redis = FakeRedis()
    engine = SimpleNamespace(dispatcher=SimpleNamespace(max_daily_loss_pct=Decimal("0.05"), min_order_equity_pct=Decimal("0.01"),
                                                        max_position_concentration_pct=Decimal("0.25"), taker_fee=Decimal("0.001")))
    await ops_monitor.publish_effective_limits(engine, redis)
    assert json.loads(redis.kv["risk:effective_limits"])["paper"]["max_order_equity_pct"] == "25"
    assert redis.ex["risk:effective_limits"] == ops_monitor.EFFECTIVE_LIMITS_TTL_S


# --- R1.8: API endpoints --------------------------------------------------------------------------

class FakeAuth:
    def __init__(self):
        self.audit = []

    async def get_session(self, sid):
        return {"user_id": "u-op", "username": "op", "role": "operator", "csrf_token": CSRF} if sid == SID else None

    async def verify_operator_step_up(self, username, password):
        return password == "right"

    async def log_audit_event(self, **kw):
        self.audit.append(kw)
        return 1


@pytest.fixture
def api():
    app = create_app()
    app.state.auth_manager = FakeAuth()
    app.state.stream_hub = SimpleNamespace(redis=FakeRedis())
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, SID)
    return app, client


def test_enforced_limits_published(api):
    app, client = api
    published = {"as_of": "2026-10-05T01:00:00Z", "paper": {"max_daily_loss_pct": "5", "min_order_equity_pct": "1",
                 "max_order_equity_pct": "25", "taker_fee_bps": "10", "account_backstop_drawdown_pct": "20"}}
    app.state.stream_hub.redis.kv["risk:effective_limits"] = json.dumps(published).encode()
    res = client.get("/api/v1/control/enforced-limits")
    assert res.status_code == 200
    data = res.json()
    assert data["as_of"] == published["as_of"]
    assert data["paper"] == published["paper"]


@pytest.mark.parametrize("raw", [None, "not json", json.dumps({"as_of": "x", "paper": None})])
def test_enforced_limits_unknown_when_not_published(api, raw):
    app, client = api
    if raw is not None:
        app.state.stream_hub.redis.kv["risk:effective_limits"] = raw
    assert client.get("/api/v1/control/enforced-limits").json() == {"as_of": None, "paper": None}


def test_enforced_limits_need_an_operator(api):
    _, client = api
    client.cookies.clear()
    assert client.get("/api/v1/control/enforced-limits").status_code == 401


def test_risk_limits_edit_needs_csrf_and_valid_values(api):
    """F7 replaced the R1.8 501: the route keeps its auth and CSRF; refused values change nothing
    (the full flow on a real DB is in test_risk_limits_f7.py)."""
    app, client = api
    body = {"max_daily_loss_pct": "2.5", "reason": "tighten for the review"}
    assert client.post("/api/v1/control/risk-limits", json=body).status_code == 403  # no CSRF
    client.cookies.clear()
    assert client.post("/api/v1/control/risk-limits", json=body, headers={"X-CSRF-Token": CSRF}).status_code == 401
    assert all(e["action"] != "RISK_LIMITS_UPDATE" for e in app.state.auth_manager.audit)


# --- R7.5: heartbeat alerts -------------------------------------------------------------------------

class FakeConn:
    def __init__(self):
        self.rows = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        self.rows.append(args)


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_heartbeat_states_unknown_vs_down():
    now = 1_000_000.0
    redis = FakeRedis({"learner:heartbeat": "{}", "local_tier:health": json.dumps({"healthy": False}),
                       "sentiment:last_ok": str(int(now - 10))})
    assert await ops_monitor.heartbeat_states(redis, now) == {
        "learner": True, "exit_monitor": False, "local_tier": False, "sentiment": True}
    assert (await ops_monitor.heartbeat_states(FakeRedis(), now))["sentiment"] is None  # never published
    assert (await ops_monitor.heartbeat_states(FakeRedis(), now))["local_tier"] is None
    stale = FakeRedis({"sentiment:last_ok": str(int(now - ops_monitor.SENTIMENT_STALE_S - 1))})
    assert (await ops_monitor.heartbeat_states(stale, now))["sentiment"] is False


async def test_heartbeat_down_alerts_once_after_grace_then_recovers():
    pool, redis = FakePool(), FakeRedis({"learner:heartbeat": "{}", "exit_monitor:heartbeat": "{}"})
    watch = ops_monitor.HeartbeatWatch(grace_s=300)
    assert await ops_monitor.check_heartbeats(pool, redis, watch, now_s=1000) == []
    del redis.kv["exit_monitor:heartbeat"]
    assert await ops_monitor.check_heartbeats(pool, redis, watch, now_s=1060) == []  # inside the grace
    assert await ops_monitor.check_heartbeats(pool, redis, watch, now_s=1400) == [("exit_monitor", "down")]
    assert await ops_monitor.check_heartbeats(pool, redis, watch, now_s=1460) == []  # once per outage
    tier, kind, _title, _body, payload, dedupe = pool.conn.rows[-1][5], *pool.conn.rows[-1][:5]
    assert (tier, kind, dedupe) == ("CRITICAL", "HEARTBEAT_DOWN", "heartbeat:exit_monitor:down:1060")
    assert json.loads(payload)["heartbeat"] == "exit_monitor"
    redis.kv["exit_monitor:heartbeat"] = "{}"
    assert await ops_monitor.check_heartbeats(pool, redis, watch, now_s=1500) == [("exit_monitor", "recovered")]
    assert pool.conn.rows[-1][5] == "INFO"
    assert len(pool.conn.rows) == 2


def test_notifier_does_not_double_send_heartbeat_alerts():
    sys.path.insert(0, str(REPO / "services" / "notifier"))
    from cryptonite_notifier.bot import OUTBOX_KINDS
    assert "HEARTBEAT_DOWN" in OUTBOX_KINDS  # it reaches Telegram through notifications_outbox


# --- R7.10: TOTP key -----------------------------------------------------------------------------

def test_totp_key_fallback_only_in_dev_or_test(monkeypatch, tmp_path):
    monkeypatch.setattr(api_auth, "DEFAULT_SECRET_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CRYPTONITE_TOTP_AES_KEY", raising=False)
    for env in ("test", "dev", "DEV"):
        monkeypatch.setenv("CRYPTONITE_ENV", env)
        assert api_auth.get_totp_aes_key() == api_auth.FALLBACK_DEV_KEY
    for env in (None, "prod", "paper", ""):
        if env is None:
            monkeypatch.delenv("CRYPTONITE_ENV", raising=False)
        else:
            monkeypatch.setenv("CRYPTONITE_ENV", env)
        with pytest.raises(RuntimeError, match="TOTP AES key"):
            api_auth.get_totp_aes_key()
    monkeypatch.setenv("CRYPTONITE_TOTP_AES_KEY", "k" * 32)  # a real key works everywhere
    assert api_auth.get_totp_aes_key() == b"k" * 32
    monkeypatch.setenv("CRYPTONITE_TOTP_AES_KEY", "too-short")
    with pytest.raises(RuntimeError):
        api_auth.get_totp_aes_key()


# --- R7.4: migration 033 ---------------------------------------------------------------------------

def _alembic(url: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)


async def _trace_columns(url: str) -> set[str]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("""SELECT table_name FROM information_schema.columns
                                   WHERE column_name = 'decision_trace_id' AND table_schema = 'public'""")
        return {r["table_name"] for r in rows}
    finally:
        await conn.close()


def test_migration_033_up_and_down():
    import asyncio

    from migrated_db import migrated_database

    with migrated_database("cryptonite_m033") as url:
        assert asyncio.run(_trace_columns(url)) == {"execution_commands", "debate_runs"}
        _alembic(url, "downgrade", "032")
        assert asyncio.run(_trace_columns(url)) == set()
        _alembic(url, "upgrade", "head")
        assert asyncio.run(_trace_columns(url)) == {"execution_commands", "debate_runs"}
