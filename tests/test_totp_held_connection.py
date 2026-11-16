"""A TOTP check made by a caller that already holds a pooled connection (the settings and risk-limit writes,
``edit_mandate``) runs on that connection: the operator row, the replay-cache check and its insert take no second
one, so concurrent edits cannot starve the pool. Replay protection is unchanged: one use per time step, and a
concurrent submit of the same code is reported as reused."""

import asyncio
import uuid
from types import SimpleNamespace

import pyotp
import pytest
from ai_api_harness import Hub, close_harness, make_harness
from cryptonite_api.routes import ai as ai_route
from fastapi import HTTPException
from migrated_db import migrated_database


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_totp_held_conn") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 1000000")
    try:
        yield harness
    finally:
        await close_harness(harness)


class GuardedPool:
    """The real pool, except that ``acquire`` fails while armed (a caller holding a connection asked for another)."""

    def __init__(self, pool):
        self.pool = pool
        self.armed = False

    def acquire(self, *a, **kw):
        if self.armed:
            raise AssertionError("a second connection was taken while the caller held one")
        return self.pool.acquire(*a, **kw)


def _request(h, pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool, auth_manager=h.auth, stream_hub=Hub(h.redis))))


async def _step_recorded(h) -> bool:
    return bool(await h.pool.fetchval("SELECT EXISTS (SELECT 1 FROM totp_replay_cache WHERE user_id = $1)", h.operator_id))


@pytest.mark.asyncio
async def test_a_code_checked_on_a_held_connection_takes_no_second_one_and_works_once(h, monkeypatch):
    guard = GuardedPool(h.pool)
    monkeypatch.setattr(h.auth, "_pool", guard)  # the auth manager's replay-cache insert must not use the pool either
    request, auth = _request(h, guard), {"user_id": str(h.operator_id)}
    code = h.totp.now()
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            guard.armed = True
            assert await ai_route.require_totp(request, auth, code, conn=conn) is True
            with pytest.raises(HTTPException) as again:  # the same code in the same transaction: reused
                await ai_route.require_totp(request, auth, code, conn=conn)
            assert again.value.status_code == 401 and again.value.detail.startswith("TOTP_REUSED")
            guard.armed = False
    assert await _step_recorded(h)
    with pytest.raises(HTTPException) as later:  # and on the pool path once committed
        await ai_route.require_totp(request, auth, code)
    assert later.value.detail.startswith("TOTP_REUSED")


@pytest.mark.asyncio
async def test_a_wrong_code_on_a_held_connection_is_refused_without_a_second_one(h, monkeypatch):
    guard = GuardedPool(h.pool)
    monkeypatch.setattr(h.auth, "_pool", guard)
    wrong = f"{(int(h.totp.now()) + 1) % 1_000_000:06d}"
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            guard.armed = True
            with pytest.raises(HTTPException) as refused:
                await ai_route.require_totp(_request(h, guard), {"user_id": str(h.operator_id)}, wrong, conn=conn)
            assert refused.value.detail.startswith("TOTP_REQUIRED")
            assert await conn.fetchval("SELECT 1") == 1  # the caller's transaction is still usable
            guard.armed = False
    assert not await _step_recorded(h)


@pytest.mark.asyncio
async def test_a_concurrent_submit_of_the_same_code_waits_for_the_holder_and_is_reused(h):
    request, auth = _request(h, h.pool), {"user_id": str(h.operator_id)}
    code = h.totp.now()
    first_checked, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                assert await ai_route.require_totp(request, auth, code, conn=conn) is True
                first_checked.set()
                await release.wait()  # still uncommitted while the second submit arrives

    async def second():
        await first_checked.wait()
        async with h.pool.acquire() as conn:
            async with conn.transaction():
                with pytest.raises(HTTPException) as refused:
                    await ai_route.require_totp(request, auth, code, conn=conn)
                return refused.value.detail

    first = asyncio.create_task(holder())
    other = asyncio.create_task(second())
    try:
        await asyncio.wait_for(first_checked.wait(), 10)
        await asyncio.sleep(0.3)
        assert not other.done()  # its insert waits on the holder's uncommitted step
    finally:
        release.set()  # never leave the holder's connection open for teardown to wait on
    await first
    assert (await asyncio.wait_for(other, 10)).startswith("TOTP_REUSED")


@pytest.mark.asyncio
async def test_parallel_risk_adding_edits_with_totp_on_do_not_starve_the_pool(h):
    """12 operators loosen 12 profiles at once through PATCH /ai/mandates on a pool of 6. Each request holds one
    connection for its transaction; a second acquire for the code check would leave all six holders waiting."""
    assert h.pool.get_max_size() < 12
    users = []
    for i in range(12):
        secret = pyotp.random_base32()
        ct, nonce, tag = h.auth.encrypt_totp_secret(secret)
        uid = await h.pool.fetchval(
            """INSERT INTO operator_users (username, password_hash, totp_secret_ciphertext, totp_secret_nonce,
               totp_secret_auth_tag) VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            f"op-par-{i}-{uuid.uuid4().hex[:6]}", h.auth.hash_password("pw"), ct, nonce, tag)
        username = await h.pool.fetchval("SELECT username FROM operator_users WHERE id = $1", uid)
        sid, csrf = await h.auth.create_session(uid, username, role="operator")
        mandate = await h.mandate(name=f"par-{i}-{uuid.uuid4().hex[:4]}")
        users.append((sid, csrf, pyotp.TOTP(secret), mandate.id))

    async def loosen(sid, csrf, totp, mid):
        h.sid = sid
        async with h.client() as c:
            return await c.patch(f"/api/v1/ai/mandates/{mid}", json={"capital_budget_usd": "11000", "totp_code": totp.now()},
                                 headers={"X-CSRF-Token": csrf})

    try:
        results = await asyncio.wait_for(asyncio.gather(*(loosen(*u) for u in users)), timeout=60)
    except TimeoutError:
        h.pool.terminate()  # fail instead of hanging in teardown on the starved connections
        pytest.fail("parallel TOTP-checked edits starved the pool (a second connection taken while one was held)")
    assert [r.status_code for r in results] == [200] * 12, [r.text for r in results if r.status_code != 200]
