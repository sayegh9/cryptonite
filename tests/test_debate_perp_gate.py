"""ADR 0011: exclusive profile ownership replaces Phase B perp preferences and caps."""

import json
import uuid

import asyncpg
import pytest
from migrated_db import migrated_database, seed_paper_account
from test_debate_gate import INSTRUMENT, Redis, as_api, load, scanner_payload

PERP = "BTCUSDT-PERP.BINANCE"
CAP_KEY = "debate.perp_mandate_daily_cap"


class CapRedis(Redis):
    """The test_debate_gate Redis, with INCR returning the count and DECR (the slot reservation)."""

    def __init__(self):
        super().__init__(brief="a brief")

    async def incr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) + 1
        return self.kv[key]

    async def decr(self, key):
        self.kv[key] = int(self.kv.get(key, 0)) - 1
        return self.kv[key]

    def drop_cooldowns(self):
        for key in [k for k in self.kv if str(k).startswith("debate:cooldown:")]:
            del self.kv[key]


@pytest.fixture(scope="module")
def gate():
    return load("debate_worker_gate_perp", "gate.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_perp_gate") as url:
        yield url


@pytest.fixture
async def admin(db_url):
    conn = await asyncpg.connect(db_url)
    await conn.execute("DELETE FROM ai_profile_coin_locks")
    await conn.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await conn.execute("DELETE FROM system_settings WHERE key = $1", CAP_KEY)
    await seed_paper_account(conn)
    try:
        yield conn
    finally:
        await conn.execute("TRUNCATE debate_runs CASCADE")
        await conn.execute("DELETE FROM ai_profile_coin_locks")
        await conn.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
        await conn.execute("DELETE FROM system_settings WHERE key = $1", CAP_KEY)
        await conn.close()


@pytest.fixture
async def pool(db_url, admin):
    p = await asyncpg.create_pool(as_api(db_url), min_size=1, max_size=4)
    yield p
    await p.close()


async def add_mandate(admin, instrument: str, *, perps: bool) -> uuid.UUID:
    mid = await admin.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status,
               allow_perps, max_leverage)
           VALUES ($1, 10000, ARRAY[$2], 25, 1, 'ACTIVE', $3, $4) RETURNING id""",
        f"m-{uuid.uuid4().hex[:6]}", instrument, perps, 3 if perps else 1)
    await admin.execute("INSERT INTO ai_profile_coin_locks(base_asset,mandate_id) VALUES('BTC',$1) ON CONFLICT DO NOTHING", mid)
    if not perps:
        await admin.execute("UPDATE ai_mandates SET style='INTRADAY' WHERE id=$1", mid)
    return mid



async def ask(gate, pool, redis, timeframe: str = "1h"):
    return await gate.pre_debate_gate(pool, redis, scanner_payload(timeframe=timeframe), None, "a brief")


async def test_a_spot_candidate_is_unchanged(gate, pool, admin):
    await add_mandate(admin, INSTRUMENT, perps=False)
    await add_mandate(admin, PERP, perps=True)  # a spot mandate takes the candidate: the perp rules do not apply
    redis = CapRedis()
    assert (await ask(gate, pool, redis, "15m")).reason == "NO_TRIAGE_GATEWAY"
    assert any(str(k).startswith("debate_gate:profile:") for k in redis.kv)


async def test_no_mandate_at_all_is_unchanged(gate, pool, admin):
    assert (await ask(gate, pool, CapRedis(), "15m")).reason == "NO_TRIAGE_GATEWAY"


async def test_a_perp_profile_takes_only_its_style_bars(gate, pool, admin):
    await add_mandate(admin, PERP, perps=True)
    redis = CapRedis()
    refused = await ask(gate, pool, redis, "5m")  # no SWING preset runs on 5m
    assert (refused.allowed, refused.reason) == (False, "TIMEFRAME_NOT_IN_STYLE")
    assert not any(str(k).startswith("debate:cooldown:") for k in redis.kv)  # refused before the cooldown
    assert (await ask(gate, pool, redis, "1h")).allowed is True
    assert (await ask(gate, pool, CapRedis(), "15m")).allowed is True  # H4: the 15m swing preset runs on its own bars


async def test_the_default_cap_is_6_a_day_per_mandate(gate, pool, admin):
    mid = await add_mandate(admin, PERP, perps=True)
    redis = CapRedis()
    reasons = []
    for _ in range(8):
        redis.drop_cooldowns()  # one candidate per setup window: each call here is a new window
        reasons.append((await ask(gate, pool, redis)).reason)
    assert reasons == ["NO_TRIAGE_GATEWAY"] * 6 + ["PROFILE_DEBATE_DAILY_CAP"] * 2
    (key,) = [k for k in redis.kv if str(k).startswith("debate_gate:profile:")]
    assert str(mid) in key and redis.kv[key] == 6


async def test_the_cap_is_the_profile_debate_limit(gate, pool, admin):
    await add_mandate(admin, PERP, perps=True)
    await admin.execute("UPDATE ai_mandates SET max_debates_per_day=1 WHERE status='ACTIVE'")
    redis = CapRedis()
    assert (await ask(gate, pool, redis)).allowed is True
    redis.drop_cooldowns()
    assert (await ask(gate, pool, redis)).reason == "PROFILE_DEBATE_DAILY_CAP"
    await admin.execute("UPDATE ai_mandates SET max_debates_per_day=0 WHERE status='ACTIVE'")
    assert (await ask(gate, pool, CapRedis())).reason == "PROFILE_DEBATE_DAILY_CAP"  # 0 stops them


async def test_a_retired_cap_setting_does_not_override_the_profile(gate, pool, admin):
    await add_mandate(admin, PERP, perps=True)
    await admin.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)", CAP_KEY, json.dumps("0"))
    assert (await ask(gate, pool, CapRedis())).allowed is True


async def test_the_reservation_refuses_over_the_cap_and_gives_the_slot_back(gate):
    redis = CapRedis()
    key = gate.profile_cap_key(uuid.uuid4(), 0)
    assert [await gate._reserve_profile_slot(redis, key, 2) for _ in range(3)] == [True, True, False]
    assert redis.kv[key] == 2


def test_the_cap_key_is_per_utc_day():
    gate = load("debate_worker_gate_perp_key", "gate.py")
    mid = uuid.uuid4()
    assert gate.profile_cap_key(mid, 0) == f"debate_gate:profile:{mid}:1970-01-01"
    assert gate.profile_cap_key(mid, 86399) == gate.profile_cap_key(mid, 0)
    assert gate.profile_cap_key(mid, 86400).endswith("1970-01-02")


async def test_retired_preference_cannot_change_profile_cadence(gate, pool, admin):
    await add_mandate(admin, INSTRUMENT, perps=False)
    await add_mandate(admin, PERP, perps=True)
    await admin.execute("INSERT INTO system_settings(key,value) VALUES('debate.perp_routing','1') "
                        "ON CONFLICT(key) DO UPDATE SET value='1'")
    try:
        assert (await ask(gate, pool, CapRedis(), "15m")).reason == "NO_TRIAGE_GATEWAY"
    finally:
        await admin.execute("DELETE FROM system_settings WHERE key='debate.perp_routing'")
