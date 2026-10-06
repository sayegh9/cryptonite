"""Debate queue recovery and run reaper (review-plan B4, F-H6): a killed worker's message is reclaimed,
a poison message goes to the DLQ after 3 deliveries, a redelivery never opens a second run, stale RUNNING
runs are reaped, idle consumers are deleted, and the one-off recovery script. Claims fence on the durable
claim generation proved against the live PEL owner under the row lock, so a stale attempt of the same
message_ref (paused across a takeover, failed late, or claiming late) writes nothing. Redis: the isolated
test DB (connections.REDIS_URL, DB 15) with per-test stream names. Postgres: a throwaway DB at Alembic
head. Models are FunctionModels; the worker's Redis in debate runs is an AsyncMock (nothing is published)."""

import asyncio
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
import redis.asyncio as redis
from connections import REDIS_URL
from cryptonite_agents.gateway import Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH, PRO
from migrated_db import migrated_database
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "services" / "debate_worker"
sys.path.insert(0, str(WORKER_DIR))
import debate_queue as dq  # noqa: E402

INSTRUMENT = "BTCUSDT.BINANCE"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def debate_main():
    return load("debate_worker_main_b4", WORKER_DIR / "main.py")


@pytest.fixture(scope="module")
def recover_script():
    return load("recover_debate_queue_b4", ROOT / "scripts" / "recover_debate_queue.py")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_b4") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    yield pool
    await pool.execute("TRUNCATE debate_turns, portfolio_decisions, exit_decisions, debate_runs CASCADE")
    await pool.close()


@pytest.fixture
async def r(monkeypatch, recover_script):
    """Test-DB Redis with this test's own stream, DLQ and lock names (never the live queue)."""
    prefix = f"test:b4:{uuid.uuid4().hex[:8]}"
    streams = (f"{prefix}:debate_queue", f"{prefix}:exit_debate_queue")
    for module in (dq, recover_script):
        monkeypatch.setattr(module, "STREAMS", streams)
    monkeypatch.setattr(dq, "DLQ_STREAM", f"{prefix}:dlq")
    monkeypatch.setattr(dq, "HOUSEKEEPING_LOCK", f"{prefix}:lock")
    client = redis.from_url(REDIS_URL, decode_responses=True)
    await dq.ensure_groups(client)
    yield client
    await client.delete(*streams, dq.DLQ_STREAM, dq.HOUSEKEEPING_LOCK)
    await client.aclose()


class Recorder:
    def __init__(self, fail: Exception | None = None, delay: float = 0.0):
        self.calls: list[tuple[str, str, dict, int]] = []
        self.fail, self.delay = fail, delay

    async def __call__(self, stream, msg_id, fields, delivered):
        self.calls.append((stream, msg_id, fields, delivered))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail


async def pending_count(client, stream) -> int:
    return (await client.xpending(stream, dq.GROUP))["pending"]


async def read_without_handling(client, consumer: str):
    """A worker that takes a message and is killed before it acks."""
    return await client.xreadgroup(dq.GROUP, consumer, {dq.STREAMS[0]: ">"}, count=1)


def test_consumer_name_is_stable_per_container():
    assert dq.consumer_name({"HOSTNAME": "3f2a9c1b7d4e"}) == "debate-3f2a9c1b7d4e"
    assert dq.consumer_name({"HOSTNAME": "3f2a9c1b7d4e"}) == dq.consumer_name({"HOSTNAME": "3f2a9c1b7d4e"})
    assert dq.consumer_name({"HOSTNAME": "a", "DEBATE_WORKER_NAME": "debate-1"}) == "debate-1"


async def test_killed_workers_message_is_reclaimed_by_another_consumer(r):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT})
    await read_without_handling(r, "debate-dead")

    handler = Recorder()
    other = dq.DebateQueue(r, "debate-alive", handler, reclaim_idle_ms=60_000)
    assert await other.reclaim() == 0  # not idle long enough: left with its owner

    other.reclaim_idle_ms = 50
    await asyncio.sleep(0.1)
    assert await other.reclaim() == 1
    assert handler.calls == [(stream, msg_id, {"instrument_id": INSTRUMENT}, 2)]  # second delivery
    assert await pending_count(r, stream) == 0  # acked


async def test_restarted_worker_recovers_its_own_pending_entries(r):
    stream = dq.STREAMS[1]
    await r.xadd(stream, {"instrument_id": INSTRUMENT, "run_id": str(uuid.uuid4())})
    await r.xreadgroup(dq.GROUP, "debate-host1", {stream: ">"}, count=1)

    handler = Recorder()
    assert await dq.DebateQueue(r, "debate-host1", handler).recover_own() == 1  # same name after restart
    assert handler.calls[0][3] == 2 and await pending_count(r, stream) == 0


async def test_poison_message_goes_to_the_dlq_after_three_deliveries(r):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT, "trigger_source": "scanner"})
    handler = Recorder(fail=RuntimeError("model gateway down"))
    first = dq.DebateQueue(r, "debate-a", handler)
    second = dq.DebateQueue(r, "debate-b", handler, reclaim_idle_ms=20)

    await first.read(block_ms=100)  # delivery 1 fails: stays pending
    assert await pending_count(r, stream) == 1
    for _ in range(3):  # deliveries 2 and 3 fail; the 4th claim dead-letters without running it
        await asyncio.sleep(0.05)
        await second.reclaim()

    assert [c[3] for c in handler.calls] == [1, 2, 3]
    assert await pending_count(r, stream) == 0
    (dlq_id, entry), = await r.xrange(dq.DLQ_STREAM)
    assert entry["instrument_id"] == INSTRUMENT and entry["trigger_source"] == "scanner"  # payload kept for replay
    assert (entry["dlq_source_stream"], entry["dlq_source_id"]) == (stream, msg_id)
    assert (entry["dlq_reason"], entry["dlq_deliveries"]) == ("MAX_DELIVERIES", "4")


async def test_malformed_payload_is_dead_lettered_at_once(r):
    await r.xadd(dq.STREAMS[0], {"junk": "1"})
    handler = Recorder(fail=dq.PoisonMessage("no instrument_id"))
    await dq.DebateQueue(r, "debate-a", handler).read(block_ms=100)
    assert len(handler.calls) == 1 and await pending_count(r, dq.STREAMS[0]) == 0
    (_, entry), = await r.xrange(dq.DLQ_STREAM)
    assert entry["dlq_reason"] == "POISON: no instrument_id"


async def test_a_live_slow_worker_keeps_its_lease(r):
    await r.xadd(dq.STREAMS[0], {"instrument_id": INSTRUMENT})
    slow = dq.DebateQueue(r, "debate-slow", Recorder(delay=0.6), lease_refresh_s=0.05)
    work = asyncio.create_task(slow.read(block_ms=100))
    await asyncio.sleep(0.4)  # longer than the other consumer's idle window, but the lease is refreshed
    other = dq.DebateQueue(r, "debate-other", Recorder(), reclaim_idle_ms=200)
    assert await other.reclaim() == 0
    await work
    assert await pending_count(r, dq.STREAMS[0]) == 0


async def test_idle_consumers_without_pending_entries_are_deleted(r):
    stream = dq.STREAMS[0]
    await r.xgroup_createconsumer(stream, dq.GROUP, "debate-gone")
    await r.xadd(stream, {"instrument_id": INSTRUMENT})
    await read_without_handling(r, "debate-holds-work")
    await r.xgroup_createconsumer(stream, dq.GROUP, "debate-me")
    await asyncio.sleep(0.1)

    deleted = await dq.delete_idle_consumers(r, keep="debate-me", idle_ms=50)
    assert deleted == [f"{stream}/debate-gone"]
    names = {c["name"] for c in await r.xinfo_consumers(stream, dq.GROUP)}
    assert names == {"debate-holds-work", "debate-me"}  # pending work and self are kept


async def insert_run(pool, status: str, age_min: float, **extra) -> uuid.UUID:
    run_id = uuid.uuid4()
    await pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, stream_message_id, claim_gen)
           VALUES ($1, $2, $3, 'scanner', now() - make_interval(secs => $4), $5, $6)""",
        run_id, INSTRUMENT, status, age_min * 60, extra.get("stream_message_id"), extra.get("claim_gen", 0))
    return run_id


async def test_reaper_fails_runs_stuck_running_for_30_minutes(pool):
    stale = await insert_run(pool, "RUNNING", 31)
    fresh = await insert_run(pool, "RUNNING", 5)
    done = await insert_run(pool, "NO_TRADE", 120)

    assert await dq.reap_stale_runs(pool) == [stale]
    rows = {r["id"]: r for r in await pool.fetch("SELECT id, status, failure_reason, completed_at FROM debate_runs")}
    assert (rows[stale]["status"], rows[stale]["failure_reason"]) == ("FAILED", "REAPED_STALE")
    assert rows[stale]["completed_at"] is not None
    assert rows[fresh]["status"] == "RUNNING" and rows[done]["status"] == "NO_TRADE"


async def test_housekeeping_reaps_on_one_replica_per_tick(r, pool, monkeypatch):
    reaps = []

    async def counting_reap(p, max_age_s=dq.STALE_RUN_S):
        reaps.append(max_age_s)
        return []

    monkeypatch.setattr(dq, "reap_stale_runs", counting_reap)
    replicas = [dq.DebateQueue(r, f"debate-{i}", Recorder()) for i in range(3)]
    await asyncio.gather(*(dq.housekeeping(r, pool, q) for q in replicas))
    assert len(reaps) == 1


def debate_model() -> FunctionModel:
    verdicts = {
        "recommendation": {"recommendation": "BUY", "conviction_score": 0.72, "allocation_pct": 8.0, "reasoning": "trend"},
        "final_allocation_pct": {"final_allocation_pct": 6.0, "conviction_level": "Overweight", "risk_reasoning": "size"},
    }

    def fn(messages, info: AgentInfo):
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("argument")])
        tool = info.output_tools[0]
        key = next(k for k in verdicts if k in tool.parameters_json_schema.get("properties", {}))
        return ModelResponse(parts=[ToolCallPart(tool.name, verdicts[key])])

    return FunctionModel(fn)


class FakeEmbedder:
    async def embed_texts(self, texts):
        return [[0.0] * 768 for _ in texts], 1


def redis_double():
    double = AsyncMock()
    double.get.return_value = json.dumps({"last_price": 65000.0})
    return double


async def test_redelivered_message_does_not_open_a_second_run(debate_main, pool):
    ref = f"stream:debate_queue/{uuid.uuid4().int % 10**13}-0"
    payload = {"instrument_id": INSTRUMENT, "trigger_source": "test"}
    model = debate_model()
    with override_model(FLASH, model), override_model(PRO, model), override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(pool, redis_double(), payload, Gateway(), message_ref=ref)
        # The worker died between commit and ack: the same entry is delivered again
        await debate_main.process_debate_trigger(pool, redis_double(), payload, Gateway(), message_ref=ref)

    runs = await pool.fetch("SELECT id, status, stream_message_id FROM debate_runs")
    assert len(runs) == 1 and runs[0]["status"] == "PENDING_APPROVAL" and runs[0]["stream_message_id"] == ref
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 1


async def test_redelivery_after_a_mid_debate_crash_closes_the_orphan_run(debate_main, pool):
    ref = "stream:debate_queue/1700000000000-0"
    orphan = await insert_run(pool, "RUNNING", 12, stream_message_id=ref)
    # No model override and no API key: any model call would raise, so a re-debate would fail the test
    await debate_main.process_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "trigger_source": "test"},
                                             Gateway(), message_ref=ref)
    runs = await pool.fetch("SELECT id, status, failure_reason FROM debate_runs")
    assert [(r["id"], r["status"], r["failure_reason"]) for r in runs] == [(orphan, "FAILED", "WORKER_LOST")]


async def test_stream_message_id_is_unique(pool):
    await insert_run(pool, "NO_TRADE", 1, stream_message_id="stream:debate_queue/1-0")
    with pytest.raises(asyncpg.UniqueViolationError):
        await insert_run(pool, "RUNNING", 0, stream_message_id="stream:debate_queue/1-0")


async def test_redelivered_exit_debate_for_a_finished_run_is_skipped(debate_main, pool, monkeypatch):
    run_id = await insert_run(pool, "PENDING_APPROVAL", 3)
    context = AsyncMock(side_effect=AssertionError("must not re-run the exit debate"))
    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", context)
    await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                  Gateway())
    assert await pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "PENDING_APPROVAL"


async def test_recovery_script_dry_run_then_apply(r, pool, recover_script, capsys):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT, "trigger_source": "scanner"})
    await read_without_handling(r, "worker-1a2b3c4d")  # the pre-B4 random name that died with it
    await r.xgroup_createconsumer(stream, dq.GROUP, "worker-deadbeef")
    stuck = await insert_run(pool, "RUNNING", 45)
    await asyncio.sleep(0.1)
    limits = {"pel_idle_ms": 50, "consumer_idle_ms": 50}

    plan = await recover_script.build_plan(r, pool, **limits)
    recover_script.print_plan(plan)
    assert [(p["message_id"], p["consumer"]) for p in plan.pending] == [(msg_id, "worker-1a2b3c4d")]
    assert [run["id"] for run in plan.runs] == [stuck]
    # Both go: the holder's only pending entry is dead-lettered first
    assert sorted(c["name"] for c in plan.consumers) == ["worker-1a2b3c4d", "worker-deadbeef"]
    assert await pending_count(r, stream) == 1  # dry run changed nothing
    out = capsys.readouterr().out
    assert "Stuck PEL entries" in out and ": 1" in out

    rollback = await recover_script.apply_plan(r, pool, plan)
    assert await pending_count(r, stream) == 0
    (_, entry), = await r.xrange(dq.DLQ_STREAM)
    assert (entry["dlq_reason"], entry["dlq_source_id"]) == ("MANUAL_RECOVERY", msg_id)
    row = await pool.fetchrow("SELECT status, failure_reason FROM debate_runs WHERE id = $1", stuck)
    assert (row["status"], row["failure_reason"]) == ("FAILED", "REAPED_STALE")
    assert rollback[0] == f"XADD {stream} * instrument_id {INSTRUMENT} trigger_source scanner"
    assert rollback[1] == f"UPDATE debate_runs SET status = 'RUNNING', failure_reason = NULL, completed_at = NULL WHERE id IN ('{stuck}');"

    assert await r.xinfo_consumers(stream, dq.GROUP) == []
    again = await recover_script.build_plan(r, pool, **limits)  # idempotent: nothing left to do
    assert (again.pending, again.runs, again.consumers) == ([], [], [])


async def test_recover_own_does_not_steal_transferred_entry(r):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT})
    # Worker A initially reads the message
    await r.xreadgroup(dq.GROUP, "debate-w1", {stream: ">"}, count=1)
    # Another consumer reclaims it before worker A recovers
    await r.xclaim(stream, dq.GROUP, "debate-w2", 0, [msg_id])

    handler = Recorder()
    # Worker A restarts and tries to recover its own entries
    recovered = await dq.DebateQueue(r, "debate-w1", handler).recover_own()
    assert recovered == 0
    assert handler.calls == []
    # Entry still belongs to debate-w2
    rows = await r.xpending_range(stream, dq.GROUP, min=msg_id, max=msg_id, count=1)
    assert rows[0]["consumer"] == "debate-w2"


async def test_read_single_message_even_if_multiple_streams_have_work(r):
    # Both streams have pending unread entries
    await r.xadd(dq.STREAMS[0], {"instrument_id": INSTRUMENT, "n": "1"})
    await r.xadd(dq.STREAMS[1], {"instrument_id": INSTRUMENT, "n": "2"})

    handler = Recorder()
    queue = dq.DebateQueue(r, "debate-reader", handler)
    # Single read should process at most 1 message
    await queue.read(block_ms=100)
    assert len(handler.calls) == 1
    # Check that only one message was read into PEL
    p0 = await pending_count(r, dq.STREAMS[0])
    p1 = await pending_count(r, dq.STREAMS[1])
    # The processed one was acked, so total pending is 0
    assert p0 + p1 == 0
    # The other message is still unread in its stream:
    unread_stream = dq.STREAMS[1] if handler.calls[0][0] == dq.STREAMS[0] else dq.STREAMS[0]
    unread = await r.xreadgroup(dq.GROUP, "debate-reader2", {unread_stream: ">"}, count=1)
    assert len(unread[0][1]) == 1


async def test_lease_loss_cancels_handler_and_refuses_stale_ack_and_dlq(r):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT})

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_handler(st, mid, fields, deliv):
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    queue = dq.DebateQueue(r, "debate-victim", long_handler, lease_refresh_s=0.05)
    task = asyncio.create_task(queue.read(block_ms=100))
    await started.wait()

    # Thief steals the message via XCLAIM
    await r.xclaim(stream, dq.GROUP, "debate-thief", 0, [msg_id])

    # Wait for victim's lease loop to detect loss and cancel long_handler
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)
    await task

    # Direct check that stale worker cannot ACK or DLQ
    assert await dq.ack_if_owner(r, stream, msg_id, "debate-victim") is False
    assert await dq.dead_letter(r, stream, msg_id, {"instrument_id": INSTRUMENT}, "TEST", 1, "debate-victim") is False

    # The entry is still intact and owned by debate-thief
    rows = await r.xpending_range(stream, dq.GROUP, min=msg_id, max=msg_id, count=1)
    assert rows[0]["consumer"] == "debate-thief"


async def test_reaped_run_fences_off_late_decision_and_routing(debate_main, pool, monkeypatch):
    ref = f"stream:debate_queue/{uuid.uuid4().int % 10**13}-0"
    payload = {"instrument_id": INSTRUMENT, "trigger_source": "test"}

    def test_model():
        def fn(messages, info: AgentInfo):
            verdicts = {
                "recommendation": {"recommendation": "BUY", "conviction_score": 0.8, "allocation_pct": 5.0, "reasoning": "trend"},
                "final_allocation_pct": {"final_allocation_pct": 5.0, "conviction_level": "Overweight", "risk_reasoning": "size"},
            }
            if not info.output_tools:
                return ModelResponse(parts=[TextPart("argument")])
            tool = info.output_tools[0]
            key = next(k for k in verdicts if k in tool.parameters_json_schema.get("properties", {}))
            return ModelResponse(parts=[ToolCallPart(tool.name, verdicts[key])])
        return FunctionModel(fn)

    model = test_model()

    original_route = debate_main.route_entry
    route_called = False

    async def failing_route(*args, **kwargs):
        nonlocal route_called
        route_called = True
        return await original_route(*args, **kwargs)

    orig_read_marks = debate_main.read_marks

    async def reaper_read_marks(r):
        # Simulate reaper setting the run to FAILED right before finalization:
        await pool.execute("UPDATE debate_runs SET status = 'FAILED', failure_reason = 'REAPED_STALE' WHERE stream_message_id = $1", ref)
        return await orig_read_marks(r)

    monkeypatch.setattr(debate_main, "route_entry", failing_route)
    monkeypatch.setattr(debate_main, "read_marks", reaper_read_marks)

    with override_model(FLASH, model), override_model(PRO, model), override_model(EMBED, FakeEmbedder()):
        await debate_main.process_debate_trigger(pool, redis_double(), payload, Gateway(), message_ref=ref)

    assert route_called is False
    row = await pool.fetchrow("SELECT status, failure_reason FROM debate_runs WHERE stream_message_id = $1", ref)
    assert row["status"] == "FAILED" and row["failure_reason"] == "REAPED_STALE"
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 0


async def test_concurrent_exit_debate_triggers_claim_and_fence(debate_main, pool, monkeypatch):
    run_id = await insert_run(pool, "RUNNING", 1)
    context = {"current_position": {"instrument_id": INSTRUMENT, "quantity": 1.0}}
    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", AsyncMock(return_value=context))

    exit_decision = {"recommendation": "CLOSE", "conviction_score": 0.9, "target_stop_price": 50000.0, "reasoning": "stop"}

    def exit_model_fn(messages, info: AgentInfo):
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("exit reasoning")])
        tool = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(tool.name, exit_decision)])

    model = FunctionModel(exit_model_fn)
    ref1 = "stream:exit_debate_queue/100-0"
    ref2 = "stream:exit_debate_queue/200-0"
    with override_model(FLASH, model), override_model(PRO, model):
        await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                      Gateway(), message_ref=ref1)
        # Second trigger with different message_ref attempts to process the same run
        await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                      Gateway(), message_ref=ref2)

    decisions = await pool.fetch("SELECT * FROM exit_decisions WHERE run_id = $1", run_id)
    assert len(decisions) == 1
    run = await pool.fetchrow("SELECT status, stream_message_id FROM debate_runs WHERE id = $1", run_id)
    assert run["stream_message_id"] == ref1


async def test_exit_debate_no_position_preserves_reaped_status(debate_main, pool, monkeypatch):
    run_id = await insert_run(pool, "FAILED", 35)
    await pool.execute("UPDATE debate_runs SET failure_reason = 'REAPED_STALE' WHERE id = $1", run_id)
    context = {"current_position": None}
    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", AsyncMock(return_value=context))

    await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                  Gateway(), message_ref="stream:exit_debate_queue/300-0")
    run = await pool.fetchrow("SELECT status, failure_reason FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] == "FAILED" and run["failure_reason"] == "REAPED_STALE"


# --- B4: durable claim fencing (same message_ref = work identity, not owner) -------------------

POSITION = {"current_position": {"instrument_id": INSTRUMENT, "quantity": 1.0}}


def exit_decision_model(fail_from: int | None = None, counter: dict | None = None):
    """The exit fleet's verdict; optionally raise from the Nth call on (stale attempts run late)."""
    exit_decision = {"recommendation": "CLOSE", "conviction_score": 0.9, "target_stop_price": 50000.0, "reasoning": "stop"}

    def fn(messages, info: AgentInfo):
        if counter is not None:
            counter["n"] += 1
            if fail_from is not None and counter["n"] >= fail_from:
                raise RuntimeError("stale attempt failed late")
        if not info.output_tools:
            return ModelResponse(parts=[TextPart("exit reasoning")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, exit_decision)])

    return FunctionModel(fn)


async def test_first_exit_delivery_claims_and_routes_despite_stale_claim_generation(debate_main, pool, monkeypatch):
    """Regression (B4): the first exit delivery used to be gated on a delivery-counter comparison
    (``claim_gen < max(1, delivered)``), which matched zero rows when the stored generation was already
    >= the delivery count: the message was ACKed without any work. Claims now lock + prove + bump."""
    ref = "stream:exit_debate_queue/400-0"
    run_id = await insert_run(pool, "RUNNING", 1, stream_message_id=ref, claim_gen=1)
    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", AsyncMock(return_value=dict(POSITION)))
    model = exit_decision_model()
    with override_model(FLASH, model), override_model(PRO, model):
        await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                      Gateway(), message_ref=ref, delivered=1, consumer="debate-first")
    run = await pool.fetchrow("SELECT status, claim_gen FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] != "RUNNING" and run["claim_gen"] == 2  # claimed under the lock; delivered never gated it
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions WHERE run_id = $1", run_id) == 1


async def test_late_stale_claim_is_refused_and_writes_nothing(debate_main, pool, monkeypatch):
    ref = "stream:exit_debate_queue/800-0"
    run_id = await insert_run(pool, "RUNNING", 2, stream_message_id=ref, claim_gen=3)

    async def not_owner(redis_client, stream, msg_id, consumer):
        return False

    monkeypatch.setattr(debate_main, "is_pel_owner", not_owner)
    await debate_main.process_exit_debate_trigger(pool, redis_double(), {"instrument_id": INSTRUMENT, "run_id": str(run_id)},
                                                  Gateway(), message_ref=ref, consumer="debate-stale")
    run = await pool.fetchrow("SELECT status, claim_gen, failure_reason, completed_at FROM debate_runs WHERE id = $1", run_id)
    assert (run["status"], run["claim_gen"], run["failure_reason"], run["completed_at"]) == ("RUNNING", 3, None, None)
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions") == 0


async def run_same_ref_takeover(debate_main, monkeypatch, pool, ref, stale_return, *, fail_stale_from: int | None = None):
    """Old consumer claims the exit run and pauses inside its claim->finalize window; the new consumer
    takes the SAME message_ref over and finishes; only then is the old attempt released."""
    run_id = await insert_run(pool, "RUNNING", 1, stream_message_id=ref)
    claimed, release = asyncio.Event(), asyncio.Event()
    context_calls = {"n": 0}

    async def gated_context(self, instrument_id):
        context_calls["n"] += 1
        if context_calls["n"] == 1:  # the old attempt: paused right after its claim (gen 1)
            claimed.set()
            await release.wait()
            return stale_return
        return dict(POSITION)  # the takeover attempt

    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", gated_context)
    counter = {"n": 0}
    model = exit_decision_model(fail_from=fail_stale_from, counter=counter)
    payload = {"instrument_id": INSTRUMENT, "run_id": str(run_id)}
    with override_model(FLASH, model), override_model(PRO, model):
        old = asyncio.create_task(debate_main.process_exit_debate_trigger(
            pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-old"))
        await asyncio.wait_for(claimed.wait(), 5)
        new = asyncio.create_task(debate_main.process_exit_debate_trigger(
            pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-new"))
        await asyncio.wait_for(new, 10)  # takeover claims (gen 2) and completes while old is paused
        release.set()
        await asyncio.wait_for(old, 10)  # stale attempt resumes late
    return run_id


async def test_takeover_same_ref_old_success_late_is_fenced(debate_main, pool, monkeypatch):
    run_id = await run_same_ref_takeover(debate_main, monkeypatch, pool, "stream:exit_debate_queue/700-0", dict(POSITION))
    run = await pool.fetchrow("SELECT status, claim_gen, failure_reason FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] != "RUNNING" and run["failure_reason"] is None
    assert run["claim_gen"] == 2  # the takeover's claim bumped past the old attempt's gen 1
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions WHERE run_id = $1", run_id) == 1  # only the winner routed


async def test_takeover_same_ref_old_failure_late_writes_nothing(debate_main, pool, monkeypatch):
    # The old attempt's model calls come after the takeover's (it resumes last), so failing from call 4
    # makes only the stale attempt fail; its FAILED write must match the fenced generation and land nowhere.
    run_id = await run_same_ref_takeover(debate_main, monkeypatch, pool, "stream:exit_debate_queue/701-0",
                                         dict(POSITION), fail_stale_from=4)
    run = await pool.fetchrow("SELECT status, claim_gen, failure_reason FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] != "RUNNING" and run["failure_reason"] is None
    assert run["claim_gen"] == 2
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions WHERE run_id = $1", run_id) == 1


async def test_takeover_same_ref_old_no_position_late_writes_nothing(debate_main, pool, monkeypatch):
    run_id = await run_same_ref_takeover(debate_main, monkeypatch, pool, "stream:exit_debate_queue/702-0",
                                         {"current_position": None})
    run = await pool.fetchrow("SELECT status, claim_gen, failure_reason FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] not in ("RUNNING", "NO_TRADE") and run["failure_reason"] is None  # old's NO_TRADE write fenced
    assert run["claim_gen"] == 2
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions WHERE run_id = $1", run_id) == 1


async def test_late_entry_already_handled_cannot_close_the_new_owners_run(debate_main, pool, monkeypatch):
    ref = "stream:debate_queue/900-0"
    orphan = await insert_run(pool, "RUNNING", 1, stream_message_id=ref)

    async def owner_is(redis_client, stream, msg_id, consumer):
        return consumer == "debate-new"

    monkeypatch.setattr(debate_main, "is_pel_owner", owner_is)
    payload = {"instrument_id": INSTRUMENT, "trigger_source": "test"}

    # The stale attempt arrives late: it must not close the healthy run of the consumer that took over
    await debate_main.process_debate_trigger(pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-old")
    run = await pool.fetchrow("SELECT status, failure_reason FROM debate_runs WHERE id = $1", orphan)
    assert (run["status"], run["failure_reason"]) == ("RUNNING", None)
    assert await pool.fetchval("SELECT count(*) FROM portfolio_decisions") == 0

    # The current owner still closes the orphan as WORKER_LOST (durable no-re-debate policy preserved)
    await debate_main.process_debate_trigger(pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-new")
    run = await pool.fetchrow("SELECT status, failure_reason FROM debate_runs WHERE id = $1", orphan)
    assert (run["status"], run["failure_reason"]) == ("FAILED", "WORKER_LOST")


async def test_handle_refuses_entry_owned_by_another_consumer(r):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT})
    await read_without_handling(r, "debate-owner")
    handler = Recorder()
    intruder = dq.DebateQueue(r, "debate-intruder", handler)
    assert await intruder.handle(stream, msg_id, {"instrument_id": INSTRUMENT}) is False  # initial ownership proof
    assert handler.calls == []
    assert await pending_count(r, stream) == 1  # left pending for the real owner


async def test_recover_apply_refuses_entry_transferred_after_snapshot(r, pool, recover_script):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT})
    await read_without_handling(r, "debate-w1")
    await asyncio.sleep(0.1)
    plan = await recover_script.build_plan(r, pool, pel_idle_ms=50, consumer_idle_ms=10**9)
    assert [(p["message_id"], p["consumer"]) for p in plan.pending] == [(msg_id, "debate-w1")]

    await r.xclaim(stream, dq.GROUP, "debate-w2", 0, [msg_id])  # ownership transfer between snapshot and apply
    rollback = await recover_script.apply_plan(r, pool, plan)

    assert plan.applied_pending == 0 and rollback == []  # refused: no DLQ copy, no rollback XADD
    assert await pending_count(r, stream) == 1
    rows = await r.xpending_range(stream, dq.GROUP, min=msg_id, max=msg_id, count=1)
    assert rows[0]["consumer"] == "debate-w2"
    assert await r.xrange(dq.DLQ_STREAM) == []


async def test_recover_apply_refuses_consumer_that_got_new_pending_since_snapshot(r, pool, recover_script):
    stream = dq.STREAMS[0]
    await r.xgroup_createconsumer(stream, dq.GROUP, "debate-c")
    await asyncio.sleep(0.1)
    plan = await recover_script.build_plan(r, pool, pel_idle_ms=10**9, consumer_idle_ms=50)
    assert [c["name"] for c in plan.consumers] == ["debate-c"]  # idle and 0 pending at snapshot time

    await r.xadd(stream, {"instrument_id": INSTRUMENT})
    await r.xreadgroup(dq.GROUP, "debate-c", {stream: ">"}, count=1)  # new work lands under it before apply
    rollback = await recover_script.apply_plan(r, pool, plan)

    assert plan.applied_consumers == 0 and rollback == []
    names = {c["name"] for c in await r.xinfo_consumers(stream, dq.GROUP)}
    assert "debate-c" in names  # not deleted while it holds pending work


async def test_recover_apply_refuses_same_owner_renewed_after_snapshot(r, pool, recover_script):
    stream = dq.STREAMS[0]
    msg_id = await r.xadd(stream, {"instrument_id": INSTRUMENT, "trigger_source": "scanner"})
    await read_without_handling(r, "debate-w1")
    await asyncio.sleep(0.1)
    plan = await recover_script.build_plan(r, pool, pel_idle_ms=50, consumer_idle_ms=10**9)
    assert [(p["message_id"], p["consumer"]) for p in plan.pending] == [(msg_id, "debate-w1")]

    # Same owner reclaims/renews the lease after snapshot (simulates keep-alive/renewal) so idle becomes 0
    await r.xclaim(stream, dq.GROUP, "debate-w1", 0, [msg_id])

    rollback = await recover_script.apply_plan(r, pool, plan)
    assert plan.applied_pending == 0 and rollback == []
    assert await pending_count(r, stream) == 1
    rows = await r.xpending_range(stream, dq.GROUP, min=msg_id, max=msg_id, count=1)
    assert rows[0]["consumer"] == "debate-w1"
    assert await r.xrange(dq.DLQ_STREAM) == []


async def test_no_db_claim_takeover_old_attempt_writes_nothing(debate_main, pool, monkeypatch):
    ref = "stream:exit_debate_queue/1500-0"
    run_id = await insert_run(pool, "RUNNING", 1, stream_message_id=ref, claim_gen=1)

    claimed, release = asyncio.Event(), asyncio.Event()
    context_calls = {"n": 0}

    async def gated_context(self, instrument_id):
        context_calls["n"] += 1
        if context_calls["n"] == 1:
            claimed.set()
            await release.wait()
            return dict(POSITION)
        return dict(POSITION)

    monkeypatch.setattr(debate_main.AntiCorruptionLayer, "get_instrument_context", gated_context)
    model = exit_decision_model()
    payload = {"instrument_id": INSTRUMENT, "run_id": str(run_id)}
    with override_model(FLASH, model), override_model(PRO, model):
        old = asyncio.create_task(debate_main.process_exit_debate_trigger(
            pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-old"))
        await asyncio.wait_for(claimed.wait(), 5)
        # New owner takes over: reclaims message in PEL (different consumer)
        async def is_owner_new(r,s,m,c):
            return c == "debate-new"
        monkeypatch.setattr(debate_main, "is_pel_owner", is_owner_new)
        await asyncio.wait_for(debate_main.process_exit_debate_trigger(
            pool, redis_double(), payload, Gateway(), message_ref=ref, consumer="debate-new"), 10)
        release.set()
        await asyncio.wait_for(old, 10)

    run = await pool.fetchrow("SELECT status, claim_gen, failure_reason FROM debate_runs WHERE id = $1", run_id)
    assert run["status"] not in ("RUNNING",)
    assert run["claim_gen"] >= 2  # bumped by takeover(s)
    assert await pool.fetchval("SELECT count(*) FROM exit_decisions WHERE run_id = $1", run_id) == 1
