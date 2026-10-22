"""P0.5 integration tests for the ADR 0011 profile lifecycle, against a real migrated Postgres.

Driven through ``cryptonite_execution.profile_lifecycle`` directly (no HTTP): the full Start / Pause / Stop path with
its audit rows, the coin locks across each status, a stop with an open position, the decision-log savepoint on a real
asyncpg transaction, and the master switch with TOTP. The API-level matrix lives in ``test_profile_lifecycle_api.py``
and is not repeated here.
"""

import uuid
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle as pl
from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.mandate_store import apply_transition, load_mandate
from cryptonite_execution.profile_log import record_decision
from cryptonite_schemas.mandate import MandateStatus
from cryptonite_schemas.profile import DecisionStage
from migrated_db import migrated_database

D = Decimal
BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
ADA = "ADAUSDT.BINANCE"
ON = pl.AutotradeSettings(enabled=True, max_allocated_pct=D("90"))
OFF = pl.AutotradeSettings(enabled=False, max_allocated_pct=D("90"))


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p05_lifecycle") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("DELETE FROM system_settings WHERE key LIKE 'autotrade.%'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def profile(h, *, instruments=(BTC,), level=1, status="STOPPED", budget=1000, name=None, perps=False):
    """A profile row, STOPPED unless told, with a daily loss limit so it can start."""
    m = await h.mandate(level, instruments=instruments, budget=budget, status=status, name=name)
    await h.pool.execute(
        "UPDATE ai_mandates SET daily_loss_limit_pct = $2, allow_perps = $3, markets = $4 WHERE id = $1",
        m.id, D(3), perps, ["SPOT", "PERP"] if perps else ["SPOT"])
    return m


async def start(h, m, *, totp=True, settings=ON):
    async with h.pool.acquire() as conn:
        return await pl.start(conn, m.id, actor="test", totp_ok=totp, settings=settings)


async def locks(h, m=None) -> set[str]:
    rows = await h.pool.fetch("SELECT base_asset FROM ai_profile_coin_locks" + (" WHERE mandate_id = $1" if m else ""),
                              *([m.id] if m else []))
    return {r["base_asset"] for r in rows}


async def refused(coro) -> pl.LifecycleError:
    with pytest.raises(pl.LifecycleError) as e:
        await coro
    return e.value


async def hold(h, m, instrument=BTC) -> str:
    """An open paper position under the profile's instance (what a filled entry leaves behind)."""
    pid = uuid.uuid4().hex
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, 0.01, 50000, 'OPEN')""", pid, m.instance_id, instrument)
    return pid


async def lifecycle_outcomes(h, m) -> list[str]:
    return [r["outcome"] for r in await h.pool.fetch(
        "SELECT outcome FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'LIFECYCLE' ORDER BY id", m.id)]


async def event_kinds(h, m) -> list[str]:
    return [r["kind"] for r in await h.pool.fetch(
        "SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]


# ------------------------------------------------------------------------- 1. the full lifecycle


async def test_full_lifecycle_start_pause_restart_stop_releases_the_locks(h):
    m = await profile(h, instruments=(BTC, ETH))
    assert (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED and await locks(h) == set()

    # an Ask profile starts without a code and takes both coin locks
    assert (await start(h, m, totp=False)).status is MandateStatus.ACTIVE
    assert await locks(h, m) == {"BTC", "ETH"}

    async with h.pool.acquire() as conn:
        paused = await pl.pause(conn, m.id, actor="test", reason="lunch")
    assert paused.status is MandateStatus.PAUSED and paused.paused_reason == "lunch"
    assert await locks(h, m) == {"BTC", "ETH"}  # a pause keeps the locks

    # restarting a paused profile needs a code, as the resume did before ADR 0011
    e = await refused(start(h, m, totp=False))
    assert e.code == "TOTP_REQUIRED"
    assert (await start(h, m, totp=True)).status is MandateStatus.ACTIVE

    async with h.pool.acquire() as conn:
        stopped = await pl.stop(conn, m.id, actor="test", close=False)  # flat, so it finishes at once
    assert stopped.status is MandateStatus.STOPPED
    assert await locks(h) == set()  # locks released with the transition to STOPPED

    assert await event_kinds(h, m) == ["STARTED", "PAUSED", "STARTED", "STOPPING", "STOPPED"]
    assert await lifecycle_outcomes(h, m) == ["STARTED", "PAUSED", "STARTED", "STOPPING", "STOPPED"]


async def test_archiving_a_halted_profile_releases_its_locks(h):
    """A profile that still holds its coins (halted, so it never reached STOPPED) is archived: the locks must go."""
    m = await profile(h, instruments=(BTC,))
    await start(h, m, totp=False)
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            cur = await load_mandate(conn, m.id, for_update=True)
            t = transition(AutonomyState(cur.autonomy_level, cur.status, cur.allow_perps), AutonomyTrigger.HARD_STOP,
                           reason="hard stop before archive")
            await apply_transition(conn, m.id, t, actor="risk:test")
    assert await locks(h, m) == {"BTC"}
    async with h.pool.acquire() as conn:
        archived = await pl.archive(conn, m.id, actor="test")
    assert archived.status is MandateStatus.ARCHIVED and await locks(h, m) == set()
    assert (await event_kinds(h, m))[-1] == "ARCHIVED" and (await lifecycle_outcomes(h, m))[-1] == "ARCHIVED"


async def test_an_archived_holder_does_not_block_its_coin(h):
    """A lock left behind by an archived profile holds nothing: the next start on that coin clears it."""
    old = await profile(h, instruments=(BTC,), name="archived-holder")
    await start(h, old, totp=False)
    await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED' WHERE id = $1", old.id)
    new = await profile(h, instruments=(BTC,), name="next-holder")
    assert (await start(h, new, totp=False)).status is MandateStatus.ACTIVE
    assert await locks(h) == {"BTC"} and await locks(h, new) == {"BTC"}


# ------------------------------------------------------------------------- 2. which statuses hold the locks


async def test_a_halted_profile_keeps_its_coin_locks_and_blocks_the_coin(h):
    halted = await profile(h, instruments=(BTC, ETH), name="halted-holder")
    await start(h, halted, totp=False)
    # a hard stop goes through the existing transition and the shared persistence, as the monitor does
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            m = await load_mandate(conn, halted.id, for_update=True)
            t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), AutonomyTrigger.HARD_STOP,
                           reason="drawdown past the hard stop")
            await apply_transition(conn, m.id, t, actor="risk:test")
    got = await load_mandate(h.pool, halted.id)
    assert got.status is MandateStatus.HALTED and got.autonomy_level == halted.autonomy_level
    assert await locks(h, halted) == {"BTC", "ETH"}

    clash = await profile(h, instruments=(ETH,), name="wants-eth")
    e = await refused(start(h, clash, totp=False))
    assert (e.code, e.detail["coin"], e.detail["holder_name"]) == ("COIN_LOCKED", "ETH", "halted-holder")


async def test_a_stopped_profile_frees_its_coin_for_another_profile(h):
    a = await profile(h, instruments=(ETH,), name="first")
    await start(h, a, totp=False)
    async with h.pool.acquire() as conn:
        await pl.stop(conn, a.id, actor="test", close=False)
    b = await profile(h, instruments=(ETH,), name="second")
    assert (await start(h, b, totp=False)).status is MandateStatus.ACTIVE
    assert await locks(h) == {"ETH"} and await locks(h, b) == {"ETH"}


# ------------------------------------------------------------------------- 3. stop with an open position


async def test_stop_with_close_holds_the_coin_until_flat_then_frees_it(h):
    m = await profile(h, instruments=(BTC,), name="closer")
    await start(h, m, totp=False)
    pid = await hold(h, m, BTC)
    async with h.pool.acquire() as conn:
        got = await pl.stop(conn, m.id, actor="test", close=True)
    assert got.status is MandateStatus.STOPPING and await locks(h, m) == {"BTC"}
    cmds = await h.pool.fetch("SELECT command_type, mandate_id FROM execution_commands")
    assert [(c["command_type"], c["mandate_id"]) for c in cmds] == [("CLOSE_POSITION", m.id)]

    other = await profile(h, instruments=(BTC,), name="waiting")
    e = await refused(start(h, other, totp=False))
    assert (e.code, e.detail["holder_name"]) == ("COIN_LOCKED", "closer")

    # the close fills: the position is flat, and the monitor's finish pass moves the profile to STOPPED
    await h.pool.execute("UPDATE paper_positions SET status = 'CLOSED', quantity = 0 WHERE position_id = $1", pid)
    async with h.pool.acquire() as conn:
        assert await pl.finish_stop(conn, m.id) is True
    assert (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED and await locks(h) == set()
    assert (await start(h, other, totp=False)).status is MandateStatus.ACTIVE
    assert await lifecycle_outcomes(h, m) == ["STARTED", "STOPPING", "STOPPED"]


# ------------------------------------------------------------------------- 4. the decision-log savepoint


async def test_a_failed_decision_write_leaves_the_caller_transaction_usable(h):
    m = await profile(h, instruments=(BTC,))
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            # the random mandate id violates the foreign key: the log refuses and reports it, never raises
            ok = await record_decision(conn, uuid.uuid4(), DecisionStage.LIFECYCLE, "FK_PROBE", "no such mandate")
            assert ok is False
            # the same transaction still runs statements, including a write to a real mandate
            assert await conn.fetchval("SELECT 1") == 1
            await conn.execute("UPDATE ai_mandates SET paused_reason = 'savepoint probe' WHERE id = $1", m.id)
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE outcome = 'FK_PROBE'") == 0
    assert (await load_mandate(h.pool, m.id)).paused_reason == "savepoint probe"  # committed


async def test_a_successful_decision_write_adds_exactly_one_row(h):
    m = await profile(h, instruments=(BTC,))
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            assert await record_decision(conn, m.id, DecisionStage.LIFECYCLE, "PROBE_OK", "probe", actor="test") is True
    assert await h.pool.fetchval(
        "SELECT count(*) FROM ai_profile_decisions WHERE mandate_id = $1 AND outcome = 'PROBE_OK'", m.id) == 1


# ------------------------------------------------------------------------- 5. the master switch and TOTP


async def test_the_master_switch_gates_veto_and_auto_and_asks_totp_for_veto(h):
    ask = await profile(h, instruments=(ETH,), level=1, name="ask")
    veto = await profile(h, instruments=(BTC,), level=2, name="veto")
    auto = await profile(h, instruments=(ADA,), level=3, name="auto")

    # switch off: Ask starts, Veto and Auto are refused and nothing is written for them
    assert (await start(h, ask, totp=False, settings=OFF)).status is MandateStatus.ACTIVE
    for m, mode in ((veto, "VETO"), (auto, "AUTO")):
        e = await refused(start(h, m, totp=True, settings=OFF))
        assert (e.code, e.detail["mode"]) == ("AUTOTRADE_DISABLED", mode)
        assert (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED
    assert await locks(h) == {"ETH"}

    # switch on: Veto still needs a code; with the code it starts
    e = await refused(start(h, veto, totp=False, settings=ON))
    assert e.code == "TOTP_REQUIRED" and e.detail["mode"] == "VETO"
    assert (await start(h, veto, totp=True, settings=ON)).status is MandateStatus.ACTIVE
    assert await locks(h, veto) == {"BTC"}
    assert await lifecycle_outcomes(h, veto) == ["STARTED"]
    assert await lifecycle_outcomes(h, auto) == []
