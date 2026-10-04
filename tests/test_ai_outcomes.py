"""Outcome labeller and outcome-based reflections (blueprint A7.1). The lake is a fake query engine over
synthetic 1m bars; models are pydantic-ai test doubles; the database is a throwaway at Alembic head."""

import datetime
import sys
from pathlib import Path

import asyncpg
import polars as pl
import pytest
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import FLASH
from migrated_db import migrated_database
from pydantic_ai.models.test import TestModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import outcomes  # noqa: E402
import reflection  # noqa: E402

MIN_NS = 60 * 10**9
T0 = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
T0_NS = int(T0.timestamp() * 10**9)


def bars(start_price: float, step: float, n: int, start_ns: int = T0_NS) -> pl.DataFrame:
    """n 1m bars after start: close rises by ``step`` per bar, highs/lows 0.5 around the close."""
    closes = [start_price + step * (i + 1) for i in range(n)]
    return pl.DataFrame({"ts_event_ns": [start_ns + (i + 1) * MIN_NS for i in range(n)],
                         "close": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes]})


def test_label_window_return_and_excursions():
    out = outcomes.label_window(100.0, T0_NS, 3600, bars(100.0, 0.1, 60))
    assert out["bars"] == 60
    assert out["return_pct"] == pytest.approx(0.06)  # 100 -> 106
    assert out["mfe"] == pytest.approx(0.065) and out["mae"] == pytest.approx(-0.004)  # first low 100.1 - 0.5


def test_a_gappy_window_is_not_labelled():
    assert outcomes.label_window(100.0, T0_NS, 3600, bars(100.0, 0.1, 50)) is None


@pytest.mark.parametrize("rec, ret, expected", [("BUY", 0.02, 0.02), ("HOLD", 0.02, -0.02), ("SELL", -0.03, 0.03)])
def test_signed_outcome(rec, ret, expected):
    assert outcomes.signed_outcome(rec, ret) == expected


class FakeLake:
    def __init__(self, frames: dict[str, pl.DataFrame]):
        self.frames = frames

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        df = self.frames.get(symbol, pl.DataFrame())
        if df.is_empty():
            return df
        return df.filter((pl.col("ts_event_ns") >= (start_ns or 0)) & (pl.col("ts_event_ns") <= (end_ns or 2**63)))


class FakeEmbedder:
    async def embed_texts(self, texts):
        return [[0.1] * 768 for _ in texts], 1


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_outcometest") as url:
        yield url


async def test_labels_elapsed_horizons_then_reflects_on_the_24h_outcome(db_url):
    day = 24 * 60
    btc_before = pl.DataFrame({"ts_event_ns": [T0_NS - MIN_NS], "close": [60000.0], "high": [60000.0], "low": [60000.0]})
    lake = FakeLake({
        "ETHUSDT": bars(3000.0, 0.05, day + 60),  # +2.4% in 24h (3000 -> 3072)
        "BTCUSDT": pl.concat([btc_before, bars(60000.0, 0.5, day + 60)]),  # +1.2% in 24h
    })
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                      "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
            await conn.execute("INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, "
                               "allocation_pct, reasoning, created_at, decision_price) "
                               "VALUES ($1, 'ETHUSDT.BINANCE', 'BUY', 0.7, 5, 'trend', $2, 3000)", run, T0)
        now = T0 + datetime.timedelta(hours=25)  # 1h, 4h and 24h have elapsed; 72h has not
        assert await outcomes.label_pending(pool, lake, now) == 3
        assert await outcomes.label_pending(pool, lake, now) == 0  # idempotent
        async with pool.acquire() as conn:
            got = {r["horizon"]: float(r["return_pct"]) for r in await conn.fetch("SELECT horizon, return_pct FROM ai_decision_outcomes")}
        assert set(got) == {"1h", "4h", "24h"} and got["24h"] == pytest.approx(0.024)

        gw = Gateway(budget=BudgetGuard())
        with override_model(FLASH, TestModel(custom_output_text="Trend-aligned buys with room to target worked.")), \
                override_model("google:text-embedding-004", FakeEmbedder()):
            assert await reflection.ReflectionEngine(pool, lake, gw).process_pending_reflections() == 1
            assert await reflection.ReflectionEngine(pool, lake, gw).process_pending_reflections() == 0
        async with pool.acquire() as conn:
            r = await conn.fetchrow("SELECT actual_outcome_pnl, alpha_vs_benchmark, outcome, embedding_model, timeframe FROM reflections")
        assert float(r["actual_outcome_pnl"]) == pytest.approx(0.024)
        assert float(r["alpha_vs_benchmark"]) == pytest.approx(0.012, abs=1e-4)  # 2.4% vs BTC's 1.2%
        assert (r["outcome"], r["embedding_model"], r["timeframe"]) == ("WIN", "google:text-embedding-004", "24h")
    finally:
        await pool.close()
