"""F1 regime tags (reporting only): the classifier on synthetic series, the backtest breakdown, outcome
aggregation by regime, migration 034, the learner storing the regime, the backfill, and the by-regime
endpoint (throwaway database at Alembic head)."""

import asyncio
import datetime
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
import polars as pl
import pytest
from cryptonite_backtester.metrics import UNKNOWN_REGIME, regime_breakdown, resample_ohlc_1h
from cryptonite_execution.calibration import outcomes_by_regime
from cryptonite_scanner import regime as rg
from migrated_db import migrated_database

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "debate_worker"))
import outcomes  # noqa: E402

HOUR_NS = 3600 * 10**9
N = 800  # > 30 days of 1h bars


def frame(closes: list[float], half_ranges: list[float], start_ns: int = 0) -> pl.DataFrame:
    return pl.DataFrame({"ts_event_ns": [start_ns + (i + 1) * HOUR_NS for i in range(len(closes))],
                         "high": [c + r for c, r in zip(closes, half_ranges, strict=True)],
                         "low": [c - r for c, r in zip(closes, half_ranges, strict=True)],
                         "close": closes})


def trend(direction: int, n: int = N) -> pl.DataFrame:
    """Steady trend whose relative range shrinks slowly, so the latest ATR% is the window's lowest."""
    closes = [1000.0 * (1 + 0.002 * direction) ** i for i in range(n)]
    return frame(closes, [c * 0.003 * (1 - i / (2 * n)) for i, c in enumerate(closes)])


def chop(n: int = N) -> pl.DataFrame:
    """Alternating closes with a constant range; ends on the higher close (the lower ATR%)."""
    closes = [100.0 if (n - 1 - i) % 2 else 101.0 for i in range(n)]
    return frame(closes, [0.5] * n)


def test_trends_are_tagged_by_direction():
    assert rg.classify_regime(trend(+1)) == "TREND_UP"
    assert rg.classify_regime(trend(-1)) == "TREND_DOWN"


def test_a_range_bound_series_is_chop():
    assert rg.classify_regime(chop()) == "CHOP"


def test_a_volatility_spike_is_high_vol_even_in_a_trend():
    base = trend(+1)
    closes = base["close"].to_list()
    ranges = [c * 0.003 for c in closes[:-3]] + [closes[-1] * 0.05] * 3  # last three bars: ~17x the usual range
    assert rg.classify_regime(frame(closes, ranges)) == "HIGH_VOL"
    assert rg.classify_regime(frame(chop()["close"].to_list(), [0.5] * (N - 3) + [8.0] * 3)) == "HIGH_VOL"


def test_too_few_bars_give_none_and_order_does_not_matter():
    assert rg.classify_regime(trend(+1, rg.MIN_BARS - 1)) is None
    assert rg.classify_regime(pl.DataFrame({"high": [], "low": [], "close": []})) is None
    assert rg.classify_regime(trend(+1).reverse()) == "TREND_UP"  # sorted by time first
    assert rg.MIN_BARS == rg.ATR_PERIOD + rg.MIN_ATR_SAMPLES


def _reference_adx(df: pl.DataFrame, p: int = 14) -> tuple[float, float, float]:
    """Plain-Python ADX with SMA-seeded Wilder smoothing (the module's documented convention)."""
    h, lo, c = df["high"].to_list(), df["low"].to_list(), df["close"].to_list()
    pdm, mdm, tr = [0.0], [0.0], [0.0]
    for i in range(1, len(c)):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))

    def wilder(xs, seed):
        out = [None] * len(xs)
        out[seed] = sum(xs[seed - p + 1:seed + 1]) / p
        for i in range(seed + 1, len(xs)):
            out[i] = out[i - 1] + (xs[i] - out[i - 1]) / p
        return out

    sp, sm, st = wilder(pdm, p), wilder(mdm, p), wilder(tr, p)
    pdi = [None if s is None else 100 * a / s for a, s in zip(sp, st, strict=True)]
    mdi = [None if s is None else 100 * a / s for a, s in zip(sm, st, strict=True)]
    dx = [0.0 if a is None else (100 * abs(a - b) / (a + b) if a + b > 0 else 0.0) for a, b in zip(pdi, mdi, strict=True)]
    adx = wilder(dx, 2 * p - 1)
    return adx[-1], pdi[-1], mdi[-1]


def test_adx_matches_a_plain_python_reference():
    closes = [100 + 5 * ((i * 7919) % 13 - 6) / 6 + 0.05 * i for i in range(300)]
    df = frame(closes, [0.4 + ((i * 31) % 7) / 10 for i in range(300)])
    got = rg.regime_inputs(df).row(-1, named=True)
    adx, pdi, mdi = _reference_adx(df)
    assert got["adx"] == pytest.approx(adx, rel=1e-9)
    assert got["plus_di"] == pytest.approx(pdi, rel=1e-9) and got["minus_di"] == pytest.approx(mdi, rel=1e-9)


# --- backtest breakdown ----------------------------------------------------------------------------

def test_resample_to_1h_buckets_by_bar_close():
    m = 60 * 10**9
    bars = [(HOUR_NS - m, 1.0, 2.0, 0.5, 1.5), (HOUR_NS, 1.5, 3.0, 1.0, 2.5), (HOUR_NS + m, 2.5, 2.6, 2.4, 2.45)]
    assert resample_ohlc_1h(bars) == [(HOUR_NS, 1.0, 3.0, 0.5, 2.5), (2 * HOUR_NS, 2.5, 2.6, 2.4, 2.45)]


def test_regime_breakdown_keys_trades_by_the_regime_at_entry():
    up = trend(+1)
    ohlc = [(ts, c, h, lo, c) for ts, h, lo, c in up.select("ts_event_ns", "high", "low", "close").iter_rows()]
    end = up["ts_event_ns"][-1]
    trades = [{"ts_opened_ns": end + 60 * 10**9, "pnl": 10.0}, {"ts_opened_ns": end + 120 * 10**9, "pnl": -4.0},
              {"ts_opened_ns": 5 * HOUR_NS, "pnl": 1.0}]  # too early: no regime yet
    out = regime_breakdown(trades, ohlc)
    assert out["TREND_UP"] == {"trades": 2, "win_rate": 0.5, "total_pnl": 6.0}
    assert out[UNKNOWN_REGIME] == {"trades": 1, "win_rate": 1.0, "total_pnl": 1.0}
    assert regime_breakdown([], []) == {}


def test_regime_breakdown_never_raises():
    assert regime_breakdown([{"ts_opened_ns": "x"}], [(1, 1.0, 1.0, 1.0, 1.0)]) is None


# --- aggregation -----------------------------------------------------------------------------------

def test_outcomes_by_regime_counts_and_mean_signed_return():
    rows = [("TREND_UP", "BUY", 0.02), ("TREND_UP", "BUY", -0.01), ("TREND_UP", "SELL", -0.03),
            ("CHOP", "HOLD", 0.001), (None, "BUY", 0.001)]
    got = {r["regime"]: r for r in outcomes_by_regime(rows)}
    assert list(got) == ["TREND_UP", "CHOP", "UNKNOWN"]
    assert got["TREND_UP"] == {"regime": "TREND_UP", "n": 3, "wins": 2, "losses": 1, "neutral": 0,
                               "mean_signed_return": pytest.approx((0.02 - 0.01 + 0.03) / 3)}
    assert (got["CHOP"]["wins"], got["CHOP"]["mean_signed_return"]) == (1, pytest.approx(-0.001))
    assert got["UNKNOWN"]["neutral"] == 1


# --- database --------------------------------------------------------------------------------------

def _alembic(url: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)


async def _has_regime(url: str) -> bool:
    conn = await asyncpg.connect(url)
    try:
        return bool(await conn.fetchval("""SELECT 1 FROM information_schema.columns WHERE table_name = 'ai_decision_outcomes'
                                           AND column_name = 'regime'"""))
    finally:
        await conn.close()


def test_migration_034_up_and_down():
    with migrated_database("cryptonite_m034") as url:
        assert asyncio.run(_has_regime(url))
        _alembic(url, "downgrade", "033")
        assert not asyncio.run(_has_regime(url))
        _alembic(url, "upgrade", "head")
        assert asyncio.run(_has_regime(url))


T0 = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)
T0_NS = int(T0.timestamp() * 10**9)
MIN_NS = 60 * 10**9


class TimeframeLake:
    """1m bars after the decision for the labels, 1h bars before it for the regime."""

    def __init__(self, minute: pl.DataFrame, hour: pl.DataFrame):
        self.frames = {"1m": minute, "1h": hour}

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        df = self.frames.get(timeframe, pl.DataFrame())
        return df.filter((pl.col("ts_event_ns") >= (start_ns or 0)) & (pl.col("ts_event_ns") <= (end_ns or 2**63)))


def minute_bars(n: int) -> pl.DataFrame:
    closes = [3000 + 0.05 * (i + 1) for i in range(n)]
    return pl.DataFrame({"ts_event_ns": [T0_NS + (i + 1) * MIN_NS for i in range(n)], "close": closes,
                         "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes]})


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_regimetest") as url:
        yield url


async def _decision(conn, mandate_id=None, rec="BUY", created=T0) -> object:
    run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status, mandate_id) "
                              "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED', $1) RETURNING id", mandate_id)
    return await conn.fetchval(
        "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
        "reasoning, created_at, decision_price) VALUES ($1, 'ETHUSDT.BINANCE', $2, 0.7, 5, 'r', $3, 3000) RETURNING id",
        run, rec, created)


async def test_learner_stores_the_regime_at_decision_time_and_the_backfill_fills_old_rows(db_url):
    hourly = trend(+1)
    hourly = hourly.with_columns((pl.col("ts_event_ns") - hourly["ts_event_ns"][-1] + T0_NS).alias("ts_event_ns"))
    lake = TimeframeLake(minute_bars(2 * 60), hourly)
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM portfolio_decisions")
            dec = await _decision(conn)
        assert await outcomes.label_pending(pool, lake, T0 + datetime.timedelta(hours=2)) == 1  # the 1h horizon
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT regime FROM ai_decision_outcomes WHERE decision_id = $1", dec) == "TREND_UP"
            # a row labelled before migration 034
            await conn.execute("UPDATE ai_decision_outcomes SET regime = NULL")
        no_history = TimeframeLake(minute_bars(2 * 60), hourly.tail(10))
        assert await outcomes.backfill_regimes(pool, no_history) == {"decisions": 1, "tagged": 0, "untagged": 1}
        assert await outcomes.backfill_regimes(pool, lake, dry_run=True) == {"decisions": 1, "tagged": 1, "untagged": 0}
        assert await outcomes.backfill_regimes(pool, lake) == {"decisions": 1, "tagged": 1, "untagged": 0}
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT regime FROM ai_decision_outcomes WHERE decision_id = $1", dec) == "TREND_UP"
        assert await outcomes.backfill_regimes(pool, lake) == {"decisions": 0, "tagged": 0, "untagged": 0}
    finally:
        await pool.close()


async def test_a_missing_regime_never_blocks_the_label(db_url):
    class BrokenHourly(TimeframeLake):
        def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
            if timeframe == "1h":
                raise OSError("lake unavailable")
            return super().query_bars_df(symbol, timeframe, start_ns, end_ns)

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM portfolio_decisions")
            dec = await _decision(conn)
        lake = BrokenHourly(minute_bars(2 * 60), pl.DataFrame())
        assert await outcomes.label_pending(pool, lake, T0 + datetime.timedelta(hours=2)) == 1
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT regime FROM ai_decision_outcomes WHERE decision_id = $1", dec) is None
    finally:
        await pool.close()


async def test_outcomes_by_regime_endpoint(db_url, operator_auth):
    from cryptonite_api.main import app
    from httpx import ASGITransport, AsyncClient

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    app.state.pool = pool
    try:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM portfolio_decisions")
            mandate = await conn.fetchval(
                """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, created_by)
                   VALUES ('regime', 10000, '{ETHUSDT.BINANCE}', 10, 20, 'test') RETURNING id""")
            for mid, rec, ret, regime, horizon in [
                (mandate, "BUY", "0.02", "TREND_UP", "24h"), (mandate, "BUY", "-0.01", "TREND_UP", "24h"),
                (mandate, "SELL", "0.001", "CHOP", "24h"), (None, "BUY", "0.05", "HIGH_VOL", "24h"),
                (mandate, "BUY", "0.03", "TREND_UP", "1h"),
            ]:
                dec = await _decision(conn, mid, rec)
                await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars, regime) "
                                   "VALUES ($1, $2, $3, 0.05, -0.05, 1440, $4)", dec, horizon, Decimal(ret), regime)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            all_ = (await client.get("/api/v1/ai/outcomes/by-regime")).json()
            mine = (await client.get("/api/v1/ai/outcomes/by-regime", params={"mandate_id": str(mandate)})).json()
            hourly = (await client.get("/api/v1/ai/outcomes/by-regime", params={"horizon": "1h"})).json()
            bad = await client.get("/api/v1/ai/outcomes/by-regime", params={"horizon": "2h"})
        assert all_["available"] is True and all_["horizon"] == "24h"
        assert [r["regime"] for r in all_["regimes"]] == ["TREND_UP", "CHOP", "HIGH_VOL"]
        by = {r["regime"]: r for r in mine["regimes"]}
        assert set(by) == {"TREND_UP", "CHOP"}
        assert (by["TREND_UP"]["n"], by["TREND_UP"]["wins"], by["TREND_UP"]["losses"]) == (2, 1, 1)
        assert by["TREND_UP"]["mean_signed_return"] == pytest.approx(0.005)
        assert by["CHOP"]["neutral"] == 1
        assert [(r["regime"], r["n"]) for r in hourly["regimes"]] == [("TREND_UP", 1)]
        assert bad.status_code == 422
    finally:
        await pool.close()
