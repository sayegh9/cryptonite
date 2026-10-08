"""D1 / F-H2: the scanner records every candidate it finds, but only a fresh one (its bar closed within one
bar of now) goes to the debate queue, through an outbox row written in the candidate's own transaction and
relayed at least once. The queue is capped (XADD MAXLEN ~ 10000).

Runs ``ScannerWorker`` as the ``cryptonite_scanner`` role against a throwaway database at Alembic head and
Redis DB 15 (never the live DB 0), with a fake lake and a rule that fires on every bar."""

import asyncio
import json
import time as real_time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import polars as pl
import pytest
import redis.asyncio as aioredis
from connections import REDIS_URL
from cryptonite_scanner.models import SignalCandidateRead
from cryptonite_scanner.resampler import ONE_MINUTE_NS
from cryptonite_scanner_service import worker as worker_mod
from cryptonite_scanner_service.worker import ScannerWorker, is_fresh
from migrated_db import migrated_database

T_NS = 1_791_201_600 * 10**9  # 2026-10-05 12:00:00 UTC, a 15m boundary
NOW_NS = T_NS + 14 * ONE_MINUTE_NS + 30 * 10**9  # 12:14:30
DAY_NS = 86_400 * 10**9
INSTRUMENT = "BTCUSDT.BINANCE"
UNIVERSE = {"instrument_ids": [INSTRUMENT], "timeframes": ["1m", "15m"]}
RULES = [{"category": "trigger", "conditions": [{"field": "close", "operator": ">", "value": 0}]}]


def test_redis_db_is_isolated():
    assert redis_url().endswith("/15")


def redis_url() -> str:
    """The suite's Redis host and password, always DB 15: never the live stack's DB 0 or 1."""
    return urlunsplit(urlsplit(REDIS_URL)._replace(path="/15"))


def minutes(first_open_ns: int, last_open_ns: int) -> pl.DataFrame:
    opens = list(range(first_open_ns, last_open_ns + 1, ONE_MINUTE_NS))
    n = len(opens)
    return pl.DataFrame({
        "bar_open_ns": opens, "ts_event_ns": [o + ONE_MINUTE_NS for o in opens],
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.5] * n, "volume": [1.0] * n,
    })


# Lake up to now: minutes 10:00 .. 12:13 (the last closes at 12:14, 30 s before now)
LIVE_LAKE = minutes(T_NS - 2 * 60 * ONE_MINUTE_NS, T_NS + 13 * ONE_MINUTE_NS)
# A backfill: two hours of minutes ending two days ago, nothing since
BACKFILLED_LAKE = minutes(T_NS - 2 * DAY_NS - 2 * 60 * ONE_MINUTE_NS, T_NS - 2 * DAY_NS - ONE_MINUTE_NS)


class FakeQueryEngine:
    def __init__(self, df: pl.DataFrame):
        self.df = df

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        return self.df


def every_bar(df, rule_groups, risk_config, instrument_id, timeframe, spec_version_id, scan_run_id, price_precision=2):
    """Stand-in for ScannerEngine.evaluate: a rule that fires on every bar it is given."""
    return [SignalCandidateRead(
        id=uuid4(), scan_run_id=scan_run_id, spec_version_id=spec_version_id, instrument_id=instrument_id,
        timeframe=timeframe, bar_open_ns=r["bar_open_ns"], bar_close_inclusive_ns=r["interval_end_exclusive_ns"] - 1,
        interval_end_exclusive_ns=r["interval_end_exclusive_ns"], matched_rules=["always"], metrics={},
        suggested_entry=Decimal("100"), suggested_stop=Decimal("98"), suggested_target=Decimal("104"),
        created_at=datetime.now(UTC),
    ) for r in df.to_dicts()]


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_d1") as url:
        yield url


def as_scanner(url: str) -> str:
    """The scanner's real role, so a missing grant on the new outbox table fails here too."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(netloc=f"cryptonite_scanner:scanner_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))


@pytest.fixture
async def admin(db_url):
    conn = await asyncpg.connect(db_url)
    yield conn
    await conn.execute("TRUNCATE scanner_debate_outbox, outbox_events, signal_candidates, scanner_checkpoints, "
                       "scan_runs, scan_spec_versions, scan_specs, ingest_checkpoints CASCADE")
    await conn.close()


@pytest.fixture
async def redis15(monkeypatch):
    client = aioredis.from_url(redis_url(), decode_responses=True)
    assert (await client.connection_pool.get_connection()).db == 15  # never the live stack's DB
    stream = f"test:d1:debate_queue:{uuid4().hex[:8]}"
    monkeypatch.setattr(worker_mod, "DEBATE_QUEUE", stream)
    yield SimpleNamespace(client=client, stream=stream)
    await client.delete(stream)
    await client.aclose()


@pytest.fixture(autouse=True)
def frozen_scanner(monkeypatch):
    """Freeze the scanner's clock at NOW and make every bar a match (indicators are not under test)."""
    monkeypatch.setattr(worker_mod, "time", SimpleNamespace(
        time_ns=lambda: NOW_NS, strftime=real_time.strftime, gmtime=real_time.gmtime))
    monkeypatch.setattr(worker_mod.ScannerEngine, "evaluate", classmethod(lambda cls, *a, **k: every_bar(*a, **k)))
    monkeypatch.setattr(worker_mod.ScannerEngine, "enrich_indicators", classmethod(lambda cls, df: df))


async def seed_spec(admin, last_ingest_ns: int):
    spec_id, version_id = uuid4(), uuid4()
    await admin.execute("INSERT INTO scan_specs (id, slug, name, description) VALUES ($1, $2, 'D1', 'd1')",
                        spec_id, f"d1-{uuid4().hex[:8]}")
    await admin.execute(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           VALUES ($1, $2, 1, 'h', $3::jsonb, $4::jsonb)""", version_id, spec_id, json.dumps(UNIVERSE), json.dumps(RULES))
    await admin.execute("UPDATE scan_specs SET active_version_id = $1 WHERE id = $2", version_id, spec_id)
    await admin.execute(
        """INSERT INTO ingest_checkpoints (venue, market_type, symbol, bar_type, timeframe, first_timestamp_ns,
               last_timestamp_ns) VALUES ('BINANCE', 'SPOT', 'BTCUSDT', 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL', '1m', 0, $1)""",
        last_ingest_ns)
    return version_id


async def make_worker(db_url, lake: pl.DataFrame, redis_client) -> ScannerWorker:
    w = ScannerWorker(database_url=as_scanner(db_url), analytics_lake_path="/nonexistent-d1-lake")
    w.query_engine = FakeQueryEngine(lake)
    w.pool = await asyncpg.create_pool(w.database_url, min_size=1, max_size=4)
    w.redis = redis_client
    return w


async def queued(redis15) -> list[dict]:
    return [json.loads(f["candidate_payload"]) for _, f in await redis15.client.xrange(redis15.stream)]


async def test_restart_after_backfill_queues_no_historical_candidate(db_url, admin, redis15):
    version_id = await seed_spec(admin, last_ingest_ns=int(BACKFILLED_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, BACKFILLED_LAKE, redis15.client)
    try:
        await w.reconcile_and_prime()  # what start() does after a restart: HISTORICAL_PRIMING
    finally:
        await w.pool.close()
    recorded = await admin.fetchval("SELECT count(*) FROM signal_candidates WHERE spec_version_id = $1", version_id)
    assert recorded == 120 + 8  # history is still recorded for research: every 1m and 15m bar
    assert await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox") == 0
    assert await queued(redis15) == []


async def test_only_the_bars_that_just_closed_are_queued(db_url, admin, redis15):
    version_id = await seed_spec(admin, last_ingest_ns=int(LIVE_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, LIVE_LAKE, redis15.client)
    try:
        await w.reconcile_and_prime()
        # the next closed-bar event re-evaluates the same window: nothing new to record or queue
        await w._execute_scan(spec_version_id=version_id, spec_hash="h", universe_data=UNIVERSE, rules_data=RULES,
                              trigger_reason="COMMITTED_BAR_EVENT", trigger_event_id=str(uuid4()))
    finally:
        await w.pool.close()
    assert await admin.fetchval("SELECT count(*) FROM signal_candidates WHERE spec_version_id = $1", version_id) == 134 + 8
    entries = await queued(redis15)
    # 1m: the minute that closed at 12:14 (30 s ago); 15m: the 11:45 window that closed at 12:00 (14.5 min ago)
    assert sorted((e["timeframe"], e["interval_end_exclusive_ns"]) for e in entries) == [
        ("15m", T_NS), ("1m", T_NS + 14 * ONE_MINUTE_NS)]
    rows = await admin.fetch("SELECT candidate_id, status, published_stream_id FROM scanner_debate_outbox")
    assert {r["status"] for r in rows} == {"PUBLISHED"} and all(r["published_stream_id"] for r in rows)
    assert {str(r["candidate_id"]) for r in rows} == {e["candidate_id"] for e in entries}
    # the stream entry keeps the debate worker's contract
    _, fields = (await redis15.client.xrange(redis15.stream))[0]
    assert set(fields) == {"instrument_id", "trigger_source", "candidate_payload", "brief_key"}
    assert fields["trigger_source"] == "scanner" and fields["instrument_id"] == INSTRUMENT


async def test_a_crash_between_commit_and_enqueue_loses_nothing(db_url, admin, redis15, monkeypatch):
    version_id = await seed_spec(admin, last_ingest_ns=int(LIVE_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, LIVE_LAKE, redis15.client)

    async def dies_after_commit(self, *a, **k):  # the process is killed right after the commit
        return None

    with monkeypatch.context() as m:
        m.setattr(ScannerWorker, "_publish_debate_candidates", dies_after_commit)
        try:
            await w._execute_scan(spec_version_id=version_id, spec_hash="h", universe_data=UNIVERSE,
                                  rules_data=RULES, trigger_reason="MANUAL_JOB", trigger_event_id=str(uuid4()))
        finally:
            await w.pool.close()
    assert await queued(redis15) == []
    assert await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox WHERE status = 'PENDING'") == 2

    # the restarted scanner relays them once their lease has run out (backdated here instead of waiting)
    await admin.execute("UPDATE scanner_debate_outbox SET next_attempt_at = now() - interval '1 second'")
    restarted = await make_worker(db_url, LIVE_LAKE, redis15.client)
    try:
        assert await restarted.relay_debate_outbox_once() == 2
        assert await restarted.relay_debate_outbox_once() == 0  # published rows are not sent again
    finally:
        await restarted.pool.close()
    entries = await queued(redis15)
    assert len(entries) == 2
    assert await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox WHERE status = 'PUBLISHED'") == 2


async def test_the_relay_never_queues_a_candidate_that_aged_out(db_url, admin, redis15):
    """D1 review blocker 2: the relay rechecks the one-bar window immediately before the queue write —
    rows it only reaches after an outage are completed as EXPIRED (with the reason), never enqueued."""
    version_id = await seed_spec(admin, last_ingest_ns=int(LIVE_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, LIVE_LAKE, DownRedis(redis15.client))
    try:
        await w._execute_scan(spec_version_id=version_id, spec_hash="h", universe_data=UNIVERSE, rules_data=RULES,
                              trigger_reason="MANUAL_JOB", trigger_event_id=str(uuid4()))
        assert await admin.fetchval("SELECT count(*) FROM scanner_debate_outbox WHERE status = 'PENDING'") == 2
        # the relay only gets to them a day later: age every stored payload past its fresh window
        await admin.execute(
            """UPDATE scanner_debate_outbox
               SET next_attempt_at = now() - interval '1 second',
                   fields = jsonb_set(fields, '{candidate_payload,interval_end_exclusive_ns}',
                                      to_jsonb($1::bigint))""",
            NOW_NS - DAY_NS)
        w.redis = redis15.client  # Redis is back, but these setups are old news now
        assert await w.relay_debate_outbox_once() == 0
    finally:
        await w.pool.close()
    assert await queued(redis15) == []
    rows = await admin.fetch("SELECT status, last_error FROM scanner_debate_outbox")
    assert {r["status"] for r in rows} == {"EXPIRED"}
    assert all("expired before publish" in (r["last_error"] or "") for r in rows)


class DownRedis:
    """Redis that accepts the brief but refuses the queue write, like an outage mid-publish."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def xadd(self, *a, **k):
        raise ConnectionError("redis down")


async def test_a_failed_enqueue_is_retried_by_the_relay(db_url, admin, redis15):
    version_id = await seed_spec(admin, last_ingest_ns=int(LIVE_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, LIVE_LAKE, DownRedis(redis15.client))
    try:
        await w._execute_scan(spec_version_id=version_id, spec_hash="h", universe_data=UNIVERSE, rules_data=RULES,
                              trigger_reason="MANUAL_JOB", trigger_event_id=str(uuid4()))
        rows = await admin.fetch("SELECT status, attempt_count, last_error FROM scanner_debate_outbox")
        assert [(r["status"], r["attempt_count"]) for r in rows] == [("PENDING", 1)] * 2
        assert all("redis down" in r["last_error"] for r in rows)
        assert await w.relay_debate_outbox_once() == 0  # still backing off

        await admin.execute("UPDATE scanner_debate_outbox SET next_attempt_at = now() - interval '1 second'")
        w.redis = redis15.client  # Redis is back
        assert await w.relay_debate_outbox_once() == 2
    finally:
        await w.pool.close()
    assert len(await queued(redis15)) == 2
    rows = await admin.fetch("SELECT status, attempt_count FROM scanner_debate_outbox")
    assert [(r["status"], r["attempt_count"]) for r in rows] == [("PUBLISHED", 2)] * 2


async def test_relay_gives_up_after_max_attempts(db_url, admin, redis15, monkeypatch):
    monkeypatch.setattr(worker_mod, "DEBATE_RELAY_MAX_ATTEMPTS", 2)
    version_id = await seed_spec(admin, last_ingest_ns=int(LIVE_LAKE["ts_event_ns"].max()))
    w = await make_worker(db_url, LIVE_LAKE, DownRedis(redis15.client))
    try:
        await w._execute_scan(spec_version_id=version_id, spec_hash="h", universe_data=UNIVERSE, rules_data=RULES,
                              trigger_reason="MANUAL_JOB", trigger_event_id=str(uuid4()))
        await admin.execute("UPDATE scanner_debate_outbox SET next_attempt_at = now() - interval '1 second'")
        assert await w.relay_debate_outbox_once() == 0
    finally:
        await w.pool.close()
    rows = await admin.fetch("SELECT status, attempt_count FROM scanner_debate_outbox")
    assert [(r["status"], r["attempt_count"]) for r in rows] == [("FAILED", 2)] * 2  # kept, visible, not lost


async def test_the_debate_queue_is_capped(redis15):
    fields = {"instrument_id": INSTRUMENT, "trigger_source": "scanner", "candidate_payload": "{}", "brief_key": ""}
    for _ in range(10_600):
        await worker_mod.enqueue_debate(redis15.client, fields)
    # MAXLEN ~ trims whole stream nodes (100 entries each by default), never below the cap
    assert 10_000 <= await redis15.client.xlen(redis15.stream) <= 10_100


class RecordingRedis:
    def __init__(self):
        self.calls = []

    async def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, kwargs))
        return "1-0"


def test_every_debate_queue_writer_passes_the_cap():
    r = RecordingRedis()
    asyncio.run(worker_mod.enqueue_debate(r, {"instrument_id": INSTRUMENT}))
    assert r.calls == [(worker_mod.DEBATE_QUEUE, {"maxlen": 10_000, "approximate": True})]


@pytest.mark.parametrize("age_ns, tf, fresh", [
    (0, "1m", True), (ONE_MINUTE_NS, "1m", True), (ONE_MINUTE_NS + 1, "1m", False),
    (14 * ONE_MINUTE_NS, "15m", True), (15 * ONE_MINUTE_NS + 1, "15m", False),
    (59 * ONE_MINUTE_NS, "1h", True), (2 * DAY_NS, "1h", False),
    (-1, "1m", False),  # not closed yet: never fresh
    (0, "7m", False),   # unknown timeframe: never queued
])
def test_is_fresh(age_ns, tf, fresh):
    c = SimpleNamespace(timeframe=tf, interval_end_exclusive_ns=NOW_NS - age_ns)
    assert is_fresh(c, NOW_NS) is fresh
