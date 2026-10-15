"""Exit monitor (blueprint A2.6): deterministic exits by default, exit debates only on events.

Includes a replay of synthetic price paths: no hard stop is missed and paid exit debates drop versus
the old rule (debate at -2% or every 24 h, re-armed every 4 h)."""

import json
import math
import random
import uuid

import asyncpg
import httpx
import pytest
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import ExitAction, Position, QuoteBook, evaluate_exit
from migrated_db import migrated_database


def pos(current: float, side: str = "LONG", entry: float = 100.0, ai: bool = True, **kw) -> Position:
    return Position("BTCUSDT.BINANCE", side, entry, current, ai, **kw)


# --- Rules --------------------------------------------------------------------------------------

def test_hard_stop_closes_ai_positions_and_only_alerts_on_shared_ones():
    assert evaluate_exit(pos(94.9), best_price=100, debate_armed=True) == [ExitAction("CLOSE", "HARD_STOP")]
    assert evaluate_exit(pos(94.9, ai=False), 100, True) == [ExitAction("ALERT", "HARD_STOP")]
    assert evaluate_exit(pos(105.1, side="SHORT"), 100, True) == [ExitAction("CLOSE", "HARD_STOP")]


def test_trailing_stop_after_activation():
    assert evaluate_exit(pos(103.5), best_price=104, debate_armed=True) == []  # gave back 0.5%
    assert evaluate_exit(pos(101.8), best_price=104, debate_armed=True) == [ExitAction("CLOSE", "TRAILING_STOP")]
    assert evaluate_exit(pos(99.0), best_price=102.5, debate_armed=False) == []  # never reached +3%


def test_quote_book_serves_only_fresh_quotes_and_the_exit_side():
    book = QuoteBook()
    book.apply({"instrument_id": "BTCUSDT.BINANCE", "bid_price": "99", "ask_price": "101", "arrival_ns": 1_000 * 10**9})
    assert book.exit_price("BTCUSDT.BINANCE", "LONG", now_s=1_005) == 99
    assert book.exit_price("BTCUSDT.BINANCE", "SHORT", now_s=1_005) == 101
    assert book.exit_price("BTCUSDT.BINANCE", "LONG", now_s=1_011) is None  # stale: no evaluation
    assert book.exit_price("ETHUSDT.BINANCE", "LONG", now_s=1_005) is None


def test_exit_debates_only_on_events_and_when_armed():
    assert evaluate_exit(pos(97.9), 100, True) == [ExitAction("EXIT_DEBATE", "DRAWDOWN")]
    assert evaluate_exit(pos(97.9), 100, False) == []
    assert evaluate_exit(pos(100.5, funding_rate=0.0008), 100, True) == [ExitAction("EXIT_DEBATE", "FUNDING_SPIKE")]
    assert evaluate_exit(pos(100.5, side="SHORT", funding_rate=0.0008), 100, True) == []  # shorts receive it
    assert evaluate_exit(pos(100.5), 100, True) == []  # a quiet, old position costs nothing


# --- Replay -------------------------------------------------------------------------------------

def replay(seed: int, vol: float, minutes: int = 3 * 24 * 60) -> tuple[bool, int, int]:
    """(missed_hard_stop, new_rule_debates, old_rule_debates) for one random-walk path, 1-min steps."""
    rng = random.Random(seed)
    price, best = 100.0, 100.0
    new_debates = old_debates = 0
    new_armed_at = old_armed_at = -math.inf
    in_episode = False
    for minute in range(minutes):
        price *= math.exp(rng.gauss(0, vol))
        best = max(best, price)
        p = pos(price)
        if p.pnl_pct > -em.DEBATE_DRAWDOWN_PCT * em.REARM_FRACTION:
            in_episode = False
        actions = evaluate_exit(p, best, minute - new_armed_at >= 240, in_episode)
        if any(a.kind == "CLOSE" for a in actions):
            return (False, new_debates, old_debates)
        if any(a.kind == "EXIT_DEBATE" for a in actions):
            new_debates += 1
            new_armed_at = minute
            in_episode = in_episode or any(a.reason == "DRAWDOWN" for a in actions)
        if minute - old_armed_at >= 240 and (p.pnl_pct < -2 or minute >= 24 * 60):
            old_debates += 1
            old_armed_at = minute
        if p.pnl_pct <= -em.STOP_LOSS_PCT - 0.5:
            return (True, new_debates, old_debates)  # a stop that should have fired already
    return (False, new_debates, old_debates)


@pytest.mark.parametrize("vol, label", [(0.0008, "~3%/day, typical BTC"), (0.0015, "~5.7%/day, high volatility")])
def test_replay_never_misses_a_hard_stop_and_runs_fewer_exit_debates(vol, label):
    results = [replay(seed, vol) for seed in range(200)]
    assert not any(missed for missed, _, _ in results)
    new_total = sum(n for _, n, _ in results)
    old_total = sum(o for _, _, o in results)
    print(f"exit debates, 200 three-day paths at {label}: new rules {new_total}, old rules {old_total}")
    assert new_total < old_total, (new_total, old_total)


# --- Against the database ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_exittest") as url:
        yield url


class Redis:
    def __init__(self):
        self.kv, self.streams, self.published = {}, [], []

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def exists(self, key):
        return int(key in self.kv)

    async def delete(self, key):
        self.kv.pop(key, None)

    async def xadd(self, stream, fields, **kwargs):
        self.streams.append((stream, fields))

    async def publish(self, channel, message):
        self.published.append(json.loads(message))

    async def hincrbyfloat(self, key, field, amount):
        h = self.kv.setdefault(key, {})
        h[field] = h.get(field, 0.0) + amount
        return h[field]

    async def expire(self, key, seconds):
        return True


def rest_book(bid: float, ask: float | None = None, calls: list | None = None) -> httpx.AsyncClient:
    """Binance's REST book ticker, answering every symbol with one price."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url)
        return httpx.Response(200, json={"symbol": request.url.params["symbol"], "bidPrice": str(bid),
                                         "bidQty": "1", "askPrice": str(ask or bid), "askQty": "1"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def rest_down() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("network down", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def book(price: float, instrument: str = "BTCUSDT.BINANCE") -> QuoteBook:
    import time as _t

    b = QuoteBook()
    b.apply({"instrument_id": instrument, "bid_price": str(price), "ask_price": str(price), "arrival_ns": _t.time_ns()})
    return b


async def seed(pool, instance_ids: list[str], price: float, sides: list[str] | None = None,
               instrument: str = "BTCUSDT.BINANCE"):
    strat_id = uuid.uuid4()
    await pool.execute("TRUNCATE paper_positions, paper_strategy_instances, paper_accounts, execution_commands CASCADE")
    await pool.execute("INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 't', 't')",
                       strat_id, f"exit-{strat_id.hex[:8]}")
    await pool.execute("""INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit,
                          default_parameters, parameter_schema) VALUES ($1, $1, 1, 'h', 'c', '{}', '{}')""", strat_id)
    await pool.execute("""INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance,
                          total_equity, realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
                          VALUES ('PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 1e5, 1e5, 1e5, 0, 0, false, now())""")
    for i, inst in enumerate(instance_ids):
        await pool.execute("""INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id,
                              strategy_source_hash, risk_policy_hash, strategy_name, instrument_id, timeframe,
                              resolved_parameters, allocation_usd, status)
                              VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'h', 'h', $3, $4, '1h', '{}', 1000, $5)""",
                           inst, strat_id, f"s-{i}", instrument, "RUNNING" if i == 0 else "SYSTEM")
        side = (sides or ["LONG"] * len(instance_ids))[i]
        await pool.execute("""INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
                              entry_price, quantity, current_price, status)
                              VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, $4, 100, 0.1, 100, 'OPEN')""",
                           f"P-{i}", inst, instrument, side)  # current_price deliberately stale (fill price)


async def test_ai_position_below_stop_is_closed_once_at_the_live_price(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        redis = Redis()
        await em.monitor_once(pool, redis, book(100.0))  # live price at entry: nothing to do
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        await em.monitor_once(pool, redis, book(94.0))
        await em.monitor_once(pool, redis, book(94.0))  # already pending: no duplicate
        rows = await pool.fetch("SELECT command_type, environment, requester_id, payload FROM execution_commands")
        assert len(rows) == 1
        assert (rows[0]["command_type"], rows[0]["environment"], rows[0]["requester_id"]) == ("CLOSE_POSITION", "PAPER", "risk:exit_monitor")
        assert json.loads(rows[0]["payload"])["reason"] == "HARD_STOP"
        assert sum(1 for p in redis.published if p["action"] == "CLOSE") == 1  # alert debounced
    finally:
        await pool.close()


async def test_rejected_close_is_retried(db_url, monkeypatch):
    monkeypatch.setattr(em, "CLOSE_RETRY_AFTER_S", 0)
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        redis = Redis()
        await em.monitor_once(pool, redis, book(94.0))
        await pool.execute("UPDATE execution_commands SET status = 'REJECTED'")
        await em.monitor_once(pool, redis, book(94.0))
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 2
    finally:
        await pool.close()


async def test_stale_quotes_are_never_acted_on(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        await em.monitor_once(pool, Redis(), QuoteBook())  # no quote at all
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    finally:
        await pool.close()


async def test_shared_instrument_only_alerts(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER", "MANUAL_TRADE"], price=94.0)
        redis = Redis()
        await em.monitor_once(pool, redis, book(94.0))
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        assert redis.published[-1]["action"] == "ALERT"
    finally:
        await pool.close()


async def test_ownership_spans_both_sides_of_a_perp(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        perp = "BTCUSDT-PERP.BINANCE"
        await seed(pool, ["AI_DEBATE_WORKER", "MANUAL_TRADE"], price=106.0, sides=["SHORT", "LONG"], instrument=perp)
        await em.monitor_once(pool, Redis(), book(106.0, perp))  # AI short is -6%, but a manual long shares it
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
    finally:
        await pool.close()


async def test_missing_quotes_raise_a_critical_alert(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        redis, coverage = Redis(), em.Coverage()
        await em.monitor_once(pool, redis, QuoteBook(), coverage=coverage)
        assert not [p for p in redis.published if p.get("reason") == "NO_FRESH_QUOTE"]  # one blind check: a blip
        await em.monitor_once(pool, redis, QuoteBook(), coverage=coverage)
        await em.monitor_once(pool, redis, QuoteBook(), coverage=coverage)
        alerts = [p for p in redis.published if p.get("reason") == "NO_FRESH_QUOTE"]
        assert len(alerts) == 1 and alerts[0]["severity"] == "CRITICAL"  # sustained: alerted once, debounced
        assert json.loads(redis.kv[em.COVERAGE_KEY])["positions"][0]["source"] is None
    finally:
        await pool.close()


async def test_a_stale_stream_quote_falls_back_to_rest_and_the_stop_still_fires(db_url):
    """The 2026-10-15 XRP stop: the stream quote went stale on every reconnect and the stop was skipped."""
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    calls: list = []
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        redis = Redis()
        await em.monitor_once(pool, redis, QuoteBook(), http=rest_book(94.0, calls=calls))
        row = await pool.fetchrow("SELECT command_type, payload FROM execution_commands")
        assert row["command_type"] == "CLOSE_POSITION" and json.loads(row["payload"])["reason"] == "HARD_STOP"
        assert str(calls[0]).startswith(em.SPOT_BOOK_TICKER_URL) and calls[0].params["symbol"] == "BTCUSDT"
        assert json.loads(redis.kv[em.COVERAGE_KEY])["positions"] == [
            {"instrument_id": "BTCUSDT.BINANCE", "source": "REST", "blind_since": None}]
        assert not [p for p in redis.published if p.get("reason") == "NO_FRESH_QUOTE"]
    finally:
        await pool.close()


async def test_a_fresh_stream_quote_never_calls_rest(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    calls: list = []
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=100.0)
        redis = Redis()
        await em.monitor_once(pool, redis, book(100.0), http=rest_book(94.0, calls=calls))
        assert calls == [] and await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        assert json.loads(redis.kv[em.COVERAGE_KEY])["positions"][0]["source"] == "STREAM"
    finally:
        await pool.close()


async def test_blind_when_rest_is_down_too_and_the_blind_time_is_counted(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_DEBATE_WORKER"], price=94.0)
        redis, coverage = Redis(), em.Coverage()
        await em.monitor_once(pool, redis, QuoteBook(), http=rest_down(), coverage=coverage)
        coverage.last_check -= 30  # the next check runs one interval later
        await em.monitor_once(pool, redis, QuoteBook(), http=rest_down(), coverage=coverage)
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0  # never acts without a price
        blind = {k: v for k, v in redis.kv.items() if k.startswith("exit_monitor:blind_s:")}
        assert len(blind) == 1 and 29 <= next(iter(blind.values()))["BTCUSDT.BINANCE"] <= 31
        assert [p["reason"] for p in redis.published] == ["NO_FRESH_QUOTE"]
        # priced again: the blind run ends and a later blind run alerts only after two checks again
        await em.monitor_once(pool, redis, QuoteBook(), http=rest_book(99.0), coverage=coverage)
        assert coverage.blind_cycles == {} and json.loads(redis.kv[em.COVERAGE_KEY])["positions"][0]["blind_since"] is None
    finally:
        await pool.close()


async def test_mandate_position_is_protected_and_closed_under_its_own_instance(db_url):
    """A5.2: positions booked under a mandate's own instance keep the deterministic stops."""
    mandate_instance = "AI_MANDATE_" + uuid.uuid4().hex
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, [mandate_instance], price=94.0)
        await em.monitor_once(pool, Redis(), book(94.0))
        row = await pool.fetchrow("SELECT command_type, target_instance_id FROM execution_commands")
        assert (row["command_type"], row["target_instance_id"]) == ("CLOSE_POSITION", mandate_instance)
    finally:
        await pool.close()


async def test_two_ai_owners_on_one_instrument_only_alert(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await seed(pool, ["AI_MANDATE_" + uuid.uuid4().hex, "AI_DEBATE_WORKER"], price=94.0)
        redis = Redis()
        await em.monitor_once(pool, redis, book(94.0))
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        assert redis.published[-1]["action"] == "ALERT"
    finally:
        await pool.close()


async def test_rest_exit_price_uses_the_exit_side_and_the_right_market():
    calls: list = []
    http = rest_book(99.0, 101.0, calls=calls)
    assert await em.rest_exit_price(http, "XRPUSDT.BINANCE", "LONG") == 99.0  # a long exits at the bid
    assert await em.rest_exit_price(http, "BTCUSDT-PERP.BINANCE", "SHORT") == 101.0  # a short at the ask
    assert str(calls[0]).startswith(em.SPOT_BOOK_TICKER_URL) and calls[0].params["symbol"] == "XRPUSDT"
    assert str(calls[1]).startswith(em.PERP_BOOK_TICKER_URL) and calls[1].params["symbol"] == "BTCUSDT"
    assert await em.rest_exit_price(rest_down(), "XRPUSDT.BINANCE", "LONG") is None
    assert await em.rest_exit_price(None, "XRPUSDT.BINANCE", "LONG") is None


async def test_coverage_counts_blind_checks_in_a_row_and_forgets_closed_positions():
    redis, coverage = Redis(), em.Coverage()
    await coverage.record(redis, {"A.BINANCE": None, "B.BINANCE": "REST"}, now_s=1_000.0)
    await coverage.record(redis, {"A.BINANCE": None, "B.BINANCE": "REST"}, now_s=1_030.0)
    assert coverage.blind_cycles == {"A.BINANCE": 2}
    snap = json.loads(redis.kv[em.COVERAGE_KEY])
    assert snap["positions"] == [{"instrument_id": "A.BINANCE", "source": None, "blind_since": 1_000.0},
                                 {"instrument_id": "B.BINANCE", "source": "REST", "blind_since": None}]
    assert [p["blind_s"] for p in redis.published] == [30]
    await coverage.record(redis, {}, now_s=1_060.0)  # the position closed
    assert coverage.blind_cycles == {} and coverage.blind_since == {}
