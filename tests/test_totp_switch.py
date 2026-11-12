"""The operator's global TOTP switch (``auth.totp_enabled``, Settings -> Security): off means no code at sign-in and no
code for any action; switching it off needs one last code, switching it on is free, and an unreadable value means on."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pyotp
import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_api import totp_policy
from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.routes import ai as ai_route
from cryptonite_api.settings_registry import REGISTRY, Tier, health_warnings, write_needs_totp
from fastapi import HTTPException
from migrated_db import migrated_database

KEY = "auth.totp_enabled"


class _Conn:
    def __init__(self, value):
        self.value = value

    async def fetchval(self, sql, *args):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, value):
        self.conn = _Conn(value)

    def acquire(self):
        return _Acquire(self.conn)


def _request(value):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(value), stream_hub=None)))


def test_switching_totp_off_needs_a_code_and_on_is_free():
    entry = next(s for s in REGISTRY if s.key == KEY)
    assert entry.default == 1 and entry.tier is Tier.T_L
    assert write_needs_totp(entry, Decimal("1"), Decimal("0")) is True
    assert write_needs_totp(entry, Decimal("0"), Decimal("1")) is False


def test_the_settings_panel_warns_while_totp_is_off():
    common = dict(environment="PROD", dev_totp_login=False, dev_totp_approvals=False, secure_cookie=True,
                  kill_switch_active=False)
    assert [w["code"] for w in health_warnings(**common, totp_off=True)] == ["TOTP_OFF"]
    assert health_warnings(**common) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "on"), [(None, True), ('"1"', True), ('"0"', False), ('"0.5"', True), ('"7"', True), ('"abc"', True),
                                         (RuntimeError("db down"), True)])
async def test_only_a_stored_zero_turns_totp_off(raw, on):
    assert await totp_policy.totp_enabled(_Pool(raw)) is on


@pytest.mark.asyncio
async def test_with_totp_off_actions_ask_for_no_code():
    request = _request('"0"')
    assert await ai_route.require_totp(request, {"user_id": str(uuid.uuid4())}, None) is False
    assert await ai_route.checked_totp(request, None, "user:x", user_id=str(uuid.uuid4())) is ai_route.TotpOutcome.VALID
    with pytest.raises(HTTPException) as refused:  # a machine token still cannot add risk
        await ai_route.require_totp(request, {"user_id": None}, None)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_with_totp_on_a_missing_code_is_refused():
    with pytest.raises(HTTPException) as refused:
        await ai_route.require_totp(_request(None), {"user_id": str(uuid.uuid4())}, None)
    assert refused.value.status_code == 401


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_totp_switch") as url:
        yield url


@pytest.mark.asyncio
async def test_sign_in_takes_the_password_alone_only_while_totp_is_off(db_url):
    pool = await asyncpg.create_pool(db_url)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=redis)
        username, password = f"op_{uuid.uuid4().hex[:8]}", "Pass12345!"
        cipher, nonce, tag = auth_mgr.encrypt_totp_secret(pyotp.random_base32())
        await pool.execute(
            """INSERT INTO operator_users (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce,
               totp_secret_auth_tag) VALUES ($1, $2, $3, $4, $5, $6)""",
            uuid.uuid4(), username, auth_mgr.hash_password(password), cipher, nonce, tag)
        with pytest.raises(HTTPException) as refused:
            await auth_mgr.authenticate(username, password)
        assert refused.value.status_code == 401
        await pool.execute("""INSERT INTO system_settings (key, value, updated_at) VALUES ($1, '"0"', now())
                              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", KEY)
        assert (await auth_mgr.authenticate(username, password))["username"] == username
        with pytest.raises(HTTPException):  # the password is still checked
            await auth_mgr.authenticate(username, "wrong-password")
    finally:
        await redis.aclose()
        await pool.close()
