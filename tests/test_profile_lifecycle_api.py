"""ADR 0011 lifecycle (P0.4): Start / Pause / Stop / finish Stop through ``profile_lifecycle``, the coin locks, the
master switch, the capital cap, TOTP, the human-operator rule on the routes, the perps rules without the L1 lock, and
the coin-lock refusal of the Brain card and the mandate check."""

import asyncio
import hashlib
import time
import uuid
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution import profile_lifecycle as pl
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.mandates import MandateExposure, MandateOrder, check_mandate
from cryptonite_schemas.mandate import Mandate, MandateReject, MandateStatus
from cryptonite_schemas.profile import ExecutionMode, Market
from migrated_db import migrated_database
from test_brain_card_preflight import (  # noqa: F401  (the Brain session worker double)
    BTC,
    PREFLIGHT,
    worker,
)

D = Decimal
ETH = "ETHUSDT.BINANCE"
ADA = "ADAUSDT.BINANCE"
BTC_PERP = "BTCUSDT-PERP.BINANCE"
ON = pl.AutotradeSettings(enabled=True, max_allocated_pct=D("90"))
OFF = pl.AutotradeSettings(enabled=False, max_allocated_pct=D("90"))
V1 = "/api/v1/ai/mandates"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p04_lifecycle") as url:
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


async def profile(h, *, instruments=(BTC,), level=1, status="STOPPED", budget=1000, perps=False, name=None,
                  daily=3):
    """A profile row (STOPPED unless told), with a daily loss limit so it can start."""
    m = await h.mandate(level, instruments=instruments, budget=budget, status=status, name=name)
    await h.pool.execute("UPDATE ai_mandates SET daily_loss_limit_pct = $2, allow_perps = $3, markets = $4 WHERE id = $1",
                         m.id, D(daily), perps, ["SPOT", "PERP"] if perps else ["SPOT"])
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
    pid = uuid.uuid4().hex
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', 50000, 0.01, 50000, 'OPEN')""", pid, m.instance_id, instrument)
    return pid


# ------------------------------------------------------------------------- start


async def test_start_takes_the_coin_locks_and_goes_active(h):
    m = await profile(h, instruments=(BTC, ETH))
    got = await start(h, m, totp=False)  # an Ask profile needs no code
    assert got.status is MandateStatus.ACTIVE
    assert await locks(h, m) == {"BTC", "ETH"}
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'LIFECYCLE' "
                                 "AND outcome = 'STARTED'", m.id) == 1
    assert await h.pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'STARTED'", m.id) == 1


async def test_starting_an_active_profile_is_a_no_op_success(h):
    m = await profile(h)
    await start(h, m)
    again = await start(h, m, settings=OFF, totp=False)  # not even the switch is asked again
    assert again.status is MandateStatus.ACTIVE
    assert await h.pool.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'STARTED'", m.id) == 1


@pytest.mark.parametrize("level,mode", [(2, "VETO"), (3, "AUTO")])
async def test_the_master_switch_blocks_veto_and_auto_but_not_ask(h, level, mode):
    ask = await profile(h, instruments=(ETH,), level=1)
    assert (await start(h, ask, settings=OFF, totp=False)).status is MandateStatus.ACTIVE
    m = await profile(h, level=level)
    e = await refused(start(h, m, settings=OFF))
    assert (e.code, e.detail["mode"]) == ("AUTOTRADE_DISABLED", mode)
    assert await locks(h, m) == set() and (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED
    assert (await start(h, m, settings=ON)).status is MandateStatus.ACTIVE


async def test_the_master_switch_is_read_from_the_stored_setting(h):
    m = await profile(h, level=3)
    async with h.pool.acquire() as conn:
        assert (await refused(pl.start(conn, m.id, actor="t", totp_ok=True, settings=await pl.load_settings(conn)))
                ).code == "AUTOTRADE_DISABLED"
        await conn.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb)")
        got = await pl.start(conn, m.id, actor="t", totp_ok=True, settings=await pl.load_settings(conn))
    assert got.status is MandateStatus.ACTIVE


async def test_an_invalid_profile_does_not_start(h):
    m = await profile(h)
    await h.pool.execute("UPDATE ai_mandates SET daily_loss_limit_pct = NULL WHERE id = $1", m.id)
    e = await refused(start(h, m))
    assert e.code == "PROFILE_INVALID" and "DAILY_LOSS_REQUIRED" in e.detail["codes"]
    assert await locks(h) == set()


@pytest.mark.parametrize("level,status,needs", [(1, "STOPPED", False), (1, "PAUSED", True), (1, "HALTED", True),
                                                (2, "STOPPED", True), (3, "STOPPED", True)])
async def test_totp_rules(h, level, status, needs):
    m = await profile(h, level=level, status=status)
    if needs:
        assert (await refused(start(h, m, totp=False))).code == "TOTP_REQUIRED"
        assert (await load_mandate(h.pool, m.id)).status.value == status
    assert (await start(h, m, totp=True)).status is MandateStatus.ACTIVE
    assert await h.pool.fetchval("SELECT paused_reason FROM ai_mandates WHERE id = $1", m.id) is None


async def test_a_paused_profile_restarts_with_the_locks_it_kept(h):
    m = await profile(h)
    await start(h, m)
    async with h.pool.acquire() as conn:
        await pl.pause(conn, m.id, actor="t", reason="lunch")
    assert await locks(h, m) == {"BTC"}
    assert (await start(h, m)).status is MandateStatus.ACTIVE


async def test_the_capital_cap_counts_every_holding_status(h):
    equity = await h.pool.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = 'PAPER-ACCOUNT-001'")
    cap = equity * D("0.9")
    a = await profile(h, instruments=(BTC,), budget=int(cap) - 1000)
    b = await profile(h, instruments=(ETH,), budget=1500)
    c = await profile(h, instruments=(ADA,), budget=1000)
    await start(h, a)
    e = await refused(start(h, b))
    assert e.code == "CAPITAL_OVER_ALLOCATED"
    assert {k: D(e.detail[k]) for k in ("allocated", "budget", "equity", "cap_pct")} == {
        "allocated": D(int(cap) - 1000), "budget": D(1500), "equity": equity, "cap_pct": D(90)}
    assert (await start(h, c)).status is MandateStatus.ACTIVE  # exactly at the cap is allowed
    async with h.pool.acquire() as conn:
        await pl.pause(conn, a.id, actor="t")  # a paused profile still holds its capital
    assert (await refused(start(h, b))).code == "CAPITAL_OVER_ALLOCATED"
    await h.pool.execute("UPDATE ai_mandates SET status = 'STOPPED' WHERE id = $1", a.id)  # stopped: released
    assert (await start(h, b)).status is MandateStatus.ACTIVE


async def test_the_cap_is_a_setting(h):
    equity = await h.pool.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = 'PAPER-ACCOUNT-001'")
    m = await profile(h, budget=int(equity * D("0.5")))
    e = await refused(start(h, m, settings=pl.AutotradeSettings(True, D("40"))))
    assert e.code == "CAPITAL_OVER_ALLOCATED" and e.detail["cap_pct"] == "40"


# ------------------------------------------------------------------------- coin locks


async def test_a_coin_another_profile_holds_names_the_holder(h):
    a = await profile(h, instruments=(BTC,), name="holder-a")
    b = await profile(h, instruments=(ETH, BTC_PERP), perps=True, name="loser-b")
    await start(h, a)
    e = await refused(start(h, b))  # BTCUSDT-PERP is the BTC coin too
    assert (e.code, e.detail["coin"], e.detail["holder_name"], e.detail["holder_id"]) == (
        "COIN_LOCKED", "BTC", "holder-a", str(a.id))
    assert (await load_mandate(h.pool, b.id)).status is MandateStatus.STOPPED
    assert await locks(h, b) == set()  # ETH, inserted before the conflict, was rolled back with the rest


async def test_concurrent_starts_on_a_shared_coin_have_exactly_one_winner(h):
    for round_ in range(4):
        await h.pool.execute("DELETE FROM ai_profile_coin_locks")
        await h.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
        a = await profile(h, instruments=(BTC, ETH), name=f"race-a-{round_}")
        b = await profile(h, instruments=(ADA, BTC), name=f"race-b-{round_}")

        async def go(m):
            async with h.pool.acquire() as conn:
                try:
                    return await pl.start(conn, m.id, actor="t", totp_ok=True, settings=ON)
                except pl.LifecycleError as e:
                    return e

        ra, rb = await asyncio.gather(go(a), go(b))
        outcomes = [ra, rb]
        winners = [r for r in outcomes if isinstance(r, Mandate)]
        losers = [r for r in outcomes if isinstance(r, pl.LifecycleError)]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        assert losers[0].code == "COIN_LOCKED" and losers[0].detail["holder_name"] == winners[0].name
        loser = a if isinstance(ra, pl.LifecycleError) else b
        assert (await load_mandate(h.pool, loser.id)).status is MandateStatus.STOPPED
        assert await locks(h, loser) == set()
        assert await h.pool.fetchval("SELECT count(*) FROM ai_mandates WHERE status = 'ACTIVE' AND id = ANY($1)", [a.id, b.id]) == 1


# ------------------------------------------------------------------------- pause, stop, finish_stop


async def test_pause_keeps_the_locks_and_cancels_pending_entries(h):
    m = await profile(h)
    await start(h, m)
    async with h.pool.acquire() as conn:
        got = await pl.pause(conn, m.id, actor="t", reason="lunch")
        assert (await refused(pl.pause(conn, m.id, actor="t"))).code == "MANDATE_NOT_ACTIVE"
    assert got.status is MandateStatus.PAUSED and got.paused_reason == "lunch"
    assert await locks(h, m) == {"BTC"}


async def test_stop_of_a_flat_profile_finishes_at_once_and_releases_the_locks(h):
    m = await profile(h, instruments=(BTC, ETH))
    await start(h, m)
    async with h.pool.acquire() as conn:
        got = await pl.stop(conn, m.id, actor="t", close=False)
    assert got.status is MandateStatus.STOPPED and await locks(h) == set()
    kinds = [r["kind"] for r in await h.pool.fetch("SELECT kind FROM ai_autonomy_events WHERE mandate_id = $1 ORDER BY id", m.id)]
    assert kinds[-3:] == ["STARTED", "STOPPING", "STOPPED"]
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id = $1 AND outcome IN "
                                 "('STOPPING', 'STOPPED')", m.id) == 2


async def test_stopping_holds_the_locks_until_the_profile_is_flat(h):
    m = await profile(h)
    await start(h, m)
    pid = await hold(h, m)
    async with h.pool.acquire() as conn:
        got = await pl.stop(conn, m.id, actor="t", close=False)
        assert got.status is MandateStatus.STOPPING and await locks(h, m) == {"BTC"}
        assert await pl.finish_stop(conn, m.id) is False  # still holds a position
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0  # no close was asked for
        # the coin stays out of reach while it winds down
        clash = await profile(h, instruments=(BTC,), name="wants-btc")
        e = await refused(pl.start(conn, clash.id, actor="t", totp_ok=True, settings=ON))
        assert (e.code, e.detail["holder_id"]) == ("COIN_LOCKED", str(m.id))
        await h.pool.execute("UPDATE paper_positions SET status = 'CLOSED', quantity = 0 WHERE position_id = $1", pid)
        assert await pl.finish_stop(conn, m.id) is True
        assert await pl.finish_stop(conn, m.id) is False  # idempotent
    assert (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED and await locks(h, m) == set()
    async with h.pool.acquire() as conn:
        assert (await pl.start(conn, clash.id, actor="t", totp_ok=True, settings=ON)).status is MandateStatus.ACTIVE


async def test_a_working_entry_order_keeps_the_profile_stopping(h):
    m = await profile(h)
    await start(h, m)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               status) VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, 'BUY', 'LIMIT', 0.01, 'SUBMITTED')""",
        f"w-{uuid.uuid4().hex[:8]}", m.instance_id, BTC)
    async with h.pool.acquire() as conn:
        assert (await pl.stop(conn, m.id, actor="t", close=False)).status is MandateStatus.STOPPING
        await h.pool.execute("UPDATE paper_orders SET status = 'CANCELED' WHERE instance_id = $1", m.instance_id)
        assert await pl.finish_stop(conn, m.id) is True


async def test_stop_with_close_queues_reduce_only_closes(h):
    m = await profile(h, instruments=(BTC, ETH))
    await start(h, m)
    await hold(h, m, BTC)
    await hold(h, m, ETH)
    async with h.pool.acquire() as conn:
        got = await pl.stop(conn, m.id, actor="t", close=True)
    assert got.status is MandateStatus.STOPPING
    cmds = await h.pool.fetch("SELECT command_type, payload, mandate_id, target_instance_id FROM execution_commands")
    assert len(cmds) == 2 and {c["command_type"] for c in cmds} == {"CLOSE_POSITION"}
    assert all(c["mandate_id"] == m.id and c["target_instance_id"] == m.instance_id for c in cmds)
    import json
    payloads = [json.loads(c["payload"]) if isinstance(c["payload"], str) else c["payload"] for c in cmds]
    assert all(p["reduce_only"] is True for p in payloads) and {p["instrument_id"] for p in payloads} == {BTC, ETH}
    assert await locks(h, m) == {"BTC", "ETH"}  # held until the closes fill


async def test_a_stopping_profile_can_be_taken_back_and_keeps_its_locks(h):
    m = await profile(h)
    await start(h, m)
    await hold(h, m)
    async with h.pool.acquire() as conn:
        await pl.stop(conn, m.id, actor="t", close=False)
    http = await h.pool.fetchval("SELECT status FROM ai_mandates WHERE id = $1", m.id)
    assert http == "STOPPING"
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/resume", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 200 and res.json()["status"] == "ACTIVE"
    assert await locks(h, m) == {"BTC"}


async def test_the_monitor_finishes_a_flat_stopping_profile(h):
    from types import SimpleNamespace

    from cryptonite_orchestrator.mandate_monitor import mandate_monitor_once

    m = await profile(h)
    await start(h, m)
    await h.pool.execute("UPDATE ai_mandates SET status = 'STOPPING' WHERE id = $1", m.id)

    class Dispatcher:
        node_cache = None

        def portfolio_equity(self):
            return 100000

    engine = SimpleNamespace(dispatcher=Dispatcher(), account_id="PAPER-ACCOUNT-001",
                             kill_switch=SimpleNamespace(is_latched=False, trip=lambda **kw: None))
    await mandate_monitor_once(engine, h.pool, None)
    assert (await load_mandate(h.pool, m.id)).status is MandateStatus.STOPPED and await locks(h, m) == set()


# ------------------------------------------------------------------------- the routes need a human operator


async def machine(h, scopes=("operator",)) -> dict[str, str]:
    token = f"p04-{uuid.uuid4().hex}"
    await h.pool.execute("INSERT INTO machine_api_tokens (token_hash, name, scopes) VALUES ($1, $2, $3)",
                         hashlib.sha256(token.encode()).hexdigest(), f"p04-{uuid.uuid4().hex[:6]}", list(scopes))
    return {"X-API-Token": token}


@pytest.mark.parametrize("path,body", [
    ("start", {}), ("pause", {}), ("stop", {"close": True}), ("resume", {}), ("archive", {}), ("autonomy/confirm", {"level": 2}),
])
async def test_lifecycle_routes_refuse_a_machine_token(h, path, body):
    m = await profile(h, status="ACTIVE")
    async with h.client() as c:
        c.cookies.clear()
        c.headers.update(await machine(h))
        res = await c.post(f"{V1}/{m.id}/{path}", json=body)
    assert res.status_code == 403, res.text
    assert (await load_mandate(h.pool, m.id)).status is MandateStatus.ACTIVE


async def test_risk_adding_routes_refuse_a_machine_token(h):
    m = await profile(h, status="ACTIVE")
    async with h.client() as c:
        c.cookies.clear()
        c.headers.update(await machine(h))
        edit = await c.patch(f"{V1}/{m.id}", json={"max_open_positions": 9, "totp_code": "123456"})
        create = await c.post(V1, json={"name": "x", "capital_budget_usd": 100, "instruments": [BTC],
                                        "max_position_pct": 10})
    assert edit.status_code == 403 and create.status_code == 403


async def test_start_stop_pause_through_the_routes(h):
    m = await profile(h, instruments=(BTC, ETH))
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/start", json={}, headers=h.headers)
        assert res.status_code == 200 and res.json()["status"] == "ACTIVE", res.text
        assert (res.json()["execution_mode"], res.json()["markets"]) == ("ASK", ["SPOT"])
        paused = await c.post(f"{V1}/{m.id}/pause", json={"reason": "lunch"}, headers=h.headers)
        assert paused.json()["status"] == "PAUSED" and paused.json()["paused_reason"] == "lunch"
        stopped = await c.post(f"{V1}/{m.id}/stop", json={"close": False}, headers=h.headers)
        assert stopped.json()["status"] == "STOPPED"
        assert await locks(h) == set()


@pytest.mark.parametrize("setup,expect_status,code", [("veto", 409, "AUTOTRADE_DISABLED"), ("noloss", 422, "PROFILE_INVALID")])
async def test_route_refusals_are_code_prefixed(h, setup, expect_status, code):
    m = await profile(h, level=2 if setup == "veto" else 1)
    if setup == "noloss":
        await h.pool.execute("UPDATE ai_mandates SET daily_loss_limit_pct = NULL WHERE id = $1", m.id)
    async with h.client() as c:
        res = await c.post(f"{V1}/{m.id}/start", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == expect_status and res.json()["detail"].startswith(f"{code}:"), res.text


async def test_the_route_start_of_a_veto_profile_needs_totp(h):
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb)")
    m = await profile(h, level=2)
    async with h.client() as c:
        no_code = await c.post(f"{V1}/{m.id}/start", json={}, headers=h.headers)
        assert no_code.status_code == 401 and no_code.json()["detail"].startswith("TOTP_REQUIRED")
        ok = await c.post(f"{V1}/{m.id}/start", json={"totp_code": h.totp.now()}, headers=h.headers)
    assert ok.status_code == 200 and ok.json()["status"] == "ACTIVE"


# ------------------------------------------------------------------------- the operator sets the mode


async def test_the_operator_sets_the_mode_directly(h):
    m = await profile(h, status="ACTIVE")
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb)")  # D10
    async with h.client() as c:
        no_code = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 3}, headers=h.headers)
        assert no_code.status_code == 401  # raising needs TOTP, and no gate or proposal stands in the way
        up = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 3, "totp_code": h.totp.now()}, headers=h.headers)
        assert up.status_code == 200 and up.json()["level"] == 3, up.text
        down = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 1}, headers=h.headers)  # lowering: free
        assert down.status_code == 200 and down.json()["level"] == 1
        same = await c.post(f"{V1}/{m.id}/autonomy/confirm", json={"level": 1}, headers=h.headers)
        assert same.status_code == 409 and same.json()["detail"] == "ALREADY_AT_LEVEL"
    ev = await h.pool.fetchrow("SELECT detail FROM ai_autonomy_events WHERE mandate_id = $1 AND kind = 'MODE_SET'", m.id)
    assert ev is not None and "health_score" in str(ev["detail"])


# ------------------------------------------------------------------------- perps at any mode, markets are the truth


def mandate_model(**kw) -> Mandate:
    base = dict(id=uuid.uuid4(), name="m", capital_budget_usd=100000, instruments=[BTC], max_position_pct=10)
    return Mandate(**{**base, **kw})


@pytest.mark.parametrize("level", [1, 2, 3])
def test_perps_are_valid_at_every_mode(level):
    m = mandate_model(instruments=[BTC_PERP], allow_perps=True, autonomy_level=level)
    assert m.allow_perps and Market.PERP in m.markets and m.max_leverage == 3
    assert m.execution_mode is [ExecutionMode.ASK, ExecutionMode.VETO, ExecutionMode.AUTO][level - 1]


def test_markets_are_the_truth_and_allow_perps_follows():
    assert mandate_model(markets=["SPOT"]).allow_perps is False
    m = mandate_model(markets=["SPOT", "PERP"], allow_perps=False)
    assert m.allow_perps is True
    assert mandate_model(instruments=[BTC_PERP], allow_perps=False).allow_perps is False  # a listed perp grants nothing
    with pytest.raises(ValueError, match="SHORT_NEEDS_MARGIN_OR_PERP"):
        mandate_model(direction="SHORT")


def test_a_running_mandate_gets_the_default_daily_limit_and_cannot_clear_it():
    assert mandate_model().daily_loss_limit_pct == D("3")
    with pytest.raises(ValueError, match="DAILY_LOSS_REQUIRED"):
        mandate_model(daily_loss_limit_pct=None)
    assert mandate_model(status="PAUSED", daily_loss_limit_pct=None).daily_loss_limit_pct is None


def test_exits_are_never_refused_by_the_status_or_the_lock_but_entries_are():
    owner = uuid.uuid4()
    buy = MandateOrder(BTC, "BUY", D("0.01"), D("50000"))
    sell = MandateOrder(BTC, "SELL", D("0.01"), D("50000"))
    held = MandateExposure(instrument_qty=D("0.01"), instrument_usd=D("500"), gross_usd=D("500"))
    for status in ("PAUSED", "STOPPING", "STOPPED", "HALTED"):
        m = mandate_model(status=status, autonomy_level=3, daily_loss_limit_pct=None)
        assert check_mandate(m, buy, MandateExposure(), approval_ok=True) is MandateReject.HALTED
        assert check_mandate(m, sell, held, approval_ok=False, coin_owner=owner) is None
    live = mandate_model(autonomy_level=3)
    assert check_mandate(live, buy, MandateExposure(), approval_ok=False) is None


def test_an_entry_on_a_coin_another_mandate_holds_is_coin_locked_not_a_breach():
    other, live = uuid.uuid4(), mandate_model(autonomy_level=3)
    buy = MandateOrder(BTC, "BUY", D("0.01"), D("50000"))
    reject = check_mandate(live, buy, MandateExposure(), approval_ok=True, coin_owner=other)
    assert reject is MandateReject.COIN_LOCKED
    from cryptonite_schemas.mandate import BREACH_CODES
    assert reject not in BREACH_CODES
    assert check_mandate(live, buy, MandateExposure(), approval_ok=True, coin_owner=live.id) is None
    assert check_mandate(live, buy, MandateExposure(), approval_ok=True, coin_owner=None) is None
    sell = MandateOrder(BTC, "SELL", D("0.01"), D("50000"))
    assert check_mandate(live, sell, MandateExposure(instrument_qty=D("0.01")), approval_ok=True, coin_owner=other) is None


def test_a_perp_order_needs_perp_in_markets_not_a_level():
    perp = MandateOrder(BTC_PERP, "BUY", D("0.01"), D("50000"), leverage=2)
    allowed = mandate_model(instruments=[BTC_PERP], markets=["PERP"], autonomy_level=3)
    spot_only = mandate_model(instruments=[BTC_PERP], markets=["SPOT"], autonomy_level=1)
    assert check_mandate(allowed, perp, MandateExposure(), approval_ok=True) is None
    assert check_mandate(spot_only, perp, MandateExposure(), approval_ok=True) is MandateReject.PERPS_NOT_ALLOWED


# ------------------------------------------------------------------------- the editor and the create route


async def test_the_allow_perps_edit_adds_perp_to_markets_at_any_mode(h):
    m = await profile(h, level=2, status="ACTIVE")
    async with h.client() as c:
        res = await c.patch(f"{V1}/{m.id}", json={"allow_perps": True, "totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 200, res.text
    assert res.json()["allow_perps"] is True and res.json()["markets"] == ["PERP", "SPOT"]
    assert res.json()["autonomy_level"] == 2 and res.json()["max_leverage"] == 3
    row = await h.pool.fetchrow("SELECT allow_perps, markets FROM ai_mandates WHERE id = $1", m.id)
    assert row["allow_perps"] is True and set(row["markets"]) == {"SPOT", "PERP"}


async def test_turning_perps_off_removes_perp_from_markets(h):
    m = await profile(h, level=1, status="ACTIVE", perps=True)
    async with h.client() as c:
        res = await c.patch(f"{V1}/{m.id}", json={"allow_perps": False, "max_leverage": 1}, headers=h.headers)
    assert res.status_code == 200, res.text
    row = await h.pool.fetchrow("SELECT allow_perps, markets FROM ai_mandates WHERE id = $1", m.id)
    assert row["allow_perps"] is False and row["markets"] == ["SPOT"]


async def test_a_profile_field_edit_that_adds_risk_needs_totp(h):
    m = await profile(h, status="ACTIVE")
    async with h.client() as c:
        raise_ = await c.patch(f"{V1}/{m.id}", json={"max_open_positions": 9}, headers=h.headers)
        assert raise_.status_code == 401
        lower = await c.patch(f"{V1}/{m.id}", json={"max_open_positions": 2}, headers=h.headers)
        assert lower.status_code == 200 and lower.json()["max_open_positions"] == 2


async def test_an_instrument_edit_moves_the_locks_and_respects_other_holders(h):
    a = await profile(h, instruments=(BTC,), name="edit-a")
    b = await profile(h, instruments=(ETH,), name="edit-b")
    await start(h, a)
    await start(h, b)
    async with h.client() as c:
        clash = await c.patch(f"{V1}/{a.id}", json={"instruments": [BTC, ETH], "totp_code": h.totp.now()}, headers=h.headers)
        assert clash.status_code == 409 and clash.json()["detail"].startswith("COIN_LOCKED:") and "edit-b" in clash.json()["detail"]
        assert await locks(h, a) == {"BTC"}
        async with h.pool.acquire() as conn:
            await pl.stop(conn, b.id, actor="t", close=False)  # b is flat: ETH is released
        # a changed instrument, no new risk beyond a coin it may now take: BTC is released, ETH locked
        # the clash above spent this step's code (each works once): the next step's is inside the accepted window
        swap = await c.patch(f"{V1}/{a.id}", json={"instruments": [ETH], "totp_code": h.totp.at(time.time() + 30)},
                             headers=h.headers)
    assert swap.status_code == 200, swap.text
    assert await locks(h, a) == {"ETH"}


async def test_creating_a_mandate_on_a_held_coin_is_refused(h):
    holder = await profile(h, instruments=(BTC,), name="create-holder")
    await start(h, holder)
    async with h.client() as c:
        res = await c.post(V1, json={"name": "newcomer", "capital_budget_usd": 500, "instruments": [BTC],
                                     "max_position_pct": 10, "totp_code": h.totp.now()}, headers=h.headers)
    # LOW: a refused Start is a 201 that says so (a 4xx invited a retry of the create: "name exists")
    body = res.json()
    assert res.status_code == 201 and body["started"] is False, res.text
    assert body["start_error"]["code"] == "COIN_LOCKED" and "create-holder" in body["start_error"]["detail"]
    assert body["mandate"]["status"] == "STOPPED" and body["mandate"]["name"] == "newcomer"
    assert await h.pool.fetchval("SELECT status FROM ai_mandates WHERE name = 'newcomer'") == "STOPPED"  # created, not running
    assert await locks(h) == {"BTC"} and await locks(h, holder) == {"BTC"}


async def test_creating_over_the_capital_cap_leaves_the_mandate_stopped(h):
    equity = await h.pool.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = 'PAPER-ACCOUNT-001'")
    async with h.client() as c:
        res = await c.post(V1, json={"name": "too-big", "capital_budget_usd": str(equity), "instruments": [ETH],
                                     "max_position_pct": 10, "totp_code": h.totp.now()}, headers=h.headers)
    body = res.json()
    assert res.status_code == 201 and body["started"] is False, res.text
    assert body["start_error"]["code"] == "CAPITAL_OVER_ALLOCATED" and body["mandate"]["status"] == "STOPPED"
    assert await h.pool.fetchval("SELECT status FROM ai_mandates WHERE name = 'too-big'") == "STOPPED"
    assert await locks(h) == set()


async def test_a_new_mandate_takes_its_locks_and_stores_the_profile_fields(h):
    async with h.client() as c:
        res = await c.post(V1, json={"name": "fresh", "capital_budget_usd": 500, "instruments": [ETH], "max_position_pct": 10,
                                     "direction": "BOTH", "markets": ["SPOT", "PERP"], "style": "INTRADAY",
                                     "max_open_positions": 2, "totp_code": h.totp.now()}, headers=h.headers)
    assert res.status_code == 201, res.text
    body = res.json()
    assert (body["direction"], body["style"], body["max_open_positions"], body["markets"], body["allow_perps"]) == (
        "BOTH", "INTRADAY", 2, ["PERP", "SPOT"], True)
    assert await locks(h) == {"ETH"}


# ------------------------------------------------------------------------- the Brain card and the coin lock


async def test_a_brain_card_on_a_coin_another_profile_holds_is_coin_locked(h, worker):  # noqa: F811
    holder = await profile(h, instruments=(BTC,), status="PAUSED", name="the-holder")
    async with h.pool.acquire() as conn:
        await pl.acquire_locks(conn, holder.id, holder.instruments)
    await h.mandate(1, instruments=(BTC,), name="the-other", budget=1000)  # ACTIVE, no lock: the card would resolve here
    async with h.client() as c:
        res = await c.get(PREFLIGHT.format(sid=uuid.uuid4(), wid="card-1"))
    body = res.json()
    assert body["ok"] is False and body["code"] == "COIN_LOCKED", body
    assert "the-holder" in body["detail"]


async def test_the_coin_lock_helper_serves_the_debate_entry_too(h):
    holder = await profile(h, instruments=(BTC,), status="PAUSED", name="debate-holder")
    other = await h.mandate(1, instruments=(BTC,), name="debate-other", budget=1000)
    async with h.pool.acquire() as conn:
        assert await pl.coin_lock_conflict(conn, other, BTC_PERP, "debate") is None  # nobody holds BTC yet
        await pl.acquire_locks(conn, holder.id, holder.instruments)
        detail = await pl.coin_lock_conflict(conn, other, BTC_PERP, "debate")
        assert detail.startswith("COIN_LOCKED:") and "debate-holder" in detail and detail.endswith("reject this debate")
        assert await pl.coin_lock_conflict(conn, holder, BTC, "debate") is None  # the holder itself is fine


# ------------------------------------------------------------------------- the two settings


def test_the_autotrade_settings_are_registered_and_raising_them_needs_totp():
    from cryptonite_api.settings_registry import REGISTRY, validate_value, write_needs_totp

    by_key = {x.key: x for x in REGISTRY}
    enabled, cap = by_key["autotrade.enabled"], by_key["autotrade.max_allocated_pct"]
    assert (enabled.default, enabled.minimum, enabled.maximum) == (0, 0, 1)
    assert [c[1] for c in enabled.choices] == ["Off", "On"]
    assert (cap.default, cap.minimum, cap.maximum) == (90, 10, 100)
    assert write_needs_totp(enabled, D(0), D(1)) is True and write_needs_totp(enabled, D(1), D(0)) is False
    assert write_needs_totp(cap, D(90), D(95)) is True and write_needs_totp(cap, D(90), D(50)) is False
    for bad in (D(101), D(9)):
        with pytest.raises(ValueError):
            validate_value(cap, bad)
    assert by_key["guard.perps_level"].default == "Any execution mode (ADR 0011)" and by_key["guard.perps_level"].adr == "0011"


def test_stored_settings_fall_back_to_the_safe_side():
    assert pl.parse_settings(None) == pl.AutotradeSettings(False, D(90))
    assert pl.parse_settings({"autotrade.enabled": "1", "autotrade.max_allocated_pct": '"50"'}) == pl.AutotradeSettings(True, D(50))
    assert pl.parse_settings({"autotrade.enabled": "7", "autotrade.max_allocated_pct": "junk"}) == pl.AutotradeSettings(False, D(90))


# ------------------------------------------------------------------------- the audit kinds (migration 050)


def test_050_widens_the_event_kinds_and_refuses_a_downgrade_over_them():
    import asyncio
    import os
    import subprocess
    import sys

    import asyncpg
    from migrated_db import REPO_ROOT

    async def run(url, sql, *args):
        c = await asyncpg.connect(url)
        try:
            return await c.fetchval(sql, *args)
        finally:
            await c.close()

    with migrated_database("cryptonite_ci_p04_kinds") as url:
        mid = asyncio.run(run(url, """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct)
                                      VALUES ('k', 100, ARRAY['BTCUSDT.BINANCE'], 10) RETURNING id"""))
        for kind in ("STARTED", "STOPPING", "STOPPED", "MODE_SET", "ARCHIVED"):
            asyncio.run(run(url, "INSERT INTO ai_autonomy_events (mandate_id, kind) VALUES ($1, $2) RETURNING 1", mid, kind))
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(run(url, "INSERT INTO ai_autonomy_events (mandate_id, kind) VALUES ($1, 'NOPE') RETURNING 1", mid))
        cmd = [sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", "downgrade", "049"]
        env = {**os.environ, "DATABASE_URL": url}
        refused = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        assert refused.returncode != 0 and "STARTED/STOPPING/STOPPED/MODE_SET/ARCHIVED" in refused.stderr + refused.stdout
        asyncio.run(run(url, "DELETE FROM ai_autonomy_events WHERE kind IN ('STARTED','STOPPING','STOPPED','MODE_SET','ARCHIVED') RETURNING 1"))
        assert subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True).returncode == 0
