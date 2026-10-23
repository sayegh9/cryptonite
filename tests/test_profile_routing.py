"""ADR 0011: profile ownership, refusal ordering, execution modes and debate budgets."""

import asyncio
import datetime
import importlib
import json
import sys
import uuid
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/debate_worker"))
routing = importlib.import_module("mandate_routing")
gate = importlib.import_module("gate")
SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
NOW = datetime.datetime(2026, 10, 23, 12, tzinfo=datetime.UTC)
BULL = {"trend_4h": "BULL", "trend_1h": "BULL", "trend_15m": "BULL", "adx": 30,
        "plus_di": 30, "minus_di": 10, "atr_pct": 1, "atr_pct_q90": 2}


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_profile_routing") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("TRUNCATE ai_profile_coin_locks, ai_profile_decisions, ai_approvals, "
                               "execution_commands, paper_positions, debate_runs CASCADE")
    await harness.pool.execute("UPDATE ai_mandates SET status='ARCHIVED'")
    await harness.pool.execute("DELETE FROM llm_cost_ledger")
    await harness.pool.execute("DELETE FROM system_settings WHERE key IN ('LLM_DAILY_BUDGET_USD','autotrade.enabled')")
    await harness.redis.flushdb()
    from cryptonite_execution import profile_lifecycle
    profile_lifecycle.reset_switch_cache()
    await harness.redis.set("perp:mark:BTCUSDT", json.dumps({"mark_price": "60000", "funding_rate": "0.0001"}))
    try:
        yield harness
    finally:
        await close_harness(harness)


async def profile(h, *, market="SPOT", level=1, lock=True, **changes):
    """Seed one isolated profile, with coin ownership unless testing the unlocked path."""
    instrument = PERP if market == "PERP" else SPOT
    mid = await h.pool.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, status,
           autonomy_level, markets, max_leverage, direction, style)
           VALUES ($1, 10000, ARRAY[$2], 25, 'ACTIVE', $3, ARRAY[$4]::text[], $5, 'LONG', 'SWING') RETURNING id""",
        f"profile-{uuid.uuid4()}", instrument, level, market, 3 if market != "SPOT" else 1)
    if changes:
        for key, value in changes.items():
            assert key in {"status", "direction", "max_open_positions", "max_debates_per_day", "active_hours_utc", "dry_run"}
            await h.pool.execute(f"UPDATE ai_mandates SET {key}=$2 WHERE id=$1", mid, value)
    if lock:
        await h.pool.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", mid)
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance
    async with h.pool.acquire() as conn:
        m = await load_mandate(conn, mid)
        await ensure_mandate_instance(conn, m, "PAPER-ACCOUNT-001")
    return m


async def route(h, mid=None, **kw):
    run = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','test')", run, SPOT)
    async with h.pool.acquire() as conn:
        result = await routing.route_entry(conn, run, SPOT, "BUY", .8, 2.5, mandate_id=str(mid) if mid else None,
                                           now=NOW, stop_loss=57000, take_profit=66000, reference_price=60000,
                                           redis=h.redis, grade_context=BULL, **kw)
    return result, run


async def test_resolution_uses_payload_then_lock_owner_else_operator(h):
    owner = await profile(h)
    other = await profile(h, lock=False)
    async with h.pool.acquire() as conn:
        assert (await routing.resolve_profile(conn, SPOT, str(other.id))).id == other.id
        assert (await routing.resolve_profile(conn, PERP, None)).id == owner.id
        assert await routing.resolve_profile(conn, "ETHUSDT.BINANCE", None) is None


@pytest.mark.parametrize("changes,reason", [({"status": "PAUSED"}, "PROFILE_NOT_RUNNING"),
                                            ({"direction": "SHORT"}, "DIRECTION_NOT_ALLOWED"),
                                            ({"max_open_positions": 1}, "MAX_OPEN_POSITIONS")])
async def test_profile_entry_refusals_write_no_approval_or_command(h, changes, reason):
    m = await profile(h, market="PERP" if changes.get("direction") == "SHORT" else "SPOT", **changes)
    if reason == "MAX_OPEN_POSITIONS":
        from test_debate_perp_routing import hold_perp
        await hold_perp(h, m, "LONG")
    (status, _), run = await route(h, m.id)
    assert status == "NO_TRADE"
    row = await h.pool.fetchrow("SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run)
    assert row["reason"] == reason
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_coin_lock_is_refused_before_market_selection(h):
    await profile(h)
    other = await profile(h, lock=False)
    (status, _), run = await route(h, other.id)
    assert status == "NO_TRADE"
    assert await h.pool.fetchval("SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run) == "COIN_LOCKED"


@pytest.mark.parametrize("market,reason", [("SPOT_MARGIN", "SPOT_MARGIN_NOT_AVAILABLE")])
async def test_unexecutable_market_is_refused(h, market, reason):
    m = await profile(h, market=market)
    await h.pool.execute("UPDATE ai_mandates SET direction='BOTH' WHERE id=$1", m.id)
    run = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','test')", run, SPOT)
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(conn, run, SPOT, "SELL", .8, 5, mandate_id=str(m.id))
    assert status == "NO_TRADE"
    assert await h.pool.fetchval("SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run) == reason


@pytest.mark.parametrize("market", ["SPOT", "PERP"])
@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize("switch", [False, True])
async def test_profile_mode_and_master_switch(h, market, level, switch):
    m = await profile(h, market=market, level=level, active_hours_utc="08:00-22:00")
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('autotrade.enabled',$1) "
                         "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", json.dumps("1" if switch else "0"))
    (status, _), run = await route(h)
    effective = level if switch else 1
    assert status == ("COMPLETED" if effective == 3 else "PENDING_APPROVAL")
    if effective < 3:
        assert await h.pool.fetchval("SELECT timeout_policy FROM ai_approvals WHERE source_ref=$1", str(run)) == (
            "PROCEED" if effective == 2 else "DENY")
    else:
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 1
    expected = {1: "APPROVAL_CREATED", 2: "VETO_WINDOW", 3: "ENQUEUED"}[effective]
    assert await h.pool.fetchval("SELECT outcome FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run) == expected


async def test_budget_reservation_is_atomic_and_returns_excess_slot(h):
    mid = uuid.uuid4()
    key = gate.profile_cap_key(mid, NOW.timestamp())
    assert sorted(await asyncio.gather(*(gate._reserve_profile_slot(h.redis, key, 2) for _ in range(3)))) == [False, True, True]
    assert int(await h.redis.get(key)) == 2
    assert 0 < await h.redis.ttl(key) <= 172800


@pytest.mark.parametrize("timeframe,allowed,reason", [("15m", False, "TIMEFRAME_NOT_IN_STYLE"), ("4h", True, "NO_TRIAGE_GATEWAY")])
async def test_gate_uses_profile_style_and_logs_its_decision(h, timeframe, allowed, reason):
    m = await profile(h)
    payload = {"instrument_id": SPOT, "mandate_id": str(m.id), "candidate_payload": json.dumps({"timeframe": timeframe})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, None, "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (allowed, reason)
    assert await h.pool.fetchval("SELECT reason FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='GATE'", m.id) == reason


async def test_global_llm_budget_exhaustion_is_logged(h):
    m = await profile(h)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','0') "
                         "ON CONFLICT(key) DO UPDATE SET value='0'")
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, None, "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (False, "AI_BUDGET_EXHAUSTED")
    assert await h.pool.fetchval("SELECT reason FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='GATE'", m.id) == "AI_BUDGET_EXHAUSTED"
