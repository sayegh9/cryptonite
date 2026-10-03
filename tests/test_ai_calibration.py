"""Confidence calibration (blueprint A7.2): reliability bins, ECE, the minimum sample that keeps the L3
gate failing closed, and mandate scoping against a throwaway database at Alembic head."""

import datetime
import uuid
from decimal import Decimal

import asyncpg
import pytest
from cryptonite_execution import calibration
from migrated_db import migrated_database


def test_perfect_calibration_has_zero_ece():
    points = [(0.3, i < 3) for i in range(10)] + [(0.7, i < 7) for i in range(10)] + [(0.9, i < 9) for i in range(10)]
    rel = calibration.reliability(points)
    assert rel["ece_pp"] == 0.0 and rel["n"] == 30
    assert [b["n"] for b in rel["bins"]] == [0, 10, 0, 10, 10]


def test_overconfidence_shows_as_ece():
    points = [(0.9, i < 10) for i in range(20)]  # says 90%, right half the time
    assert calibration.reliability(points)["ece_pp"] == pytest.approx(40.0)


def test_too_few_samples_give_no_number():
    rel = calibration.reliability([(0.6, True)] * (calibration.MIN_SAMPLES - 1))
    assert rel["ece_pp"] is None and rel["min_samples"] == calibration.MIN_SAMPLES


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_calibtest") as url:
        yield url


async def test_mandate_calibration_counts_only_its_own_labelled_decisions(db_url):
    conn = await asyncpg.connect(db_url)
    try:
        mandate = await conn.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, created_by)
               VALUES ('calib', 10000, '{ETHUSDT.BINANCE}', 10, 20, 'test') RETURNING id""")
        since = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)

        async def decision(mandate_id, conviction, rec, ret, horizon="24h"):
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, mandate_id) "
                                      "VALUES ('ETHUSDT.BINANCE', 'test', $1) RETURNING id", mandate_id)
            dec = await conn.fetchval(
                "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
                "created_at, decision_price) VALUES ($1, 'ETHUSDT.BINANCE', $2, $3, 5, $4, 3000) RETURNING id",
                run, rec, conviction, since + datetime.timedelta(days=1))
            await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                               "VALUES ($1, $2, $3, 0.05, -0.05, 1440)", dec, horizon, ret)

        for i in range(20):  # this mandate: 0.8 conviction BUYs, right 16 of 20 times -> ECE 0
            await decision(mandate, Decimal("0.80"), "BUY", Decimal("0.02") if i < 16 else Decimal("-0.02"))
        for _ in range(10):  # another mandate's decisions and a 1h-only label must not count
            await decision(None, Decimal("0.90"), "BUY", Decimal("-0.03"))
        await decision(mandate, Decimal("0.50"), "BUY", Decimal("0.01"), horizon="1h")

        assert len(await calibration.verdict_points(conn, mandate, since)) == 20
        assert await calibration.calibration_error_pp(conn, mandate, since) == Decimal("0.0")
        assert await calibration.calibration_error_pp(conn, uuid.uuid4(), since) is None  # no decisions: gate stays closed
    finally:
        await conn.close()
