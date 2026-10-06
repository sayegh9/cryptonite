"""B3 (F-H3): the orchestrator marks paper positions to market, and readers prefer the live mark.

- a price move updates paper_positions.current_price / unrealized_pnl and paper_accounts.updated_at
  (and total_equity) within the projector's throttle window;
- a quote older than the max age marks nothing, and readers then flag the stored mark stale;
- the debate router's room values the mandate's position at the live mark;
- the enforced mandate check still prefers the order's live reference price over the stored mark.

Runs against a throwaway DB at Alembic head; Redis is an in-memory fake (never the dev Redis).
"""

import asyncio
import importlib.util
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from cryptonite_execution.bridge import (
    CoalescedMarkToMarketProjector,
    EventBridge,
    EventBridgeWorker,
    recompute_total_equity,
)
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_orchestrator import marks as orch_marks
from cryptonite_orchestrator.mandate_gate import mandate_exposure
from cryptonite_schemas.mandate import Mandate
from cryptonite_schemas.marks import MARKS_KEY, encode_mark, parse_marks, resolve_mark, summarize
from migrated_db import migrated_database, seed_paper_account

D = Decimal
ACCOUNT = "PAPER-ACCOUNT-001"
BTC = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
WORKER = Path(__file__).resolve().parents[1] / "services" / "debate_worker"


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}


class FakeQuoteCache:
    def __init__(self):
        self.quotes = {}

    def set(self, inst, bid, ask, ts_ns):
        self.quotes[inst] = SimpleNamespace(bid_price=D(str(bid)), ask_price=D(str(ask)), ts_init=ts_ns)

    def quote(self, inst):
        return self.quotes.get(inst)


def fake_engine(marks=None, stale=()):
    marks = marks or {}
    d = SimpleNamespace(mark_prices=dict(marks), mark_ts_ns={k: time.time_ns() for k in marks},
                        is_mark_stale=lambda inst: inst in stale or inst not in marks,
                        perp_positions={}, open_orders={})
    return SimpleNamespace(dispatcher=d, account_id=ACCOUNT)


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_b3") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    p = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE paper_orders, paper_positions CASCADE")
        await seed_paper_account(conn, ACCOUNT, cash=100000)
        await conn.execute("UPDATE paper_accounts SET cash_balance = 95000, total_equity = 100000, "
                           "updated_at = now() - interval '1 hour' WHERE account_id = $1", ACCOUNT)
    try:
        yield p
    finally:
        await p.close()


async def add_instance(conn, instance_id):
    strat = await conn.fetchval("SELECT strategy_version_id FROM paper_strategy_instances WHERE instance_id = 'MANUAL_TRADE'")
    await conn.execute(
        """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
               risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
           VALUES ($1, $2, $3, 'ai_mandate', 'ai_mandate', 'AIMandate', 'AI.MANDATE', '1m', '{}'::jsonb, 0, 'SYSTEM')
           ON CONFLICT DO NOTHING""", instance_id, ACCOUNT, strat)


async def add_position(conn, instance_id="MANUAL_TRADE", inst=BTC, side="LONG", entry="50000", qty="0.1"):
    pid = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price, quantity,
               current_price, unrealized_pnl, status) VALUES ($1, $2, $3, $4, $5, $6, $7, $6, 0, 'OPEN')""",
        pid, ACCOUNT, instance_id, inst, side, D(entry), D(qty))
    return pid


# --- the projector, wired as the orchestrator wires it ----------------------------------------------

async def test_price_move_updates_marks_and_account_within_the_throttle_window(pool):
    async with pool.acquire() as conn:
        pid = await add_position(conn)
        before = await conn.fetchrow("SELECT total_equity, updated_at FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    quotes, redis = FakeQuoteCache(), FakeRedis()
    quotes.set(BTC, 50000, 50001, time.time_ns())
    projector = await orch_marks.start_mark_to_market(pool, redis, fake_engine(), quotes, interval_s=1.0)
    try:
        quotes.set(BTC, 52000, 52001, time.time_ns())  # the price moves
        deadline = time.monotonic() + 2.5  # one 1 s throttle window plus slack
        row = None
        while time.monotonic() < deadline:
            row = await pool.fetchrow("SELECT current_price, unrealized_pnl FROM paper_positions WHERE position_id = $1", pid)
            if row["current_price"] == D("52000"):
                break
            await asyncio.sleep(0.1)
    finally:
        await projector.stop()
    assert row["current_price"] == D("52000")  # a long is marked at the bid
    assert row["unrealized_pnl"] == D("200")  # 0.1 * (52000 - 50000)
    acc = await pool.fetchrow("SELECT total_equity, updated_at FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    assert acc["updated_at"] > before["updated_at"]
    assert acc["total_equity"] == D("95000") + D("0.1") * D("52000")
    live = parse_marks(await redis.hgetall(MARKS_KEY))
    assert live[BTC].price == D("52000")
    assert resolve_mark(BTC, D("50000"), live).source == "live"


async def test_short_perp_is_marked_from_the_dispatcher_mark(pool):
    async with pool.acquire() as conn:
        pid = await add_position(conn, inst=PERP, side="SHORT", entry="50000", qty="0.04")
    projector = orch_marks.CoalescedMarkToMarketProjector(
        pool, lambda inst: orch_marks.live_mark_price(fake_engine({PERP: D("51000")}), FakeQuoteCache(), inst))
    assert await projector.project_once() == 1
    row = await pool.fetchrow("SELECT current_price, unrealized_pnl FROM paper_positions WHERE position_id = $1", pid)
    assert (row["current_price"], row["unrealized_pnl"]) == (D("51000"), D("-40"))
    assert await projector.project_once() == 0  # unchanged mark: no write


async def test_stale_quote_marks_nothing_and_readers_flag_the_stored_mark(pool):
    async with pool.acquire() as conn:
        pid = await add_position(conn)
        before = await conn.fetchval("SELECT updated_at FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    quotes, redis = FakeQuoteCache(), FakeRedis()
    old_ns = time.time_ns() - 60 * 1_000_000_000  # the feed stopped a minute ago
    quotes.set(BTC, 52000, 52001, old_ns)
    projector = orch_marks.CoalescedMarkToMarketProjector(
        pool, lambda inst: orch_marks.live_mark_price(fake_engine(), quotes, inst),
        on_marked=lambda m: orch_marks.publish_marks(redis, m))
    assert await projector.project_once() == 0
    row = await pool.fetchrow("SELECT current_price FROM paper_positions WHERE position_id = $1", pid)
    assert row["current_price"] == D("50000")
    assert await pool.fetchval("SELECT updated_at FROM paper_accounts WHERE account_id = $1", ACCOUNT) == before
    # a reader with a mark the projector last confirmed a minute ago, and one with none at all
    redis.hashes[MARKS_KEY] = {BTC: encode_mark(D("50000"), old_ns, old_ns)}
    view = resolve_mark(BTC, row["current_price"], parse_marks(await redis.hgetall(MARKS_KEY)))
    assert (view.source, view.stale, view.price) == ("stored", True, D("50000"))
    assert 55 < view.age_s < 120
    never = resolve_mark(BTC, row["current_price"], {})
    assert never.fields() == {"mark_source": "stored", "mark_stale": True, "mark_age_s": None}
    assert summarize([view, never]) == {"marks_stale": True, "marks_age_s": None}


def test_interval_is_clamped_to_one_to_five_seconds():
    assert orch_marks.mark_interval_s("") == 2.0
    assert orch_marks.mark_interval_s("0.25") == 1.0
    assert orch_marks.mark_interval_s("30") == 5.0
    assert orch_marks.mark_interval_s("junk") == 2.0


# --- readers ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def routing():
    spec = importlib.util.spec_from_file_location("b3_mandate_routing", WORKER / "mandate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_mandate_room_uses_the_live_mark(pool, routing):
    m = Mandate(id=uuid.uuid4(), name="b3", capital_budget_usd=D("10000"), instruments=[BTC], max_position_pct=D("25"))
    async with pool.acquire() as conn:
        await add_instance(conn, m.instance_id)
        await add_position(conn, m.instance_id, entry="50000", qty="0.04")  # $2,000 at entry: 20% of the budget
        stored_room = await routing.entry_room_pct(conn, m, BTC)
        now = time.time_ns()
        live = parse_marks({BTC: encode_mark(D("40000"), now, now)})  # the price fell 20%
        live_room = await routing.entry_room_pct(conn, m, BTC, live)
    assert stored_room == D("5")  # 25% limit - 20% held at the stored (entry) mark
    assert live_room == D("9")  # 25% - $1,600 (16%) at the live mark


async def test_enforced_mandate_check_prefers_the_live_reference_price(pool):
    m = Mandate(id=uuid.uuid4(), name="b3-gate", capital_budget_usd=D("10000"), instruments=[BTC], max_position_pct=D("25"))
    async with pool.acquire() as conn:
        await add_instance(conn, m.instance_id)
        await add_position(conn, m.instance_id, entry="50000", qty="0.04")  # stored mark 50,000
        await conn.execute(
            """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, price, filled_qty, avg_fill_price, status) VALUES ($1, $2, $3, $4, 'BUY', 'MARKET',
                   0.04, NULL, 0.04, 50000, 'FILLED')""", f"b3-{uuid.uuid4().hex[:8]}", m.instance_id, ACCOUNT, BTC)
        engine = fake_engine()
        with_ref = await mandate_exposure(conn, engine, m, BTC, {BTC: D("60000")})
        without = await mandate_exposure(conn, engine, m, BTC, {})
    assert with_ref.instrument_usd == D("2400")  # the order's live reference price wins
    assert without.instrument_usd == D("2000")  # else the stored mark


# --- concurrency regression & quote validation ---------------------------------------------------

async def test_recompute_total_equity_is_atomic_and_locks_account_row(pool):
    """Deterministic regression for the concurrent-fill race:
    recompute_total_equity holds account FOR UPDATE in a transaction, preventing interleaved
    bridge fill commits from having their updated equity overwritten by a stale position sum.
    """
    async with pool.acquire() as conn:
        pid = await add_position(conn)  # 0.1 BTC @ 50,000 = $5,000 pos_val; cash = 95,000

    # Connection 1 acquires the account row FOR UPDATE
    async with pool.acquire() as conn1:
        async with conn1.transaction():
            await conn1.fetchval("SELECT account_id FROM paper_accounts WHERE account_id = $1 FOR UPDATE", ACCOUNT)

            # Connection 2 attempts recompute_total_equity: must block waiting for conn1's lock
            started = asyncio.Event()
            done = asyncio.Event()

            async def run_recompute():
                async with pool.acquire() as conn2:
                    started.set()
                    await recompute_total_equity(conn2, ACCOUNT)
                    done.set()

            task = asyncio.create_task(run_recompute())
            await started.wait()
            await asyncio.sleep(0.1)
            assert not done.is_set(), "recompute_total_equity must be blocked waiting for account lock"

            # While holding the lock, simulate a fill that closes the position and sets cash = 100,000
            await conn1.execute("UPDATE paper_positions SET status = 'CLOSED', quantity = 0 WHERE position_id = $1", pid)
            await conn1.execute("UPDATE paper_accounts SET cash_balance = 100000 WHERE account_id = $1", ACCOUNT)

        # conn1 commits here, releasing the account lock
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await task

    acc = await pool.fetchrow("SELECT cash_balance, total_equity FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    assert acc["cash_balance"] == D("100000")
    # Crucial regression assertion: total_equity must be exactly $100,000, NOT $100,000 + stale $5,000 open_val!
    assert acc["total_equity"] == D("100000")


async def test_concurrent_bridge_fill_and_mark_projection_equity_race(pool):
    """End-to-end race test: EventBridgeWorker commits an order fill closing a position concurrently
    with CoalescedMarkToMarketProjector.project_once(); total_equity must never be corrupted with phantom equity.
    """
    async with pool.acquire() as conn:
        pid = await add_position(conn, entry="50000", qty="0.1")  # pos_val = $5,000
        acc_before = await conn.fetchrow("SELECT cash_balance, total_equity FROM paper_accounts WHERE account_id = $1", ACCOUNT)
        assert acc_before["cash_balance"] == D("95000")
        assert acc_before["total_equity"] == D("100000")

    bridge = EventBridge(LatchedKillSwitch())
    worker = EventBridgeWorker(bridge, pool, engine_run_id=f"RUN-{uuid.uuid4().hex[:8]}")

    quotes = FakeQuoteCache()
    quotes.set(BTC, 52000, 52001, time.time_ns())
    projector = CoalescedMarkToMarketProjector(pool, lambda inst: orch_marks.live_mark_price(fake_engine(), quotes, inst))

    # Fill event that closes the 0.1 BTC position at 52,000:
    # trade proceeds: 0.1 * 52,000 - commission 1.00 = +5,199.00 -> cash = 95,000 + 5,199 = 100,199
    fill_evt = {
        "event_type": "ORDER_FILLED",
        "event_id": uuid.uuid4(),
        "native_event_id": str(uuid.uuid4()),
        "client_order_id": f"ORD-{uuid.uuid4().hex[:8]}",
        "event_version": 1,
        "side": "SELL",
        "price": "52000",
        "quantity": "0.1",
        "commission": "1",
        "account_id": ACCOUNT,
        "instance_id": "MANUAL_TRADE",
        "instrument_id": BTC,
        "occurred_at_ns": time.time_ns(),
    }
    bridge.queue.put_nowait(fill_evt)

    # Run bridge fill and projector concurrently
    await asyncio.gather(
        worker.drain_and_commit_batch(),
        projector.project_once(),
    )

    acc_after = await pool.fetchrow("SELECT cash_balance, total_equity FROM paper_accounts WHERE account_id = $1", ACCOUNT)
    pos_after = await pool.fetchrow("SELECT status, quantity FROM paper_positions WHERE position_id = $1", pid)

    assert pos_after["status"] == "CLOSED"
    assert acc_after["cash_balance"] == D("100199")
    # Total equity must match cash_balance exactly since positions are closed (no phantom $5,000 or $5,200)
    assert acc_after["total_equity"] == D("100199")


def test_nonfinite_and_future_quotes_rejected():
    """Verify NaN, Infinity, negative/zero prices, and future timestamps are rejected across
    orchestrator quote intake, schema parsing, and mark resolution.
    """
    from cryptonite_schemas.marks import LiveMark

    now = time.time_ns()
    engine = fake_engine()
    quotes = FakeQuoteCache()

    # 1. Nonfinite/negative/zero quotes in live_mark_price
    for bad_px in ("nan", "inf", "-inf", "0", "-100"):
        quotes.set(BTC, bad_px, bad_px, now)
        assert orch_marks.live_mark_price(engine, quotes, BTC, now_ns=now) is None

    # Future timestamp in live_mark_price (> 1s in the future)
    quotes.set(BTC, 50000, 50001, now + 10_000_000_000)
    assert orch_marks.live_mark_price(engine, quotes, BTC, now_ns=now) is None

    # Perp nonfinite and future marks in live_mark_price
    assert orch_marks.live_mark_price(fake_engine({PERP: D("nan")}), quotes, PERP, now_ns=now) is None
    assert orch_marks.live_mark_price(fake_engine({PERP: D("inf")}), quotes, PERP, now_ns=now) is None
    assert orch_marks.live_mark_price(fake_engine({PERP: D("-50")}), quotes, PERP, now_ns=now) is None

    future_engine = fake_engine({PERP: D("50000")})
    future_engine.dispatcher.mark_ts_ns[PERP] = now + 10_000_000_000
    assert orch_marks.live_mark_price(future_engine, quotes, PERP, now_ns=now) is None

    # 2. Schema parse_marks ignores nonfinite, zero, and bogus timestamps
    bad_hash = {
        b"BTCUSDT.BINANCE": b'{"price": "NaN", "quote_ts_ns": 1, "marked_at_ns": 1}',
        b"ETHUSDT.BINANCE": b'{"price": "Infinity", "quote_ts_ns": 1, "marked_at_ns": 1}',
        b"SOLUSDT.BINANCE": b'{"price": "-10", "quote_ts_ns": 1, "marked_at_ns": 1}',
        b"ADAUSDT.BINANCE": b'{"price": "0", "quote_ts_ns": 1, "marked_at_ns": 1}',
        b"XRPUSDT.BINANCE": b'{"price": "1.0", "quote_ts_ns": 0, "marked_at_ns": 1}',
    }
    assert parse_marks(bad_hash) == {}

    # 3. resolve_mark rejects future quote timestamps as live
    future_live = {BTC: LiveMark(D("50000"), now + 10_000_000_000, now)}
    view = resolve_mark(BTC, D("48000"), future_live, now_ns=now)
    assert view.source == "stored"
    assert view.stale is True

    # 4. Projector _live_price rejects nonfinite and future quotes
    mock_pool = None
    proj = CoalescedMarkToMarketProjector(mock_pool, lambda inst: D("nan"))
    assert proj._live_price(BTC) is None
    proj_inf = CoalescedMarkToMarketProjector(mock_pool, lambda inst: D("inf"))
    assert proj_inf._live_price(BTC) is None
    proj_fut = CoalescedMarkToMarketProjector(mock_pool, lambda inst: (D("50000"), now + 10_000_000_000))
    assert proj_fut._live_price(BTC) is None

