"""H8: an operator debate's BUY and SELL verdicts reach an approval card through the real worker path.

Stubbed verdicts (FunctionModels, no paid call) for a PERP profile with direction BOTH go through the evidence gate on
complete fixture bars, the structural levels from the lake (the scanner's math on a synthetic 1m frame), the H7a CRO
clamp and mandate routing, onto an ``ai_approvals`` card with a stop and a target. A run with incomplete bars stays
non-actionable and stages no card.
"""
import json
import math
import sys
import time
import uuid

import asyncpg
import polars as pl
import pytest
from cryptonite_agents import tools
from migrated_db import migrated_database, seed_paper_account
from test_debate_gate import Models, load, run
from test_debate_gate import Redis as GateRedis

BTC = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
MINUTE_NS = 60 * 10**9
OPERATOR = {"instrument_id": BTC, "trigger_source": "user_on_demand"}
ANALYST = ("Measured: last close 65000.0 inside the window's range; 1 lower low against 1 higher high.\n"
           "Interpretation: range-bound.")


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_h8_e2e") as url:
        yield url


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h8_e2e", "main.py")


def lake_frame(last_close: float) -> pl.DataFrame:
    """52 hours of contiguous 1m bars around the 65,000-65,020 quote, ending at the current minute."""
    count = 3120
    end = time.time_ns() // MINUTE_NS * MINUTE_NS
    opens = [end - (count - i) * MINUTE_NS for i in range(count)]
    close = [last_close + math.sin((i - count + 1) / 30) * 300 for i in range(count)]
    return pl.DataFrame({"bar_open_ns": opens, "ts_event_ns": [o + MINUTE_NS for o in opens], "open": close,
                         "high": [c + 100 for c in close], "low": [c - 100 for c in close], "close": close,
                         "volume": [10.0] * count})


@pytest.fixture
def lake(request, monkeypatch, main):
    """The levels are the scanner's from the last close; the card is graded at the quote, so the last close sits on
    the quote's far side for the verdict (ask 65,020 for a long, bid 65,000 for a short) to keep the preset's R:R."""
    import cryptonite_storage.queries as queries

    frame = lake_frame(getattr(request, "param", 65000.0))

    class Lake:
        def __init__(self, *args, **kwargs):
            self._con = self

        def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
            return frame.filter((pl.col("ts_event_ns") > start_ns) & (pl.col("ts_event_ns") <= end_ns))

        def close(self):
            pass

    monkeypatch.setattr(queries, "AnalyticsQueryEngine", Lake)  # the analyst's bars block
    monkeypatch.setattr(sys.modules["structure"], "AnalyticsQueryEngine", Lake)  # the structural levels
    monkeypatch.setattr(tools, "derivatives_evidence", lambda instrument_id, now_s=None: {
        "symbol": "BTCUSDT", "perp_instrument": PERP, "source": "live feed",
        "funding_rate": {"value": 0.0001, "as_of": None, "age_s": 5.0}, "open_interest": {"unavailable": "missing"},
        "mark_price": {"value": 65010.0, "as_of": None, "age_s": 5.0}})


@pytest.fixture
async def pool(db_url):
    from cryptonite_execution import profile_lifecycle
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_orchestrator.mandate_gate import ensure_mandate_instance

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await seed_paper_account(conn)
        await conn.execute("""
            INSERT INTO instruments (instrument_id, raw_symbol, venue, market_type, base_currency, quote_currency,
                                     price_precision, size_precision, price_increment, size_increment, min_notional,
                                     status, is_active)
            VALUES ($1, 'BTCUSDT', 'BINANCE', 'SPOT', 'BTC', 'USDT', 2, 6, 0.01, 0.000001, 10, 'TRADING', true)
            ON CONFLICT (instrument_id) DO UPDATE SET is_active = true""", BTC)
        await conn.execute("INSERT INTO system_settings(key, value) VALUES ('autotrade.enabled', '1') "
                           "ON CONFLICT (key) DO UPDATE SET value = '1'")
        mid = await conn.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, autonomy_level, status,
                                        allow_perps, markets, max_leverage, direction, style)
               VALUES ($1, 10000, ARRAY[$2], 25, 1, 'ACTIVE', true, ARRAY['PERP'], 3, 'BOTH', 'INTRADAY')
               RETURNING id""", f"h8-perp-{uuid.uuid4().hex[:6]}", PERP)
        await ensure_mandate_instance(conn, await load_mandate(conn, mid), "PAPER-ACCOUNT-001")
        await conn.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('BTC', $1)", mid)
    profile_lifecycle.reset_switch_cache()
    yield pool
    await pool.execute("TRUNCATE ai_approvals, debate_turns, portfolio_decisions, debate_runs CASCADE")
    await pool.execute("DELETE FROM ai_profile_coin_locks")
    await pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await pool.close()


def worker_redis() -> GateRedis:
    redis = GateRedis("MARKET BRIEF BTCUSDT")
    redis.kv["perp:mark:BTCUSDT"] = json.dumps({"type": "perp.mark", "mark_price": "65010", "funding_rate": "0.0001",
                                                "ts_event_ns": time.time_ns()})
    return redis


@pytest.mark.parametrize("pm, cro, side, lake", [
    ({"recommendation": "BUY", "conviction_score": 0.7, "allocation_pct": 5.0, "reasoning": "long"},
     {"final_allocation_pct": 8.0, "conviction_level": "Buy", "risk_reasoning": "size up"}, "BUY", 65030.0),
    ({"recommendation": "SELL", "conviction_score": 0.7, "allocation_pct": 5.0, "reasoning": "short"},
     {"final_allocation_pct": 4.0, "conviction_level": "Sell", "risk_reasoning": "ok"}, "SELL", 64990.0),
], indirect=["lake"])
async def test_a_directional_verdict_reaches_an_approval_card_with_structural_levels(main, pool, lake, pm, cro, side):
    run_row = await run(main, pool, OPERATOR, Models(pm=pm, cro=cro, analyst_text=ANALYST), worker_redis())
    assert (run_row["status"], run_row["failure_reason"]) == ("PENDING_APPROVAL", None)
    d = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run_row["id"])
    assert d["recommendation"] == side
    assert float(d["allocation_pct"]) <= 5.0  # the CRO clamp: never above the PM's size
    card = await pool.fetchrow("SELECT payload FROM ai_approvals WHERE source = 'debate' AND source_ref = $1",
                               str(run_row["id"]))
    assert card is not None
    payload = card["payload"] if isinstance(card["payload"], dict) else json.loads(card["payload"])
    assert payload["instrument_id"] == PERP and payload["side"] == side
    stop, target = float(payload["stop_loss"]), float(payload["take_profit"])
    assert (stop < 65000 < target) if side == "BUY" else (target < 65000 < stop)


async def test_a_missing_data_run_stays_non_actionable_with_no_card(main, pool, lake, monkeypatch):
    monkeypatch.setattr(tools, "recent_bars", lambda instrument_id, timeframe="1h", n=48, now_ns=None: {
        "instrument_id": instrument_id, "timeframe": timeframe, "expected": n, "returned": 0, "missing": n,
        "complete": False, "reason": "no closed 1h bars in the lake", "bars": [], "summary": None})
    pm = {"recommendation": "SELL", "conviction_score": 0.8, "allocation_pct": 5.0, "reasoning": "short"}
    cro = {"final_allocation_pct": 5.0, "conviction_level": "Sell", "risk_reasoning": "ok"}
    run_row = await run(main, pool, OPERATOR, Models(pm=pm, cro=cro, analyst_text=ANALYST), worker_redis())
    assert (run_row["status"], run_row["failure_reason"]) == ("NO_TRADE", "EVIDENCE_INCOMPLETE")
    assert await pool.fetchval("SELECT count(*) FROM ai_approvals") == 0
