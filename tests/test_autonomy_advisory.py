"""ADR 0011 D2/D3: the execution mode is the operator's choice and the gate is advisory. Every transition rule of
``cryptonite_execution.autonomy`` after the ladder came off, and the halt against the real 050 CHECKs."""

import uuid

import asyncpg
import pytest
from cryptonite_execution.autonomy import (
    AutonomyState,
    AutonomyTrigger,
    GateMetrics,
    TransitionRefused,
    evaluate_gate,
    transition,
)
from cryptonite_execution.mandate_store import apply_transition, load_mandate
from cryptonite_orchestrator.mandate_gate import record_breach
from cryptonite_schemas.mandate import AutonomyLevel, MandateReject, MandateStatus
from migrated_db import migrated_database

L1, L2, L3 = AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3
ACTIVE, HALTED, PAUSED, STOPPING = (MandateStatus.ACTIVE, MandateStatus.HALTED, MandateStatus.PAUSED,
                                    MandateStatus.STOPPING)


def s(level, status=ACTIVE, perps=False):
    return AutonomyState(level, status, perps)


# ------------------------------------------------------------------------- the operator sets the mode


@pytest.mark.parametrize("start,target", [(L1, L2), (L1, L3), (L2, L3)])
def test_the_operator_sets_any_higher_mode_without_a_passing_gate(start, target):
    failing = evaluate_gate(start, GateMetrics())  # nothing measured: the advisory score fails
    assert not failing.passed
    t = transition(s(start), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, gate=failing, target_level=target)
    assert (t.after.level, t.kind) == (target, "PROMOTED")


def test_setting_the_mode_needs_totp_and_a_running_profile():
    with pytest.raises(TransitionRefused, match="TOTP_REQUIRED"):
        transition(s(L1), AutonomyTrigger.PROMOTION_CONFIRMED, target_level=L3)
    for status in (HALTED, STOPPING, MandateStatus.STOPPED):
        with pytest.raises(TransitionRefused, match="MANDATE_NOT_ACTIVE"):
            transition(s(L1, status), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, target_level=L2)
    assert transition(s(L1, PAUSED), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True,
                      target_level=L2).after.status is PAUSED


def test_setting_the_mode_it_already_has_is_refused():
    with pytest.raises(TransitionRefused, match="ALREADY_AT_LEVEL"):
        transition(s(L2), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, target_level=L2)


def test_a_perps_profile_keeps_its_perps_at_every_mode():
    up = transition(s(L1, perps=True), AutonomyTrigger.PROMOTION_CONFIRMED, totp_ok=True, target_level=L3)
    down = transition(up.after, AutonomyTrigger.OPERATOR_DEMOTE, target_level=L1)
    assert up.after.allow_perps and down.after.allow_perps and down.after.level is L1


# ------------------------------------------------------------------------- no automatic level change


@pytest.mark.parametrize("trigger", [AutonomyTrigger.BREACH, AutonomyTrigger.HALF_DRAWDOWN])
@pytest.mark.parametrize("level", [L1, L2, L3])
def test_a_breach_and_half_the_drawdown_change_nothing(trigger, level):
    t = transition(s(level, perps=True), trigger, reason="why")
    assert t.kind == "NOOP" and t.after == t.before and t.reason == "why" and not t.level_changed


# ------------------------------------------------------------------------- halts keep the mode and the markets


@pytest.mark.parametrize("trigger,kind", [(AutonomyTrigger.HARD_STOP, "HALTED"), (AutonomyTrigger.BACKSTOP, "BACKSTOP")])
@pytest.mark.parametrize("level", [L1, L2, L3])
@pytest.mark.parametrize("perps", [False, True])
def test_a_halt_keeps_level_and_perps(trigger, kind, level, perps):
    t = transition(s(level, perps=perps), trigger)
    assert (t.kind, t.after) == (kind, s(level, HALTED, perps))


# ------------------------------------------------------------------------- demote, pause, resume


def test_the_operator_lowers_the_mode_freely():
    t = transition(s(L3), AutonomyTrigger.OPERATOR_DEMOTE)
    assert (t.after.level, t.kind) == (L2, "DEMOTED")
    assert transition(s(L3), AutonomyTrigger.OPERATOR_DEMOTE, target_level=L1).after.level is L1
    with pytest.raises(TransitionRefused, match="ALREADY_AT_BOTTOM"):
        transition(s(L1), AutonomyTrigger.OPERATOR_DEMOTE)
    with pytest.raises(TransitionRefused, match="NOT_LOWER"):
        transition(s(L2), AutonomyTrigger.OPERATOR_DEMOTE, target_level=L3)


def test_pause_and_resume():
    assert transition(s(L2), AutonomyTrigger.PAUSE).after.status is PAUSED
    with pytest.raises(TransitionRefused, match="MANDATE_NOT_ACTIVE"):
        transition(s(L2, STOPPING), AutonomyTrigger.PAUSE)
    for status in (HALTED, PAUSED, STOPPING):  # a stopping profile can be taken back
        t = transition(s(L2, status), AutonomyTrigger.RESUME, totp_ok=True)
        assert (t.after.status, t.after.level, t.kind) == (ACTIVE, L2, "RESUMED")
        with pytest.raises(TransitionRefused, match="TOTP_REQUIRED"):
            transition(s(L2, status), AutonomyTrigger.RESUME)
    with pytest.raises(TransitionRefused, match="MANDATE_NOT_STOPPED"):
        transition(s(L2, ACTIVE), AutonomyTrigger.RESUME, totp_ok=True)


# ------------------------------------------------------------------------- against the real 050 schema


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_autonomy_advisory") as url:
        yield url


@pytest.fixture
async def conn(db_url):
    c = await asyncpg.connect(db_url)
    try:
        yield c
    finally:
        await c.close()


async def perp_mandate(conn, level=3) -> uuid.UUID:
    return await conn.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, allow_perps, max_leverage,
                                    autonomy_level, daily_loss_limit_pct)
           VALUES ($1, 1000, ARRAY['BTCUSDT-PERP.BINANCE'], 25, true, 3, $2, 3) RETURNING id""",
        f"perp-{uuid.uuid4().hex[:6]}", level)


@pytest.mark.parametrize("trigger", [AutonomyTrigger.HARD_STOP, AutonomyTrigger.BACKSTOP])
async def test_a_hard_stop_on_a_perp_mandate_succeeds_against_the_050_checks(conn, trigger):
    """The old reset to L1 + allow_perps=False would break chk_ai_mandates_perps_markets and fail the halt itself."""
    mid = await perp_mandate(conn, level=3)
    m = await load_mandate(conn, mid)
    assert m.allow_perps and "PERP" in m.markets
    t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), trigger, reason="test halt")
    await apply_transition(conn, mid, t, actor="test")
    row = await conn.fetchrow("SELECT status, autonomy_level, allow_perps, markets FROM ai_mandates WHERE id = $1", mid)
    assert (row["status"], row["autonomy_level"], row["allow_perps"]) == ("HALTED", 3, True)
    assert "PERP" in row["markets"]
    assert await conn.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = $2",
                               mid, t.kind) == 1


async def test_pause_records_the_reason_and_resume_clears_it(conn):
    mid = await perp_mandate(conn, level=2)
    m = await load_mandate(conn, mid)
    state = AutonomyState(m.autonomy_level, m.status, m.allow_perps)
    paused = transition(state, AutonomyTrigger.PAUSE, reason="lunch")
    await apply_transition(conn, mid, paused, actor="test")
    assert await conn.fetchval("SELECT paused_reason FROM ai_mandates WHERE id = $1", mid) == "lunch"
    await apply_transition(conn, mid, transition(paused.after, AutonomyTrigger.RESUME, totp_ok=True), actor="test")
    row = await conn.fetchrow("SELECT status, paused_reason FROM ai_mandates WHERE id = $1", mid)
    assert (row["status"], row["paused_reason"]) == ("ACTIVE", None)


async def test_a_breach_is_recorded_and_alerted_but_the_mode_stays(conn):
    mid = await perp_mandate(conn, level=3)
    m = await load_mandate(conn, mid)
    row = {"command_id": uuid.uuid4()}
    await record_breach(conn, m, MandateReject.LEVERAGE_EXCEEDED, row, {"instrument_id": "BTCUSDT-PERP.BINANCE"})
    after = await conn.fetchrow("SELECT autonomy_level, status FROM ai_mandates WHERE id = $1", mid)
    assert (after["autonomy_level"], after["status"]) == (3, "ACTIVE")
    assert await conn.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'BREACH'", mid) == 1
    assert await conn.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'DEMOTED'", mid) == 0
    assert await conn.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind = 'MANDATE_BREACH' AND payload->>'mandate_id' = $1",
                               str(mid)) == 1


async def test_the_allow_perps_remedy_adds_perp_to_markets(conn):
    """J: an UPDATE writing allow_perps writes markets with it (apply_transition keeps them together)."""
    mid = await conn.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level)
           VALUES ($1, 1000, ARRAY['BTCUSDT.BINANCE'], 25, 2) RETURNING id""", f"spot-{uuid.uuid4().hex[:6]}")
    assert await conn.fetchval("SELECT markets FROM ai_mandates WHERE id = $1", mid) == ["SPOT"]
    t = transition(AutonomyState(AutonomyLevel.L2, ACTIVE, True), AutonomyTrigger.PAUSE)
    await apply_transition(conn, mid, t, actor="test")  # allow_perps True requested: markets gets PERP in the same UPDATE
    row = await conn.fetchrow("SELECT allow_perps, markets FROM ai_mandates WHERE id = $1", mid)
    assert row["allow_perps"] is True and set(row["markets"]) == {"SPOT", "PERP"}
