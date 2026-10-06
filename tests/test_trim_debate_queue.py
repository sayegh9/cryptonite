"""D1: the one-off ``scripts/trim_debate_queue.py`` reports exactly what it would drop, changes nothing in a
dry run, trims to the cap with ``--apply``, and refuses to drop undelivered or unacked entries without
``--force`` — revalidating the current groups and PEL atomically inside the same Lua script as the XTRIM,
so groups created or pending work that appears between plan and apply is never dropped unforced.
Redis DB 15 only (never the live DB 0)."""

import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import redis
from connections import REDIS_URL

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trim_debate_queue.py"


@pytest.fixture(scope="module")
def trim():
    spec = importlib.util.spec_from_file_location("trim_debate_queue", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def r():
    client = redis.Redis.from_url(urlunsplit(urlsplit(REDIS_URL)._replace(path="/15")), decode_responses=True)
    assert client.connection_pool.connection_kwargs["db"] == 15
    yield client
    for key in client.scan_iter("test:d1trim:*"):
        client.delete(key)
    client.close()


def fill(r, n: int, read: int, ack: int) -> str:
    """n entries; the group has read the first ``read`` and acknowledged the first ``ack`` of them."""
    stream = f"test:d1trim:{uuid4().hex[:8]}"
    for i in range(n):
        r.xadd(stream, {"instrument_id": "BTCUSDT.BINANCE", "trigger_source": "scanner" if i % 4 else "user_on_demand"})
    r.xgroup_create(stream, "debate_workers", id="0")
    if read:
        got = r.xreadgroup("debate_workers", "c1", {stream: ">"}, count=read)[0][1]
        if ack:
            r.xack(stream, "debate_workers", *[i for i, _ in got[:ack]])
    return stream


def test_dry_run_reports_and_changes_nothing(trim, r):
    stream = fill(r, 120, read=120, ack=120)
    p = trim.plan_trim(r, stream, 100)
    assert (p.length, p.drop, p.unsafe) == (120, 20, False)
    assert sum(p.sources.values()) == 20 and p.sources["user_on_demand"] == 5
    assert r.xlen(stream) == 120
    assert "would drop the oldest 20" in trim.report(p)


def test_apply_trims_exactly_the_planned_entries(trim, r):
    stream = fill(r, 120, read=120, ack=120)
    p = trim.plan_trim(r, stream, 100)
    r.xadd(stream, {"trigger_source": "scanner"})  # a new entry between plan and apply is kept
    assert trim.apply_trim(r, p) == 20
    assert r.xlen(stream) == 101 and r.xrange(stream, count=1)[0][0] == p.first_kept


def test_a_group_created_after_the_plan_blocks_the_apply(trim, r):
    stream = fill(r, 120, read=120, ack=120)
    p = trim.plan_trim(r, stream, 100)
    assert not p.unsafe
    r.xgroup_create(stream, "late_group", id="0")  # created after planning: never delivered anything
    with pytest.raises(trim.UnsafeTrim, match="changed since planning"):
        trim.apply_trim(r, p)
    assert r.xlen(stream) == 120  # the atomic revalidation dropped nothing
    assert trim.apply_trim(r, p, force=True) == 20  # explicit force does it
    assert r.xlen(stream) == 100 and r.xrange(stream, count=1)[0][0] == p.first_kept


def test_newly_pending_entries_between_plan_and_apply_block_the_apply(trim, r):
    stream = fill(r, 120, read=120, ack=120)
    p = trim.plan_trim(r, stream, 100)
    assert not p.unsafe
    # rewind the group and re-read everything unacked: PEL entries land inside the drop range while
    # last-delivered-id stays current, so only the PEL revalidation can catch this
    r.xgroup_setid(stream, "debate_workers", id="0")
    r.xreadgroup("debate_workers", "c2", {stream: ">"}, count=200)
    with pytest.raises(trim.UnsafeTrim, match="changed since planning"):
        trim.apply_trim(r, p)
    assert r.xlen(stream) == 120 and r.xpending(stream, "debate_workers")["pending"] == 120


def test_undelivered_or_unacked_entries_make_the_plan_unsafe(trim, r):
    stream = fill(r, 120, read=10, ack=5)
    p = trim.plan_trim(r, stream, 100)
    assert p.undelivered == {"debate_workers": 10} and p.pending == {"debate_workers": 5} and p.unsafe


def test_nothing_to_trim_under_the_cap(trim, r):
    stream = fill(r, 50, read=50, ack=50)
    p = trim.plan_trim(r, stream, 100)
    assert p.drop == 0 and trim.apply_trim(r, p) == 0 and "nothing to trim" in trim.report(p)
