"""Debate queue recovery and run reaper (review-plan B4, F-H6): a killed worker's message is reclaimed,
a poison message goes to the DLQ after 3 deliveries, a redelivery never opens a second run, stale RUNNING
runs are reaped, idle consumers are deleted, and the one-off recovery script. Redis: the isolated test DB
(connections.REDIS_URL, DB 15) with per-test stream names. Postgres: a throwaway DB at Alembic head.
Models are FunctionModels; the worker's Redis in debate runs is an AsyncMock (nothing is published)."""

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
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, stream_message_id)
           VALUES ($1, $2, $3, 'scanner', now() - make_interval(secs => $4), $5)""",
        run_id, INSTRUMENT, status, age_min * 60, extra.get("stream_message_id"))
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
    assert "Stuck PEL entries (idle > 10 min): 1" in capsys.readouterr().out

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
