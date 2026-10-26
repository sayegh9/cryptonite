"""P0 Gate R fix round (ADR 0011): the review findings C1, C2, H1, H2, M1, M3, M5 and the LOW create contract.

Every test here failed before its fix. Real migrated Postgres, the real API app, as test_profile_lifecycle_api does.
"""

import ast
import asyncio
import importlib.util
import json
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle as pl
from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.mandate_store import apply_transition, load_mandate
from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_orchestrator.mandate_gate import approval_granted
from cryptonite_schemas.mandate import BREACH_CODES, MandateReject, MandateStatus
from migrated_db import migrated_database

D = Decimal
BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
V1 = "/api/v1/ai/mandates"
AGENTS = "/api/v1/agents/debates"
ON = pl.AutotradeSettings(enabled=True, max_allocated_pct=D("90"))
WORKER = Path(__file__).resolve().parents[1] / "services" / "debate_worker"
sys.path.insert(0, str(WORKER))


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p0_gater") as url:
        yield url


@pytest.fixture(scope="module")
def routing():
    spec = importlib.util.spec_from_file_location("gater_mandate_routing", WORKER / "mandate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    pl.reset_switch_cache()
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED', name = id::text")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("DELETE FROM system_settings WHERE key LIKE 'autotrade.%'")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions, debate_runs CASCADE")
    try:
        yield harness
    finally:
        pl.reset_switch_cache()
        await close_harness(harness)


async def profile(h, *, instruments=(BTC,), level=1, status="STOPPED", budget=1000, name=None):
    m = await h.mandate(level, instruments=instruments, budget=budget, status=status, name=name)
    await h.pool.execute("UPDATE ai_mandates SET daily_loss_limit_pct = 3, markets = ARRAY['SPOT'] WHERE id = $1", m.id)
    return m


async def switch(h, on: bool) -> None:
    await h.pool.execute(
        """INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps(1 if on else 0))
    pl.reset_switch_cache()


async def start(h, m, *, totp=True, settings=ON):
    async with h.pool.acquire() as conn:
        return await pl.start(conn, m.id, actor="test", totp_ok=totp, settings=settings)


async def locks(h, m=None) -> set[str]:
    rows = await h.pool.fetch("SELECT base_asset FROM ai_profile_coin_locks" + (" WHERE mandate_id = $1" if m else ""),
                              *([m.id] if m else []))
    return {r["base_asset"] for r in rows}


async def status_of(h, m) -> str:
    return await h.pool.fetchval("SELECT status FROM ai_mandates WHERE id = $1", m.id)


async def hold(h, m, instrument=BTC) -> None:
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, 0.01, 50000, 'OPEN')""",
        uuid.uuid4().hex, m.instance_id, instrument)


async def halt(h, m) -> None:
    async with h.pool.acquire() as conn:
        async with conn.transaction():
            cur = await load_mandate(conn, m.id, for_update=True)
            t = transition(AutonomyState(cur.autonomy_level, cur.status, cur.allow_perps), AutonomyTrigger.HARD_STOP,
                           reason="test halt")
            await apply_transition(conn, m.id, t, actor="risk:test")


# ------------------------------------------------------------------------- C1: resume is a Start


async def test_resume_of_a_halted_veto_profile_is_refused_while_the_switch_is_off(h):
    m = await profile(h, level=2, status="ACTIVE")
    await halt(h, m)
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/resume", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("AUTOTRADE_DISABLED:"), res.text
    assert await status_of(h, m) == "HALTED"
    await switch(h, True)
    async with h.client() as c:
        ok = await c.post(f"{V1}/{m.id}/resume", json={"totp_code": h.totp.at(time.time() + 30)},
                          headers=h.headers)
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE", ok.text


async def test_resume_applies_the_capital_cap_and_the_coin_locks(h):
    equity = await h.pool.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = 'PAPER-ACCOUNT-001'")
    big = await profile(h, status="ACTIVE", budget=int(equity * D("0.5")), name="resume-big")
    await halt(h, big)
    await h.pool.execute("UPDATE ai_mandates SET capital_budget_usd = $2 WHERE id = $1", big.id, equity)  # raised while halted
    async with h.client() as c:
        res = await c.post(f"{V1}/{big.id}/resume", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("CAPITAL_OVER_ALLOCATED:"), res.text
    assert await status_of(h, big) == "HALTED"

    # the lock of a halted profile that somebody else now holds: the resume cannot take the coin
    await h.pool.execute("UPDATE ai_mandates SET capital_budget_usd = 1000 WHERE id = $1", big.id)
    await h.pool.execute("DELETE FROM ai_profile_coin_locks")
    thief = await profile(h, status="ACTIVE", name="resume-thief")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, thief.id, thief.instruments)
    async with h.client() as c:
        res = await c.post(f"{V1}/{big.id}/resume", json={"totp_code": h.totp.at(time.time() + 30)},
                           headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("COIN_LOCKED:"), res.text


async def test_resume_of_an_active_or_stopped_profile_is_still_refused(h):
    m = await profile(h, status="ACTIVE")
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/resume", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_NOT_STOPPED")


async def test_start_accepts_a_stopping_profile(h):
    m = await profile(h)
    await start(h, m, totp=True)
    await hold(h, m)
    async with h.pool.acquire() as conn:
        assert (await pl.stop(conn, m.id, actor="t", close=False)).status is MandateStatus.STOPPING
    with pytest.raises(pl.LifecycleError) as e:
        await start(h, m, totp=False)
    assert e.value.code == "TOTP_REQUIRED"
    assert (await start(h, m, totp=True)).status is MandateStatus.ACTIVE


def _non_test_sources():
    root = Path(__file__).resolve().parents[1]
    for top in ("services", "packages", "scripts"):
        for path in (root / top).rglob("*.py"):
            if ".venv" not in path.parts and "tests" not in path.parts:
                yield path.relative_to(root).as_posix(), ast.parse(path.read_text())


def test_resume_trigger_is_only_used_by_the_autonomy_table_and_the_lifecycle():
    """C1: nothing outside the state machine and ``profile_lifecycle`` may name ``AutonomyTrigger.RESUME``."""
    allowed = {"packages/execution/cryptonite_execution/autonomy.py",
               "packages/execution/cryptonite_execution/profile_lifecycle.py"}
    users = {rel for rel, tree in _non_test_sources() for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "RESUME"
             and isinstance(n.value, ast.Name) and n.value.id == "AutonomyTrigger"}
    assert users <= allowed, f"{users - allowed} use AutonomyTrigger.RESUME: route the resume through profile_lifecycle"


def test_only_known_modules_call_apply_transition():
    """C1: a new caller of ``apply_transition`` can reach ACTIVE behind the Start checks."""
    allowed = {
        "services/orchestrator/cryptonite_orchestrator/mandate_monitor.py",  # HALT / account backstop
        "services/orchestrator/cryptonite_orchestrator/mandate_gate.py",  # breach record
        "packages/execution/cryptonite_execution/profile_lifecycle.py",
        "services/api/cryptonite_api/routes/ai.py",  # pause / demote / mode set
        "scripts/cleanup_test_pollution.py",  # PAUSE of test mandates
    }
    callers = {rel for rel, tree in _non_test_sources() for n in ast.walk(tree)
               if isinstance(n, ast.Call) and (getattr(n.func, "id", None) == "apply_transition"
                                               or getattr(n.func, "attr", None) == "apply_transition")}
    assert callers <= allowed, f"{callers - allowed} call apply_transition: route it through profile_lifecycle"


# ------------------------------------------------------------------------- C2: the switch applies at runtime


async def test_confirm_above_ask_is_refused_while_the_switch_is_off(h):
    m = await profile(h, status="ACTIVE")
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 2, "totp_code": h.totp.now()},
                           headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("AUTOTRADE_DISABLED:"), res.text
    assert (await load_mandate(h.pool, m.id)).autonomy_level == 1
    await switch(h, True)
    async with h.client() as c:
        ok = await c.post(f"{V1}/{m.id}/autonomy/confirm",
                          json={"level": 2, "totp_code": h.totp.at(time.time() + 30)}, headers=h.headers)
    assert ok.status_code == 200, ok.text
    await switch(h, False)  # lowering is free whatever the switch says
    async with h.client() as c:
        low = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 1}, headers=h.headers)
    assert low.status_code == 200, low.text


@pytest.mark.parametrize("level", [2, 3])
async def test_with_the_switch_off_a_veto_or_auto_entry_is_asked_for_like_level_1(h, routing, level):
    m = await profile(h, level=level, status="ACTIVE")
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, got = await routing.route_entry(conn, run_id, BTC, "Buy", 0.7, 5.0, mandate_id=str(m.id),
                                                reference_price=50000, stop_loss=49000)
    assert status == "PENDING_APPROVAL" and got.id == m.id
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0  # never a direct enqueue
    a = await h.pool.fetchrow("SELECT * FROM ai_approvals WHERE source_ref = $1", str(run_id))
    assert (a["action_class"], a["timeout_policy"], a["level_at_creation"]) == ("ADD_RISK", "DENY", 1)  # no veto window


async def test_with_the_switch_on_a_level_3_entry_still_goes_straight_to_the_queue(h, routing):
    await switch(h, True)
    m = await profile(h, level=3, status="ACTIVE")
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.7, 5.0, mandate_id=str(m.id),
                                              reference_price=50000, stop_loss=49000)
    assert status == "COMPLETED" and await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 1


@pytest.mark.parametrize("level", [2, 3])
async def test_switch_off_stopless_spot_buy_is_refused_before_approval(h, routing, level):
    """The switch's ASK fallback cannot approve an entry lacking its structural stop."""
    await switch(h, False)
    m = await profile(h, level=level, status="ACTIVE")
    run_id = uuid.uuid4()
    async with h.pool.acquire() as conn:
        status, got = await routing.route_entry(conn, run_id, BTC, "Buy", 0.7, 5.0,
                                                mandate_id=str(m.id), reference_price=50000)
    assert status == "NO_TRADE" and got.id == m.id
    assert await h.pool.fetchval(
        "SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run_id,
    ) == "STOP_REQUIRED"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE mandate_id=$1", m.id) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE mandate_id=$1", m.id) == 0


async def test_with_the_switch_off_a_level_3_exit_is_still_immediate(h, routing):
    m = await profile(h, level=3, status="ACTIVE")
    await hold(h, m)
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(conn, uuid.uuid4(), BTC, "Sell", 0.7, 5.0, mandate_id=str(m.id))
    assert status == "COMPLETED" and await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 1


async def test_the_switch_loader_is_cached_and_fails_safe_to_off(h):
    async with h.pool.acquire() as conn:
        assert await pl.autotrade_switch_on(conn) is False
        await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb)")
        assert await pl.autotrade_switch_on(conn) is False  # cached
        pl.reset_switch_cache()
        assert await pl.autotrade_switch_on(conn) is True
    assert pl.SWITCH_CACHE_S <= 30
    pl.reset_switch_cache()

    class Broken:
        def transaction(self):
            raise RuntimeError("db down")

    assert await pl.autotrade_switch_on(Broken()) is False


def _order():
    return MandateOrder(BTC, "BUY", D("0.001"), D("50000"), "LIMIT")


@pytest.mark.parametrize("level", [2, 3])
def test_check_mandate_needs_an_approval_at_levels_2_and_3_while_the_switch_is_off(level):
    from cryptonite_schemas.mandate import Mandate

    m = Mandate(id=uuid.uuid4(), name="g", capital_budget_usd=D(10000), instruments=[BTC], max_position_pct=D(25),
                autonomy_level=level)
    refusal = check_mandate(m, _order(), MandateExposure(), approval_ok=False, autotrade_on=False)
    assert refusal is MandateReject.APPROVAL_REQUIRED and refusal.value == "MANDATE_APPROVAL_REQUIRED"
    assert refusal not in BREACH_CODES  # a refusal, never a breach
    assert check_mandate(m, _order(), MandateExposure(), approval_ok=True, autotrade_on=False) is None
    assert (check_mandate(m, _order(), MandateExposure(), approval_ok=False, autotrade_on=True)
            is (MandateReject.APPROVAL_REQUIRED if level == 2 else None))
    exit_ = MandateOrder(BTC, "SELL", D("0.001"), D("50000"), "MARKET")
    held = MandateExposure(instrument_qty=D("1"))
    assert check_mandate(m, exit_, held, approval_ok=False, autotrade_on=False) is None  # exits unaffected


async def test_an_expired_veto_window_is_no_approval_while_the_switch_is_off(h):
    m = await profile(h, level=2, status="ACTIVE")
    cmd = uuid.uuid4()
    aid = await h.pool.fetchval(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at, status, command_id)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 's', 'brain', 'r', 2, 'PROCEED', now(),
                   'EXPIRED_PROCEEDED', $2) RETURNING id""", m.id, cmd)
    row = {"command_id": cmd}
    async with h.pool.acquire() as conn:
        assert await approval_granted(conn, row, {"approval_id": str(aid)}, m, autotrade_on=True) is True
        assert await approval_granted(conn, row, {"approval_id": str(aid)}, m, autotrade_on=False) is False
        await h.pool.execute("UPDATE ai_approvals SET status = 'APPROVED' WHERE id = $1", aid)
        assert await approval_granted(conn, row, {"approval_id": str(aid)}, m, autotrade_on=False) is True


def test_the_registry_note_says_the_switch_applies_at_runtime():
    from cryptonite_api.settings_registry import REGISTRY

    note = next(s for s in REGISTRY if s.key == "autotrade.enabled").note
    assert "runtime" in note and "read at Start" not in note


# ------------------------------------------------------------------------- H1: the capital race


async def test_two_concurrent_starts_that_together_exceed_the_cap_let_exactly_one_win(h, monkeypatch):
    equity = await h.pool.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = 'PAPER-ACCOUNT-001'")
    each = int(equity * D("0.5"))  # 50% + 50% > the 90% cap
    a = await profile(h, instruments=(BTC,), budget=each, name="cap-a")
    b = await profile(h, instruments=(ETH,), budget=each, name="cap-b")
    real = pl.acquire_locks

    async def slow(conn, mid, instruments):  # widen the window between the capital check and the commit
        await asyncio.sleep(0.4)
        await real(conn, mid, instruments)

    monkeypatch.setattr(pl, "acquire_locks", slow)
    results = await asyncio.gather(start(h, a), start(h, b), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    bad = [r for r in results if isinstance(r, pl.LifecycleError)]
    assert len(ok) == 1 and len(bad) == 1 and bad[0].code == "CAPITAL_OVER_ALLOCATED", results


# ------------------------------------------------------------------------- H2: an edit never drops a coin in use


async def test_removing_an_instrument_with_an_open_position_keeps_its_lock(h):
    m = await profile(h, instruments=(BTC, ETH), status="STOPPED", name="retain")
    await start(h, m)
    await hold(h, m, ETH)
    async with h.client() as c:
        res = await c.patch(f"{V1}/{m.id}", json={"instruments": [BTC]}, headers=h.headers)
        assert res.status_code == 200, res.text
        assert res.json()["locks_retained"] == ["ETH"]
        assert await locks(h, m) == {"BTC", "ETH"}  # ETH stays locked: its position still has exits to run
        # flat again: the next edit releases it
        await h.pool.execute("UPDATE paper_positions SET status = 'CLOSED' WHERE instance_id = $1", m.instance_id)
        res = await c.patch(f"{V1}/{m.id}", json={"instruments": [BTC], "max_open_positions": 1}, headers=h.headers)
    assert res.status_code == 200, res.text


async def test_sync_locks_keeps_a_coin_with_a_working_order(h):
    m = await profile(h, instruments=(BTC, ETH), name="retain-order")
    await start(h, m)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               status) VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'LIMIT', 0.01, 'SUBMITTED')""",
        f"w-{uuid.uuid4().hex[:8]}", m.instance_id, ETH)
    async with h.pool.acquire() as conn:
        cur = await load_mandate(conn, m.id)
        edited = cur.model_copy(update={"instruments": [BTC]})
        assert await pl.sync_locks(conn, edited) == ["ETH"]
    assert await locks(h, m) == {"BTC", "ETH"}


# ------------------------------------------------------------------------- M1: never proceed without the lock


class Interleave:
    """A connection whose holder lookup finds nothing (the holder released in between); ``hold_forever`` keeps the
    holder's lock in the table so every retry conflicts again."""

    def __init__(self, conn, holder_id, coin, *, release_on_lookup: bool):
        self._conn, self.holder_id, self.coin, self.release = conn, holder_id, coin, release_on_lookup
        self.lookups = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def fetchrow(self, sql, *args):
        if "JOIN ai_mandates" in sql and "ai_profile_coin_locks" in sql:
            self.lookups += 1
            if self.release and self.lookups == 1:
                await self._conn.execute("DELETE FROM ai_profile_coin_locks WHERE mandate_id = $1", self.holder_id)
            return None
        return await self._conn.fetchrow(sql, *args)


async def test_acquire_retries_when_the_holder_released_between_the_insert_and_the_lookup(h):
    holder = await profile(h, status="PAUSED", name="m1-holder")
    taker = await profile(h, status="STOPPED", name="m1-taker")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, holder.id, holder.instruments)
        await pl.acquire_locks(Interleave(conn, holder.id, "BTC", release_on_lookup=True), taker.id, taker.instruments)
    assert await locks(h, taker) == {"BTC"}


async def test_acquire_raises_coin_locked_when_it_still_cannot_take_the_lock(h):
    holder = await profile(h, status="PAUSED", name="m1-holder2")
    taker = await profile(h, status="STOPPED", name="m1-taker2")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, holder.id, holder.instruments)
        proxy = Interleave(conn, holder.id, "BTC", release_on_lookup=False)
        with pytest.raises(pl.LifecycleError) as e:
            await pl.acquire_locks(proxy, taker.id, taker.instruments)
    assert e.value.code == "COIN_LOCKED" and proxy.lookups == pl.LOCK_ATTEMPTS
    assert await locks(h, taker) == set()


# ------------------------------------------------------------------------- M3: the coin lock before any approval


async def _debate(h, instrument=BTC, recommendation="BUY") -> uuid.UUID:
    run_id = uuid.uuid4()
    await h.pool.execute(
        "INSERT INTO debate_runs (id, instrument_id, status, trigger_source) VALUES ($1, $2, 'PENDING_APPROVAL', 'test')",
        run_id, instrument)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning)
           VALUES ($1, $2, $3, 0.7, 5, 'r')""", run_id, instrument, recommendation)
    return run_id


async def test_the_inbox_refuses_a_debate_on_a_coin_another_profile_holds(h):
    holder = await profile(h, status="PAUSED", name="inbox-holder")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, holder.id, holder.instruments)
    await h.mandate(1, instruments=(BTC,), name="inbox-other", budget=1000)  # ACTIVE, no lock: the debate resolves here
    run_id = await _debate(h)
    await h.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 50000}), ex=60)
    async with h.client() as c:
        sizing = await c.get(f"{AGENTS}/{run_id}/sizing")
        approve = await c.post(f"{AGENTS}/{run_id}/approve", headers=h.headers)
    assert sizing.json()["ok"] is False and sizing.json()["code"] == "COIN_LOCKED"
    assert approve.status_code == 409 and approve.json()["detail"].startswith("COIN_LOCKED:")
    assert "inbox-holder" in approve.json()["detail"]
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_debate_routing_refuses_a_coin_another_profile_holds_before_writing_an_approval(h, routing):
    holder = await profile(h, status="PAUSED", name="route-holder")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, holder.id, holder.instruments)
    m = await h.mandate(1, instruments=(BTC,), name="route-other", budget=1000)
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(conn, uuid.uuid4(), BTC, "Buy", 0.7, 5.0, mandate_id=str(m.id))
    assert status == "NO_TRADE"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


# ------------------------------------------------------------------------- M5: stop a halted profile, archive


async def test_a_halted_profile_can_be_stopped_without_totp(h):
    m = await profile(h, status="ACTIVE")
    await halt(h, m)
    assert await status_of(h, m) == "HALTED"
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/stop", json={"close": False}, headers=h.headers)  # no TOTP
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "STOPPED" and await locks(h) == set()  # flat: finished at once


async def test_a_halted_profile_with_a_position_goes_to_stopping(h):
    m = await profile(h)
    await start(h, m)
    await hold(h, m)
    await halt(h, m)
    async with h.pool.acquire() as conn:
        assert (await pl.stop(conn, m.id, actor="t", close=False)).status is MandateStatus.STOPPING
    assert await locks(h, m) == {"BTC"}


async def test_archive_from_stopped_and_from_a_flat_halted_or_paused_profile(h):
    for how in ("STOPPED", "PAUSED", "HALTED"):
        m = await profile(h, status="STOPPED", name=f"arch-{how}")
        if how != "STOPPED":
            await start(h, m)
            if how == "PAUSED":
                async with h.pool.acquire() as conn:
                    await pl.pause(conn, m.id, actor="t")
            else:
                await halt(h, m)
            assert await locks(h, m) == {"BTC"}
        async with h.client() as c:
            res = await c.post(f"{V1}/{m.id}/archive", headers=h.headers)  # no TOTP
        assert res.status_code == 200 and res.json()["status"] == "ARCHIVED", (how, res.text)
        assert await locks(h) == set()
        kinds = [r["kind"] for r in await h.pool.fetch(
            "SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
        assert kinds[-1] == "ARCHIVED"


async def test_archive_is_refused_for_active_stopping_and_a_halted_profile_with_a_position(h):
    active = await profile(h, status="STOPPED", name="arch-active")
    await start(h, active)
    async with h.pool.acquire() as conn:
        with pytest.raises(pl.LifecycleError) as e:
            await pl.archive(conn, active.id, actor="t")
    assert e.value.code == "NOT_ARCHIVABLE"
    await hold(h, active)
    await halt(h, active)
    async with h.client() as c:
        res = await c.post(f"{V1}/{active.id}/archive", headers=h.headers)
    assert res.status_code == 409 and res.json()["detail"].startswith("NOT_ARCHIVABLE:")
    assert await status_of(h, active) == "HALTED" and await locks(h, active) == {"BTC"}
