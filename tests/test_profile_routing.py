"""ADR 0011: profile ownership, refusal ordering, execution modes and debate budgets."""

import asyncio
import datetime
import importlib
import json
import sys
import uuid
from decimal import Decimal
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
    await harness.pool.execute("DELETE FROM ai_decision_traces")
    await harness.pool.execute("DELETE FROM system_settings WHERE key IN ('LLM_DAILY_BUDGET_USD','autotrade.enabled')")
    await harness.pool.execute("UPDATE paper_accounts SET total_equity=10000, cash_balance=10000")
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
           autonomy_level, markets, max_leverage, direction, style, allow_perps)
           VALUES ($1, 10000, ARRAY[$2], 25, 'ACTIVE', $3, ARRAY[$4]::text[], $5, 'LONG', 'SWING', $6) RETURNING id""",
        f"profile-{uuid.uuid4()}", instrument, level, market, 3 if market != "SPOT" else 1, market == "PERP")
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


@pytest.mark.parametrize("stop", [None, 60000, 61000, float("nan")])
async def test_spot_entry_needs_a_below_entry_structural_stop_before_approval(h, stop):
    """A profile spot BUY with no usable loss-side stop cannot create operator work."""
    m = await profile(h, level=1)
    run = uuid.uuid4()
    await h.pool.execute(
        "INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','test')",
        run, SPOT,
    )
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_entry(
            conn, run, SPOT, "BUY", .8, 2.5, mandate_id=str(m.id), now=NOW,
            stop_loss=stop, take_profit=66000, reference_price=60000,
            redis=h.redis, grade_context=BULL,
        )
    assert status == "NO_TRADE"
    assert await h.pool.fetchval(
        "SELECT reason FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run,
    ) == "STOP_REQUIRED"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE mandate_id=$1", m.id) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands WHERE mandate_id=$1", m.id) == 0


async def test_spot_entry_carries_structural_stop_without_spot_stop_price(h):
    """A valid profile spot stop survives the route in the exit monitor's existing field."""
    m = await profile(h, level=1)
    (status, _), run = await route(h, m.id)
    assert status == "PENDING_APPROVAL"
    row = await h.pool.fetchrow("SELECT payload FROM ai_approvals WHERE source_ref=$1", str(run))
    payload = json.loads(row["payload"])
    assert payload["proposed_stop_loss"] == 57000
    assert "stop_price" not in payload


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
    await profile(h, market=market, level=level, active_hours_utc="08:00-22:00")
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


async def test_veto_outside_active_hours_expires_without_an_order(h):
    from cryptonite_execution.approvals import sweep_expired
    await profile(h, level=2, active_hours_utc="00:00-01:00")
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1')")
    (status, _), run = await route(h)
    assert status == "PENDING_APPROVAL"
    assert await h.pool.fetchval("SELECT timeout_policy FROM ai_approvals WHERE source_ref=$1", str(run)) == "DENY"
    async with h.pool.acquire() as conn:
        await sweep_expired(conn, NOW + datetime.timedelta(hours=1))
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE source_ref=$1", str(run)) == "EXPIRED"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_debate_and_dry_run_route_decisions_keep_the_plan_path(h):
    m = await profile(h, level=3, dry_run=True)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1')")
    (status, _), run = await route(h)
    assert status == "COMPLETED"
    rows = await h.pool.fetch("SELECT stage,outcome,detail FROM ai_profile_decisions WHERE run_id=$1 ORDER BY id", run)
    assert [(r["stage"], r["outcome"]) for r in rows] == [("DEBATE", "BUY"), ("ROUTE", "DRY_RUN_ROUTED")]
    assert json.loads(rows[0]["detail"])["conviction"] == .8
    assert await h.pool.fetchval("SELECT mandate_id FROM execution_commands") == m.id


@pytest.mark.parametrize("level,outcome", [(1, "APPROVAL_CREATED"), (2, "VETO_WINDOW")])
async def test_dry_run_pending_approvals_log_their_actual_mode(h, level, outcome):
    await profile(h, level=level, dry_run=True, active_hours_utc="08:00-22:00")
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('autotrade.enabled','1')")
    (status, _), run = await route(h)
    assert status == "PENDING_APPROVAL"
    assert await h.pool.fetchval(
        "SELECT outcome FROM ai_profile_decisions WHERE run_id=$1 AND stage='ROUTE'", run
    ) == outcome
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals WHERE source_ref=$1", str(run)) == 1
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_triage_that_spends_past_the_profile_share_is_refused(h, monkeypatch):
    from types import SimpleNamespace

    m = await profile(h, max_debates_per_day=2)
    await profile(h, lock=False, max_debates_per_day=6)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','32')")
    prior_trace = uuid.uuid4()
    await h.pool.execute(
        "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) VALUES($1,'debate.triage','test','OK',$2)",
        prior_trace, str(m.id),
    )
    await h.pool.execute(
        """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,total_cost_usd,trace_id)
           VALUES($1,'debate.triage','test',1,1,7.99,$2)""", str(prior_trace), prior_trace,
    )

    async def spend_during_triage(gateway, brief, **kwargs):
        trace = uuid.uuid4()
        await h.pool.execute(
            "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) VALUES($1,'debate.triage','test','OK',$2)",
            trace, str(m.id),
        )
        await h.pool.execute(
            """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,total_cost_usd,trace_id)
               VALUES($1,'debate.triage','test',1,1,.02,$2)""", str(trace), trace,
        )
        return SimpleNamespace(output=SimpleNamespace(worth_debate=True), trace_id=uuid.uuid4())

    monkeypatch.setattr(gate, "TRIAGE", SimpleNamespace(role="triage", run=spend_during_triage))
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, object(), "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (False, "AI_BUDGET_EXHAUSTED")
    assert await h.redis.get(gate.profile_cap_key(m.id, NOW.timestamp())) is None
    assert not await h.redis.exists(f"debate:cooldown:{SPOT}:1h")
    assert await h.pool.fetchval(
        "SELECT reason FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='GATE'", m.id
    ) == "AI_BUDGET_EXHAUSTED"


async def test_concurrent_profile_triage_calls_share_one_budget_boundary(h, monkeypatch):
    from types import SimpleNamespace

    m = await profile(h, max_debates_per_day=2)
    await profile(h, lock=False, max_debates_per_day=6)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','32')")
    prior_trace = uuid.uuid4()
    await h.pool.execute(
        "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) VALUES($1,'debate.triage','test','OK',$2)",
        prior_trace, str(m.id),
    )
    await h.pool.execute(
        """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,total_cost_usd,trace_id)
           VALUES($1,'debate.triage','test',1,1,7.9,$2)""", str(prior_trace), prior_trace,
    )
    triage_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def spend_once(gateway, brief, **kwargs):
        nonlocal calls
        calls += 1
        triage_started.set()
        await release_first.wait()
        trace = uuid.uuid4()
        await h.pool.execute(
            "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) VALUES($1,'debate.triage','test','OK',$2)",
            trace, str(m.id),
        )
        await h.pool.execute(
            """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,total_cost_usd,trace_id)
               VALUES($1,'debate.triage','test',1,1,.2,$2)""", str(trace), trace,
        )
        return SimpleNamespace(output=SimpleNamespace(worth_debate=True), trace_id=uuid.uuid4())

    monkeypatch.setattr(gate, "TRIAGE", SimpleNamespace(role="triage", run=spend_once))
    first = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    second = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "4h"})}
    first_task = asyncio.create_task(gate.pre_debate_gate(h.pool, h.redis, first, object(), "brief", NOW.timestamp()))
    await asyncio.wait_for(triage_started.wait(), timeout=5)
    second_task = asyncio.create_task(gate.pre_debate_gate(h.pool, h.redis, second, object(), "brief", NOW.timestamp()))
    await asyncio.sleep(0.1)
    release_first.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert [first_result.reason, second_result.reason] == ["AI_BUDGET_EXHAUSTED", "AI_BUDGET_EXHAUSTED"]
    assert calls == 1
    assert await h.pool.fetchval("SELECT SUM(total_cost_usd) FROM llm_cost_ledger") == Decimal("8.1")


async def test_profile_budget_lease_loss_fails_closed_without_releasing_another_owner(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield object()

    class Redis:
        key = None

        async def set(self, key, value, nx=False, ex=None):
            self.key = value
            return True

        async def get(self, key):
            return self.key

        async def eval(self, script, numkeys, key, token, *args):
            if "EXPIRE" in script:
                self.key = "replacement-owner"
                return 0
            if self.key == token:
                self.key = None
                return 1
            return 0

    async def budget_available(conn, mandate, now_s):
        return False

    monkeypatch.setattr(gate, "profile_budget_exhausted", budget_available)
    monkeypatch.setattr(gate, "PROFILE_BUDGET_LOCK_RENEW_S", .001)
    redis = Redis()
    with pytest.raises(gate.ProfileBudgetLockError, match="PROFILE_BUDGET_LOCK_LOST"):
        async with gate.profile_budget_guard(Pool(), redis, SimpleNamespace(id=uuid.uuid4())):
            await asyncio.sleep(.01)
    assert redis.key == "replacement-owner"


async def test_profile_budget_lock_acquisition_failure_fails_closed(monkeypatch):
    from types import SimpleNamespace

    class Redis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis unavailable")

    with pytest.raises(gate.ProfileBudgetLockError, match="PROFILE_BUDGET_LOCK_UNAVAILABLE"):
        async with gate.profile_budget_guard(object(), Redis(), SimpleNamespace(id=uuid.uuid4())):
            pytest.fail("profile call proceeded without its budget lock")


async def test_profile_budget_guard_rechecks_execution_day_after_midnight(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from cryptonite_agents.gateway import BudgetExceeded

    before = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)
    after = datetime.datetime(2027, 1, 1, 0, 0, 1, tzinfo=datetime.UTC)
    current_time = [before.timestamp()]
    observed_days = []

    @asynccontextmanager
    async def acquire():
        yield object()

    class Pool:
        def acquire(self):
            return acquire()

    async def budget_exhausted(conn, mandate, now_s):
        day = datetime.datetime.fromtimestamp(now_s, datetime.UTC).date()
        observed_days.append(day)
        return day == after.date()

    monkeypatch.setattr(gate, "_utc_now_s", lambda: current_time[0])
    monkeypatch.setattr(gate, "profile_budget_exhausted", budget_exhausted)
    monkeypatch.setattr(gate, "PROFILE_BUDGET_LOCK_RENEW_S", 5)
    with pytest.raises(BudgetExceeded, match="PROFILE_DAILY_SHARE_EXHAUSTED"):
        async with gate.profile_budget_guard(Pool(), _ProfileLeaseRedis(), SimpleNamespace(id=uuid.uuid4())):
            current_time[0] = after.timestamp()
    assert observed_days == [before.date(), after.date()]


async def test_parallel_slow_profile_calls_wait_for_healthy_budget_lease(monkeypatch):
    """A healthy slow model call must not make its queued sibling look budget-exhausted."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    redis = _ProfileLeaseRedis()
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    @asynccontextmanager
    async def acquire():
        yield object()

    class Pool:
        def acquire(self):
            return acquire()

    async def budget_available(conn, mandate, now_s):
        return False

    monkeypatch.setattr(gate, "profile_budget_exhausted", budget_available)
    monkeypatch.setattr(gate, "PROFILE_BUDGET_LOCK_RENEW_S", 5)
    profile = SimpleNamespace(id=uuid.uuid4())

    async def delayed_model_call(name):
        async with gate.profile_budget_guard(Pool(), redis, profile):
            calls.append(name)
            if name == "first analyst":
                entered_first.set()
                await release_first.wait()
            else:
                await asyncio.sleep(.01)

    first = asyncio.create_task(delayed_model_call("first analyst"))
    await asyncio.wait_for(entered_first.wait(), timeout=1)
    second = asyncio.create_task(delayed_model_call("second analyst"))
    try:
        await asyncio.sleep(.15)  # longer than the old admission cutoff and polling interval
    finally:
        release_first.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert results == [None, None]
    assert calls == ["first analyst", "second analyst"]


@pytest.mark.parametrize(
    "current_day_condition,expected_reason",
    [("budget", "AI_BUDGET_EXHAUSTED"), ("counter", "PROFILE_DEBATE_DAILY_CAP")],
)
async def test_profile_lease_wait_across_utc_midnight_uses_execution_day(
    h, monkeypatch, current_day_condition, expected_reason
):
    """A contender uses the day it acquires the lease for share checks and slot reservation."""
    from types import SimpleNamespace

    before = datetime.datetime(2026, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)
    after = datetime.datetime(2027, 1, 1, 0, 0, 1, tzinfo=datetime.UTC)
    current_time = [before.timestamp()]
    monkeypatch.setattr(gate, "_utc_now_s", lambda: current_time[0], raising=False)

    m = await profile(h, max_debates_per_day=1)
    await profile(h, lock=False, max_debates_per_day=9)
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','100') "
        "ON CONFLICT(key) DO UPDATE SET value='100'"
    )
    current_slot_key = gate.profile_cap_key(m.id, after.timestamp())
    if current_day_condition == "budget":
        trace_id = uuid.uuid4()
        await h.pool.execute(
            """INSERT INTO ai_decision_traces(id,role,model,status,mandate_id,created_at)
               VALUES($1,'debate.triage','test','ERROR',$2,$3)""",
            trace_id, str(m.id), after,
        )
        await h.pool.execute(
            """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,
               total_cost_usd,trace_id,created_at) VALUES($1,'debate.triage','test',1,1,11,$2,$3)""",
            str(trace_id), trace_id, after,
        )
    else:
        await h.redis.set(current_slot_key, 1, ex=gate.PROFILE_CAP_TTL_S)

    lock_key = f"debate_gate:profile_budget_lock:{m.id}"
    owner = "midnight-test-holder"
    assert await h.redis.set(lock_key, owner, nx=True, ex=gate.PROFILE_BUDGET_LOCK_TTL_S)
    waiting = asyncio.Event()

    class ContentionRedis:
        def __getattr__(self, name):
            return getattr(h.redis, name)

        async def set(self, key, value, **kwargs):
            result = await h.redis.set(key, value, **kwargs)
            if key == lock_key and not result:
                waiting.set()
            return result

    triage_calls = 0

    async def triage(*args, **kwargs):
        nonlocal triage_calls
        triage_calls += 1
        return SimpleNamespace(
            output=SimpleNamespace(worth_debate=True), trace_id=uuid.uuid4()
        )

    monkeypatch.setattr(gate, "TRIAGE", SimpleNamespace(role="debate.triage", run=triage))
    payload = {
        "instrument_id": SPOT,
        "candidate_payload": json.dumps({"timeframe": "1h"}),
    }
    task = asyncio.create_task(
        gate.pre_debate_gate(h.pool, ContentionRedis(), payload, object(), "brief", now_s=before.timestamp())
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        current_time[0] = after.timestamp()
        await h.redis.delete(lock_key)
        decision = await asyncio.wait_for(task, timeout=3)
    finally:
        await h.redis.delete(lock_key)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert (decision.allowed, decision.reason) == (False, expected_reason)
    assert triage_calls == 0
    current_count = await h.redis.get(current_slot_key)
    assert int(current_count) == 1 if current_day_condition == "counter" else current_count is None


class _ProfileLeaseRedis:
    """Small owner-token-aware Redis double for lease concurrency tests."""

    def __init__(self):
        self.value = None

    async def set(self, key, value, nx=False, ex=None):
        if nx and self.value is not None:
            return None
        self.value = value
        return True

    async def get(self, key):
        return self.value

    async def eval(self, script, numkeys, key, token, *args):
        if self.value != token:
            return 0
        if "EXPIRE" in script:
            return 1
        self.value = None
        return 1


async def test_later_persona_budget_exhaustion_is_a_visible_profile_decision(h, monkeypatch):
    from types import SimpleNamespace

    from cryptonite_agents.gateway import BudgetExceeded, Gateway
    from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
    from cryptonite_agents.gateway.routes import FLASH, FLASH_LITE, PRO
    from test_debate_gate import Models, load, scanner_payload
    from test_debate_perp_gate import CapRedis

    m = await profile(h)
    main = load("profile_budget_worker_main", "main.py")

    async def exhausted(*args, **kwargs):
        raise BudgetExceeded("debate.lite", "DAILY_BUDGET_EXHAUSTED")

    monkeypatch.setattr(main, "DEBATE_LITE", SimpleNamespace(name="DebateLite", role="debate.lite", run=exhausted))
    model = Models().model()
    with override_model(FLASH, model), override_model(PRO, model), override_model(FLASH_LITE, model):
        await main.process_debate_trigger(h.pool, CapRedis(), scanner_payload(), Gateway(pool=h.pool))
    row = await h.pool.fetchrow(
        "SELECT id,status,failure_reason FROM debate_runs WHERE instrument_id=$1 ORDER BY created_at DESC LIMIT 1", SPOT
    )
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "AI_BUDGET_EXHAUSTED"
    decision = await h.pool.fetchrow(
        "SELECT stage,outcome,reason,run_id FROM ai_profile_decisions WHERE mandate_id=$1 ORDER BY id DESC LIMIT 1", m.id
    )
    assert (decision["stage"], decision["outcome"], decision["reason"], decision["run_id"]) == (
        "GATE", "REFUSED", "AI_BUDGET_EXHAUSTED", row["id"]
    )
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_profile_lease_failure_is_visible_without_claiming_budget_exhaustion(h, monkeypatch):
    from types import SimpleNamespace

    from cryptonite_agents.gateway import Gateway
    from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
    from cryptonite_agents.gateway.routes import FLASH, FLASH_LITE, PRO
    from test_debate_gate import Models, load, scanner_payload
    from test_debate_perp_gate import CapRedis

    m = await profile(h)
    main = load("profile_lease_worker_main", "main.py")

    async def lease_lost(*args, **kwargs):
        raise gate.ProfileBudgetLockError("PROFILE_BUDGET_LOCK_UNAVAILABLE")

    monkeypatch.setattr(main, "DEBATE_LITE", SimpleNamespace(name="DebateLite", role="debate.lite", run=lease_lost))
    model = Models().model()
    with override_model(FLASH, model), override_model(PRO, model), override_model(FLASH_LITE, model):
        await main.process_debate_trigger(h.pool, CapRedis(), scanner_payload(), Gateway(pool=h.pool))
    row = await h.pool.fetchrow(
        "SELECT id,status,failure_reason FROM debate_runs WHERE instrument_id=$1 ORDER BY created_at DESC LIMIT 1", SPOT
    )
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "PROFILE_BUDGET_LOCK_UNAVAILABLE"
    decision = await h.pool.fetchrow(
        "SELECT stage,outcome,reason,run_id FROM ai_profile_decisions WHERE mandate_id=$1 ORDER BY id DESC LIMIT 1", m.id
    )
    assert (decision["stage"], decision["outcome"], decision["reason"], decision["run_id"]) == (
        "GATE", "REFUSED", "PROFILE_BUDGET_LOCK_UNAVAILABLE", row["id"]
    )
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


@pytest.mark.parametrize("market", ["SPOT", "PERP"])
@pytest.mark.parametrize("status", ["PAUSED", "STOPPING", "HALTED"])
async def test_reductions_bypass_profile_entry_refusals_and_disabled_switch(h, market, status):
    m = await profile(h, market=market, level=3, status=status, dry_run=True)
    instrument = PERP if market == "PERP" else SPOT
    await h.pool.execute(
        """INSERT INTO paper_positions(position_id,account_id,instance_id,instrument_id,side,entry_price,
           current_price,quantity,status) VALUES($1,'PAPER-ACCOUNT-001',$2,$3,'LONG',60000,60000,.01,'OPEN')""",
        uuid.uuid4().hex, m.instance_id, instrument)
    async with h.pool.acquire() as conn:
        routed, _ = await routing.route_entry(conn, uuid.uuid4(), SPOT, "SELL", .1, 5, mandate_id=str(m.id))
    assert routed == "COMPLETED"
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 1
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0


async def test_no_market_defence_refuses_without_side_effects(h, monkeypatch):
    m = await profile(h)
    # A malformed in-memory profile cannot acquire a market, even if a future caller bypasses validation.
    async def malformed(*args, **kwargs):
        return m.model_copy(update={"markets": frozenset()})
    monkeypatch.setattr(routing, "resolve_profile", malformed)
    (status, _), run = await route(h, m.id)
    assert status == "NO_TRADE"
    assert await h.pool.fetchval("SELECT reason FROM ai_profile_decisions WHERE stage='ROUTE' AND run_id=$1", run) == "NO_MARKET"
    assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
    assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_profile_ai_budget_share_counts_only_its_traced_spend(h):
    m = await profile(h, max_debates_per_day=2)
    await profile(h, lock=False, max_debates_per_day=6)
    await h.pool.execute("INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','8')")
    trace = uuid.uuid4()
    await h.pool.execute("INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) VALUES($1,'debate.lite','test','OK',$2)",
                         trace, str(m.id))
    await h.pool.execute(
        """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,total_cost_usd,trace_id)
           VALUES($1,'debate.lite','test',1,1,2,$2)""", str(trace), trace)
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, None, "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (False, "AI_BUDGET_EXHAUSTED")
    assert await h.pool.fetchval("SELECT SUM(total_cost_usd) FROM llm_cost_ledger") == 2  # global spend remains below $8


async def test_billed_gateway_failure_counts_toward_profile_share(h, monkeypatch):
    """A provider-billed failure is trace-attributed to its mandate and blocks that profile only."""
    import importlib

    from cryptonite_agents.gateway import (
        BudgetGuard,
        CallContext,
        Gateway,
        ModelUnavailable,
        RoleRoute,
    )

    m = await profile(h, max_debates_per_day=1)
    await profile(h, lock=False, max_debates_per_day=9)
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','100') "
        "ON CONFLICT(key) DO UPDATE SET value='100'"
    )
    gateway_module = importlib.import_module("cryptonite_agents.gateway.gateway")

    async def billed_failure(*args, **kwargs):
        raise gateway_module.backend.BackendError(
            "mock provider failure",
            gateway_module.backend.Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        )

    monkeypatch.setattr(gateway_module.backend, "run_model", billed_failure)
    route = RoleRoute("debate.triage", "google:gemini-2.5-pro")
    budget = BudgetGuard(pool=h.pool, default_cap_usd=100)
    gateway = Gateway(pool=h.pool, routes={route.role: route}, budget=budget)
    context = CallContext(role=route.role, mandate_id=str(m.id))

    with pytest.raises(ModelUnavailable):
        await gateway.run(route.role, "mock prompt", context=context)

    trace = await h.pool.fetchrow(
        "SELECT id,mandate_id,status,cost_usd FROM ai_decision_traces WHERE role=$1", route.role
    )
    assert trace["mandate_id"] == str(m.id)
    assert trace["status"] == "ERROR"
    assert trace["cost_usd"] > 10
    spent = await h.pool.fetchrow(
        """SELECT SUM(l.total_cost_usd) AS global_spent,
                  SUM(l.total_cost_usd) FILTER (WHERE t.mandate_id=$1) AS profile_spent
           FROM llm_cost_ledger l JOIN ai_decision_traces t ON t.id=l.trace_id""",
        str(m.id),
    )
    assert spent["global_spent"] == trace["cost_usd"]
    assert spent["profile_spent"] == trace["cost_usd"]
    async with h.pool.acquire() as conn:
        assert await gate.profile_budget_exhausted(conn, m, datetime.datetime.now(datetime.UTC).timestamp())
    decision = await gate.pre_debate_gate(
        h.pool, h.redis,
        {"instrument_id": SPOT, "mandate_id": str(m.id), "candidate_payload": json.dumps({"timeframe": "1h"})},
        None, "a brief", now_s=datetime.datetime.now(datetime.UTC).timestamp(),
    )
    assert (decision.allowed, decision.reason) == (False, "AI_BUDGET_EXHAUSTED")
    # Attribution changes only the profile filter; global spend remains the single billed ledger amount.
    assert await h.pool.fetchval("SELECT SUM(total_cost_usd) FROM llm_cost_ledger") == trace["cost_usd"]


async def test_profile_daily_debate_cap_zero_refuses_without_spending(h):
    m = await profile(h, max_debates_per_day=0)
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, None, "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (False, "PROFILE_DEBATE_DAILY_CAP")
    assert await h.redis.get(gate.profile_cap_key(m.id, NOW.timestamp())) is None


async def test_worker_preserves_profile_context_through_gate_debate_and_route(h):
    from cryptonite_agents.gateway import Gateway
    from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
    from cryptonite_agents.gateway.routes import FLASH, FLASH_LITE, PRO
    from test_debate_gate import Models, load, scanner_payload
    from test_debate_perp_gate import CapRedis
    m = await profile(h)
    main = load("profile_debate_worker_main", "main.py")
    model = Models().model()
    with override_model(FLASH, model), override_model(PRO, model), override_model(FLASH_LITE, model):
        await main.process_debate_trigger(h.pool, CapRedis(), scanner_payload(), Gateway(pool=h.pool))
    assert await h.pool.fetchval("SELECT mandate_id FROM debate_runs") == m.id
    run_id = await h.pool.fetchval("SELECT id FROM debate_runs WHERE mandate_id=$1", m.id)
    rows = await h.pool.fetch("SELECT stage,run_id FROM ai_profile_decisions WHERE mandate_id=$1 ORDER BY id", m.id)
    assert [r["stage"] for r in rows] == ["GATE", "DEBATE", "ROUTE"]
    assert {r["run_id"] for r in rows} == {run_id}
    traces = await h.pool.fetch("SELECT mandate_id FROM ai_decision_traces")
    assert len(traces) == 2
    assert {r["mandate_id"] for r in traces} == {str(m.id)}


async def test_gateway_budget_refusal_is_not_reported_as_triage_outage(h):
    from cryptonite_agents.gateway import BudgetGuard, Gateway
    await profile(h)
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h"})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, Gateway(budget=BudgetGuard(default_cap_usd=0)),
                                           "a brief", now_s=NOW.timestamp())
    assert (decision.allowed, decision.reason) == (False, "AI_BUDGET_EXHAUSTED")
    assert not await h.redis.exists(f"debate:cooldown:{SPOT}:1h")


async def test_exit_debate_records_its_profile_decisions(h):
    m = await profile(h, level=3, status="PAUSED")
    await h.pool.execute(
        """INSERT INTO paper_positions(position_id,account_id,instance_id,instrument_id,side,entry_price,
           current_price,quantity,status) VALUES($1,'PAPER-ACCOUNT-001',$2,$3,'LONG',60000,60000,.01,'OPEN')""",
        uuid.uuid4().hex, m.instance_id, SPOT)
    run = uuid.uuid4()
    await h.pool.execute("INSERT INTO debate_runs(id,instrument_id,status,trigger_source) VALUES($1,$2,'RUNNING','exit_monitor')", run, SPOT)
    async with h.pool.acquire() as conn:
        status, _ = await routing.route_exit(conn, run, SPOT, "CLOSE")
    assert status == "COMPLETED"
    rows = await h.pool.fetch("SELECT stage,outcome FROM ai_profile_decisions WHERE run_id=$1 ORDER BY id", run)
    assert [(r["stage"], r["outcome"]) for r in rows] == [("DEBATE", "CLOSE"), ("ROUTE", "ENQUEUED")]


@pytest.mark.parametrize("trend,expected", [("down", None), ("up", "TRIAGE_RULE_TIMEFRAMES_AGAINST")])
def test_short_candidate_triage_checks_trends_against_its_own_structure(trend, expected):
    data = {"candidate": {"entry": 100, "stop": 105, "target": 90, "reward_to_risk": 2, "timeframe": "1h"},
            "timeframes": [{"timeframe": "4h", "trend": trend}, {"timeframe": "1h", "trend": trend}]}
    assert gate._rule_rejection(data) == expected


def test_short_structure_does_not_bypass_reward_or_chop_rules():
    data = {"candidate": {"entry": 100, "stop": 105, "target": 95, "reward_to_risk": 1, "timeframe": "1h"},
            "timeframes": [{"timeframe": "4h", "trend": "down"},
                           {"timeframe": "1h", "trend": "down", "volume_ratio": .5, "bands": "inside"}]}
    assert gate._rule_rejection(data) == "TRIAGE_RULE_REWARD_TO_RISK"
    data["candidate"]["reward_to_risk"] = 2
    assert gate._rule_rejection(data) == "TRIAGE_RULE_CHOP"


def test_malformed_structure_cannot_suppress_long_triage_refusals():
    data = {"candidate": {"entry": "invalid", "stop": 105, "target": 90, "reward_to_risk": 1},
            "timeframes": [{"timeframe": "4h", "trend": "down"}, {"timeframe": "1h", "trend": "down"}]}
    assert gate._rule_rejection(data) == "TRIAGE_RULE_REWARD_TO_RISK"


@pytest.mark.parametrize(
    "case",
    [
        "daily_cap",
        "budget_share",
        "budget_share_accept",
        "style_timeframe",
        "direction",
        "mode_ask",
        "mode_veto",
        "mode_auto",
        "master_switch_off",
        "dry_run",
        "max_open_positions",
    ],
)
async def test_tagged_and_coin_locked_global_candidates_have_identical_profile_contract(
    h, monkeypatch, case
):
    """P1.2's global-candidate exception must behave exactly like a profile-tagged candidate.

    The triage backend is stubbed at the role boundary, so this exercises the real profile resolution,
    gate, decision-log and routing code without making a provider call. Each candidate pair uses the
    same active profile and coin: one carries the published ``mandate_id`` and the other resolves it
    solely through ``ai_profile_coin_locks``.
    """
    from types import SimpleNamespace

    level = {"mode_ask": 1, "mode_veto": 2, "mode_auto": 3, "master_switch_off": 3}.get(case, 3)
    changes = {"max_debates_per_day": 1}
    if case == "direction":
        changes["direction"] = "SHORT"
    if case == "max_open_positions":
        changes["max_open_positions"] = 1
    if case == "dry_run":
        changes["dry_run"] = True
    if case == "mode_veto":
        changes["active_hours_utc"] = "00:00-00:00"
    m = await profile(h, market="PERP" if case == "direction" else "SPOT", level=level, **changes)
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('autotrade.enabled',$1) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        "0" if case == "master_switch_off" else "1",
    )
    await h.pool.execute(
        "INSERT INTO system_settings(key,value) VALUES('LLM_DAILY_BUDGET_USD','100') "
        "ON CONFLICT(key) DO UPDATE SET value='100'"
    )

    timeframe = "15m" if case == "style_timeframe" else "1h"
    if case == "daily_cap":
        await h.redis.set(gate.profile_cap_key(m.id, NOW.timestamp()), 1, ex=gate.PROFILE_CAP_TTL_S)
    elif case in {"budget_share", "budget_share_accept"}:
        # The profile receives its proportional $10 share; prior traced spend exhausts that share.
        await profile(h, lock=False, max_debates_per_day=9)
        if case == "budget_share":
            await h.pool.execute(
                "UPDATE system_settings SET value='100' WHERE key='LLM_DAILY_BUDGET_USD'"
            )
            trace_id = uuid.uuid4()
            await h.pool.execute(
                "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id) "
                "VALUES($1,'debate.triage','stub','OK',$2)", trace_id, str(m.id)
            )
            await h.pool.execute(
                """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,
                   total_cost_usd,trace_id,created_at) VALUES($1,'debate.triage','stub',1,1,10,$2,$3)""",
                str(trace_id), trace_id, NOW,
            )
    elif case == "max_open_positions":
        await h.pool.execute(
            """INSERT INTO paper_positions(position_id,account_id,instance_id,instrument_id,side,entry_price,
               current_price,quantity,status) VALUES($1,'PAPER-ACCOUNT-001',$2,$3,'LONG',60000,60000,.01,'OPEN')""",
            uuid.uuid4().hex, m.instance_id, SPOT,
        )

    monkeypatch.setattr(gate, "_utc_now_s", lambda: NOW.timestamp())

    async def mock_triage(*args, context=None, **kwargs):
        """Record deterministic, profile-attributed mock spend inside the real share guard."""
        trace_id = uuid.uuid4()
        await h.pool.execute(
            "INSERT INTO ai_decision_traces(id,role,model,status,mandate_id,created_at) "
            "VALUES($1,'debate.triage','stub','OK',$2,$3)",
            trace_id, context.mandate_id, NOW,
        )
        await h.pool.execute(
            """INSERT INTO llm_cost_ledger(request_id,agent_name,model_name,input_tokens,output_tokens,
               total_cost_usd,trace_id,created_at) VALUES($1,'debate.triage','stub',1,1,1,$2,$3)""",
            str(trace_id), trace_id, NOW,
        )
        return SimpleNamespace(output=SimpleNamespace(worth_debate=True), trace_id=trace_id)

    monkeypatch.setattr(
        gate,
        "TRIAGE",
        SimpleNamespace(role="debate.triage", run=mock_triage),
    )

    candidate_id = str(uuid.uuid4())

    async def run_candidate(tagged: bool):
        run_id = uuid.uuid4()
        payload = {
            "instrument_id": SPOT,
            "trigger_source": "scanner",
            "candidate_payload": json.dumps({"timeframe": timeframe, "candidate_id": candidate_id}),
        }
        if tagged:
            payload["mandate_id"] = str(m.id)
        decision = await gate.pre_debate_gate(
            h.pool, h.redis, payload, object(), "stub brief", now_s=NOW.timestamp(), run_id=run_id
        )
        slot_count = int(await h.redis.get(gate.profile_cap_key(m.id, NOW.timestamp())) or 0)
        route_result = None
        if decision.allowed:
            async with h.pool.acquire() as conn:
                status, routed_profile = await routing.route_entry(
                    conn, run_id, SPOT, "BUY", .8, 3, mandate_id=payload.get("mandate_id"),
                    now=NOW, stop_loss=57000, take_profit=66000, reference_price=60000,
                    redis=h.redis, grade_context=BULL,
                )
            route_result = (status, str(routed_profile.id) if routed_profile else None)
        rows = await h.pool.fetch(
            "SELECT mandate_id,stage,outcome,reason FROM ai_profile_decisions WHERE run_id=$1 ORDER BY id", run_id
        )
        # Each stage row is attached to the resolved profile even when the source payload omitted its id.
        assert all(row["mandate_id"] == m.id for row in rows)
        return (decision.allowed, decision.reason, route_result, slot_count), [
            (row["stage"], row["outcome"], row["reason"]) for row in rows
        ], run_id

    tagged = await run_candidate(True)
    await h.redis.delete(f"debate:cooldown:{SPOT}:{timeframe}")
    if case not in {"daily_cap", "budget_share"}:
        await h.redis.delete(gate.profile_cap_key(m.id, NOW.timestamp()))
    untagged = await run_candidate(False)
    assert tagged[:2] == untagged[:2]

    expected_gate = {
        "daily_cap": "PROFILE_DEBATE_DAILY_CAP",
        "budget_share": "AI_BUDGET_EXHAUSTED",
        "style_timeframe": "TIMEFRAME_NOT_IN_STYLE",
    }.get(case, "TRIAGE_ACCEPTED")
    assert tagged[0][1] == expected_gate
    if expected_gate != "TRIAGE_ACCEPTED":
        assert tagged[1] == [("GATE", "REFUSED", expected_gate)]
        assert tagged[0][2] is None
        assert tagged[0][3] == (1 if case == "daily_cap" else 0)
        assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        return

    expected_route = {
        "direction": ("NO_TRADE", "NO_TRADE", "DIRECTION_NOT_ALLOWED"),
        "max_open_positions": ("NO_TRADE", "NO_TRADE", "MAX_OPEN_POSITIONS"),
        "mode_ask": ("PENDING_APPROVAL", "APPROVAL_CREATED", None),
        "mode_veto": ("PENDING_APPROVAL", "VETO_WINDOW", None),
        "mode_auto": ("COMPLETED", "ENQUEUED", None),
        "budget_share_accept": ("COMPLETED", "ENQUEUED", None),
        "master_switch_off": ("PENDING_APPROVAL", "APPROVAL_CREATED", None),
        "dry_run": ("COMPLETED", "DRY_RUN_ROUTED", None),
    }[case]
    assert tagged[0][2][0] == expected_route[0]
    assert tagged[0][2][1] == str(m.id)
    assert tagged[0][3] == 1
    assert tagged[1] == [
        ("GATE", "ACCEPTED", "TRIAGE_ACCEPTED"),
        ("DEBATE", "BUY", None),
        ("ROUTE", expected_route[1], expected_route[2]),
    ]
    if case == "direction" or case == "max_open_positions":
        assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    else:
        assert await h.pool.fetchval("SELECT count(*) FROM ai_approvals") == (
            0 if expected_route[0] == "COMPLETED" else 2
        )
    # Assert persisted ownership and approval policy independently for both routes.
    for _, _, _, _, run_id in (tagged[0] + (tagged[2],), untagged[0] + (untagged[2],)):
        approvals = await h.pool.fetch(
            "SELECT mandate_id, action_class, command_type, timeout_policy FROM ai_approvals WHERE source_ref=$1",
            str(run_id),
        )
        commands = await h.pool.fetch(
            "SELECT mandate_id, command_type, idempotency_key, payload FROM execution_commands "
            "WHERE idempotency_key=$1",
            f"debate:{run_id}",
        )
        if expected_route[0] == "NO_TRADE":
            assert not approvals
            assert not commands
        elif expected_route[0] == "PENDING_APPROVAL":
            assert len(approvals) == 1
            assert approvals[0]["mandate_id"] == m.id
            assert approvals[0]["action_class"] == "ADD_RISK"
            assert approvals[0]["command_type"] == "SUBMIT_ORDER"
            assert approvals[0]["timeout_policy"] == ("PROCEED" if case == "mode_veto" else "DENY")
            assert not commands
        else:
            assert not approvals
            assert len(commands) == 1
            assert commands[0]["mandate_id"] == m.id
            assert commands[0]["command_type"] == "SUBMIT_ORDER"
            assert commands[0]["idempotency_key"] == f"debate:{run_id}"
            assert json.loads(commands[0]["payload"])["mandate_id"] == str(m.id)
    if case not in {"direction", "max_open_positions"}:
        # The profile slot is independently reset between runs; each accepted candidate consumes one.
        for _, _, _, slot_count, _ in (tagged[0] + (tagged[2],), untagged[0] + (untagged[2],)):
            assert slot_count == 1
    if case == "budget_share_accept":
        attributed_spend = await h.pool.fetchval(
            """SELECT COALESCE(SUM(l.total_cost_usd),0) FROM llm_cost_ledger l
               JOIN ai_decision_traces t ON t.id=l.trace_id WHERE t.mandate_id=$1""",
            str(m.id),
        )
        assert attributed_spend == Decimal("2")


async def _audit_worker(h, monkeypatch):
    """Exercise durable worker boundaries with deterministic data and no model calls."""
    from types import SimpleNamespace

    from test_debate_gate import load
    main = load("profile_audit_worker", "main.py")

    async def nothing(*args):
        return None

    async def brief(*args):
        return "audit brief"

    class Context:
        def __init__(self, pool):
            pass

        async def get_instrument_context(self, instrument):
            return {}

    async def no_model(*args, **kwargs):
        raise RuntimeError("audit stops after durable run")

    monkeypatch.setattr(main, "read_live_quote", nothing)
    monkeypatch.setattr(main, "resolve_quote_price", lambda *args: SimpleNamespace(
        price=60000, source="audit", age_s=0, quote_ts_ns=1))
    monkeypatch.setattr(main, "load_brief", brief)
    monkeypatch.setattr(main, "load_brief_data", nothing)
    monkeypatch.setattr(main, "AntiCorruptionLayer", Context)
    monkeypatch.setattr(main, "DEBATE_LITE", SimpleNamespace(name="audit", role="debate.lite", run=no_model))
    return main


@pytest.mark.parametrize("refused", [False, True])
async def test_candidate_delivery_records_one_atomic_gate_and_run(h, monkeypatch, refused):
    """Concurrent distinct messages persist exactly one candidate outcome and audit."""
    m = await profile(h)
    main = await _audit_worker(h, monkeypatch)
    await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=false")
    if refused:
        await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=true")
    async def synchronized_gate(pool, redis, payload, gateway, brief, **kwargs):
        return await gate.pre_debate_gate(pool, redis, payload, None, brief, **kwargs)

    monkeypatch.setattr(main, "pre_debate_gate", synchronized_gate)
    cid = uuid.uuid4()
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h", "candidate_id": str(cid)})}
    await asyncio.gather(*(main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref=f"audit/{i}-0")
                           for i in (1, 2)))
    await main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="audit/3-0")
    rows = await h.pool.fetch("SELECT outcome,run_id FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='GATE'", m.id)
    runs = await h.pool.fetch("SELECT id,status FROM debate_runs WHERE candidate_id=$1", cid)
    assert len(rows) == len(runs) == 1
    assert rows[0]["outcome"] == ("REFUSED" if refused else "ACCEPTED")
    assert rows[0]["run_id"] == runs[0]["id"]
    if refused:
        assert runs[0]["status"] == "GATE_REFUSED"
    else:
        assert int(await h.redis.get(gate.profile_cap_key(m.id, gate._utc_now_s()))) == 1


async def test_candidate_retry_after_slot_reservation_preserves_slot_and_audit(h, monkeypatch):
    """Failure before a durable run leaves the candidate able to reuse its last daily slot."""
    m = await profile(h, max_debates_per_day=1)
    main = await _audit_worker(h, monkeypatch)
    await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=false")

    async def no_triage(pool, redis, payload, gateway, brief, **kwargs):
        return await gate.pre_debate_gate(pool, redis, payload, None, brief, **kwargs)

    monkeypatch.setattr(main, "pre_debate_gate", no_triage)
    context_class = main.AntiCorruptionLayer

    class Crash(context_class):
        async def get_instrument_context(self, instrument):
            raise RuntimeError("crash after reservation")

    monkeypatch.setattr(main, "AntiCorruptionLayer", Crash)
    cid = uuid.uuid4()
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h", "candidate_id": str(cid)})}
    with pytest.raises(RuntimeError, match="crash after reservation"):
        await main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="audit/1-0")
    assert await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE stage='GATE'") == 0
    monkeypatch.setattr(main, "AntiCorruptionLayer", context_class)
    await main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="audit/2-0")
    assert await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid) == 1
    assert await h.pool.fetchval("SELECT outcome FROM ai_profile_decisions WHERE stage='GATE'") == "ACCEPTED"
    assert int(await h.redis.get(gate.profile_cap_key(m.id, gate._utc_now_s()))) == 1


@pytest.mark.parametrize("paid_triage", [False, True])
@pytest.mark.parametrize("crash_owner", [False, True])
async def test_candidate_lease_serializes_mixed_outcomes_and_paid_triage(h, monkeypatch, paid_triage, crash_owner):
    """A duplicate waits for the accepted owner; it cannot refuse or bill triage ahead of it."""
    from types import SimpleNamespace

    m = await profile(h, max_debates_per_day=1)
    main = await _audit_worker(h, monkeypatch)
    await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=false")
    calls = 0
    evaluations = 0
    context_entered = asyncio.Event()
    release_context = asyncio.Event()

    async def triage(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(output=SimpleNamespace(worth_debate=True), trace_id=uuid.uuid4())

    monkeypatch.setattr(gate, "TRIAGE", SimpleNamespace(role="debate.triage", run=triage))

    async def evaluate(pool, redis, payload, gateway, brief, **kwargs):
        nonlocal evaluations
        evaluations += 1
        if not paid_triage and payload.get("audit_duplicate"):
            brief = None
        return await gate.pre_debate_gate(pool, redis, payload, gateway if paid_triage else None, brief, **kwargs)

    context_calls = 0

    class DelayedContext(main.AntiCorruptionLayer):
        async def get_instrument_context(self, instrument):
            nonlocal context_calls
            context_calls += 1
            first_context = context_calls == 1
            context_entered.set()
            await release_context.wait()
            if crash_owner and first_context:
                raise RuntimeError("owner crashed before commit")
            return {}

    monkeypatch.setattr(main, "pre_debate_gate", evaluate)
    monkeypatch.setattr(main, "AntiCorruptionLayer", DelayedContext)
    cid = uuid.uuid4()
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h", "candidate_id": str(cid)})}
    first = asyncio.create_task(main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="lease/1-0"))
    await asyncio.wait_for(context_entered.wait(), 5)
    second = asyncio.create_task(main.process_debate_trigger(
        h.pool, h.redis, {**payload, "audit_duplicate": True}, object(), message_ref="lease/2-0"))
    try:
        await asyncio.sleep(.15)
        pending_before_commit = not second.done()
        early_evaluations = evaluations
        early_calls = calls
        early_runs = await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid)
    finally:
        release_context.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
    if crash_owner:
        assert isinstance(results[0], RuntimeError)
        assert str(results[0]) == "owner crashed before commit"
    else:
        assert results[0] is None
    assert results[1] is None
    assert pending_before_commit
    assert early_evaluations == 1
    assert early_calls == (1 if paid_triage else 0)
    assert early_runs == 0
    assert evaluations == (2 if crash_owner else 1)
    assert calls == ((2 if crash_owner else 1) if paid_triage else 0)
    assert await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid) == 1
    expected_outcome = "REFUSED" if crash_owner and not paid_triage else "ACCEPTED"
    assert await h.pool.fetchval("SELECT outcome FROM ai_profile_decisions WHERE stage='GATE'") == expected_outcome
    assert int(await h.redis.get(gate.profile_cap_key(m.id, gate._utc_now_s()))) == 1


async def test_candidate_crashed_owner_expiry_retries_reserved_slot_and_triage(h, monkeypatch):
    """A dead owner's lease expires; its uncommitted last slot is reusable by a pending delivery."""
    from types import SimpleNamespace

    m = await profile(h, max_debates_per_day=1)
    main = await _audit_worker(h, monkeypatch)
    await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=false")
    cid = uuid.uuid4()
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h", "candidate_id": str(cid)})}
    decision = await gate.pre_debate_gate(h.pool, h.redis, payload, None, "brief", defer_record=True)
    assert decision.allowed
    lease_key = f"debate:candidate_gate:{cid}"
    await h.redis.set(lease_key, "crashed-owner", px=250)
    calls = 0

    async def triage(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(output=SimpleNamespace(worth_debate=True), trace_id=uuid.uuid4())

    monkeypatch.setattr(gate, "TRIAGE", SimpleNamespace(role="debate.triage", run=triage))
    retry = asyncio.create_task(main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="lease/3-0"))
    try:
        await asyncio.sleep(.05)
        pending_before_expiry = not retry.done()
        calls_before_expiry = calls
    finally:
        await asyncio.wait_for(retry, 5)
    assert pending_before_expiry
    assert calls_before_expiry == 0
    assert calls == 1
    assert await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid) == 1
    assert await h.pool.fetchval("SELECT outcome FROM ai_profile_decisions WHERE stage='GATE'") == "ACCEPTED"
    assert int(await h.redis.get(gate.profile_cap_key(m.id, gate._utc_now_s()))) == 1
    assert await h.redis.get(lease_key) is None


async def test_candidate_lease_renewal_and_lost_owner_cannot_commit_or_release_successor(h, monkeypatch):
    """A slow owner renews; a token takeover fences its outcome and survives stale release."""
    await profile(h)
    main = await _audit_worker(h, monkeypatch)
    await h.pool.execute("UPDATE paper_accounts SET daily_loss_latched=false")
    monkeypatch.setattr(gate, "CANDIDATE_GATE_LEASE_TTL_S", 1, raising=False)
    monkeypatch.setattr(gate, "CANDIDATE_GATE_LEASE_RENEW_S", .05, raising=False)
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def no_triage(pool, redis, payload, gateway, brief, **kwargs):
        return await gate.pre_debate_gate(pool, redis, payload, None, brief, **kwargs)

    class DelayedContext(main.AntiCorruptionLayer):
        async def get_instrument_context(self, instrument):
            entered.set()
            await resume.wait()
            return {}

    monkeypatch.setattr(main, "pre_debate_gate", no_triage)
    monkeypatch.setattr(main, "AntiCorruptionLayer", DelayedContext)
    cid = uuid.uuid4()
    key = f"debate:candidate_gate:{cid}"
    payload = {"instrument_id": SPOT, "candidate_payload": json.dumps({"timeframe": "1h", "candidate_id": str(cid)})}
    owner = asyncio.create_task(main.process_debate_trigger(h.pool, h.redis, payload, object(), message_ref="lease/4-0"))
    await asyncio.wait_for(entered.wait(), 5)
    token = await h.redis.get(key)
    try:
        await asyncio.sleep(1.15)  # Longer than the original lease: its live token must be renewed.
        renewed_token = await h.redis.get(key)
        await h.redis.set(key, "successor", ex=10)
    finally:
        resume.set()
        results = await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)
    assert token and renewed_token == token
    assert isinstance(results[0], gate.CandidateGateLeaseError)
    assert await h.redis.get(key) == "successor"
    assert await h.pool.fetchval("SELECT count(*) FROM debate_runs WHERE candidate_id=$1", cid) == 0
    assert await h.pool.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE stage='GATE'") == 0
