"""Stage 1.7 (leverage plan): the exit monitor values perps at their mark, carries leverage and the liquidation
price, raises a CRITICAL LIQUIDATION_PROXIMITY alert when half the stop-to-liquidation buffer is used and closes an
AI-owned perp (reduce-only) at three quarters, as the backup to the resting stop."""

import json
import time

import asyncpg
import pytest
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import ExitAction, Position, evaluate_exit
from test_exit_monitor import Redis, book, db_url, seed  # noqa: F401 - db_url is a fixture

PERP = "BTCUSDT-PERP.BINANCE"


def perp_pos(current, side="LONG", entry=100.0, liq=90.0, stop=None, ai=True):
    return Position(PERP, side, entry, current, ai, stop_price=stop, leverage=10, liquidation_price=liq)


def test_buffer_is_measured_from_the_stop_else_the_entry():
    assert perp_pos(96.0, stop=96.0).liquidation_buffer_used == 0.0
    assert perp_pos(93.0, stop=96.0).liquidation_buffer_used == pytest.approx(0.5)
    assert perp_pos(95.0).liquidation_buffer_used == pytest.approx(0.5)  # no stop: entry 100 -> liq 90
    short = perp_pos(105.0, side="SHORT", liq=110.0)
    assert short.liquidation_buffer_used == pytest.approx(0.5)
    assert perp_pos(99.0, liq=None).liquidation_buffer_used is None  # spot / no liquidation price
    # a stop beyond the liquidation price is ignored: the entry is the reference
    assert perp_pos(95.0, stop=85.0).liquidation_buffer_used == pytest.approx(0.5)


def test_half_used_buffer_raises_the_critical_alert():
    pos = perp_pos(93.4, stop=96.0)  # (96 - 93.4) / 6 = 43 %: nothing from the proximity rule
    assert ExitAction("ALERT", "LIQUIDATION_PROXIMITY") not in evaluate_exit(pos, 100.0, False)
    pos = perp_pos(92.9, stop=96.0)  # 52 %
    actions = evaluate_exit(pos, 100.0, False)
    assert actions[0] == ExitAction("ALERT", "LIQUIDATION_PROXIMITY")
    assert actions[1].kind == "CLOSE"  # the price is through the stop (and the hard stop) as well


def test_three_quarters_closes_an_ai_perp_and_only_alerts_a_manual_one():
    assert evaluate_exit(perp_pos(91.4, stop=96.0), 100.0, False) == [ExitAction("CLOSE", "LIQUIDATION_PROXIMITY")]
    assert evaluate_exit(perp_pos(91.4, stop=96.0, ai=False), 100.0, False) == \
        [ExitAction("ALERT", "LIQUIDATION_PROXIMITY")]


def test_short_proximity():
    assert evaluate_exit(perp_pos(107.6, side="SHORT", liq=110.0), 100.0, False) == \
        [ExitAction("CLOSE", "LIQUIDATION_PROXIMITY")]


def test_spot_is_unchanged():
    spot = Position("BTCUSDT.BINANCE", "LONG", 100.0, 94.0, True)
    assert evaluate_exit(spot, 100.0, False) == [ExitAction("CLOSE", "HARD_STOP")]


async def _seed_perp(pool, leverage=10, liq=90.5, side="LONG"):
    await seed(pool, ["AI_DEBATE_WORKER"], price=100.0, instrument=PERP, sides=[side])
    await pool.execute("UPDATE paper_positions SET leverage = $1, margin_mode = 'ISOLATED', liquidation_price = $2",
                       leverage, liq)


def _mark(redis, price):
    redis.kv["perp:mark:BTCUSDT"] = json.dumps({"type": "perp.mark", "mark_price": str(price),
                                                "ts_event_ns": time.time_ns()})


async def test_perp_is_valued_at_the_mark_and_carries_leverage_and_liquidation(db_url):  # noqa: F811
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        await _seed_perp(pool)
        redis = Redis()
        _mark(redis, 99.0)
        sources: dict = {}
        async with pool.acquire() as conn:
            (p,) = await em.load_positions(conn, redis, book(150.0, PERP), sources=sources)
        assert (p.current_price, p.leverage, p.liquidation_price, p.price_source) == (99.0, 10, 90.5, "MARK")
        assert sources == {PERP: "MARK"}
        redis.kv.pop("perp:mark:BTCUSDT")  # no fresh mark: the quote stream, as before
        async with pool.acquire() as conn:
            (p,) = await em.load_positions(conn, redis, book(98.0, PERP))
        assert (p.current_price, p.price_source) == (98.0, "STREAM")
    finally:
        await pool.close()


async def test_monitor_alerts_then_closes_near_liquidation(db_url):  # noqa: F811
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        # 10x long from 100, liquidation 96 (a tight one, so the 5 % hard stop does not fire first)
        await _seed_perp(pool, liq=96.0)
        redis = Redis()
        _mark(redis, 97.9)  # 52 % of the 100 -> 96 buffer
        await em.monitor_once(pool, redis, book(100.0, PERP))
        assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        alerts = [p for p in redis.published if p["reason"] == "LIQUIDATION_PROXIMITY"]
        assert alerts and alerts[0]["action"] == "ALERT"
        # Leverage plan 4.6: a CRITICAL alert the notifier sends to Telegram and ntfy, saying how close it is
        assert alerts[0]["severity"] == "CRITICAL" and alerts[0]["liquidation_price"] == 96.0
        assert "used 52% of the buffer from the stop (else the entry) to liquidation (96)" in alerts[0]["message"]
        _mark(redis, 96.9)  # 77 %
        await em.monitor_once(pool, redis, book(100.0, PERP))
        rows = await pool.fetch("SELECT command_type, payload FROM execution_commands")
        assert [(r["command_type"], json.loads(r["payload"])["reason"]) for r in rows] == \
            [("CLOSE_POSITION", "LIQUIDATION_PROXIMITY")]
    finally:
        await pool.close()
