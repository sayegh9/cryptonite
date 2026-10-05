"""F3 live-vs-backtest report (reporting only): the endpoint with and without running strategy instances,
slippage against limit prices, backtest artifacts, and the daily research digest section. Throwaway
database at Alembic head; Parquet artifacts in a temp dir."""

import datetime
import json
import uuid
from decimal import Decimal

import asyncpg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cryptonite_agents.research.queue import build_digest, live_vs_backtest_lines, publish_digest
from cryptonite_api.live_vs_backtest import adverse_slippage_bps, summarize_trades
from cryptonite_api.main import app
from httpx import ASGITransport, AsyncClient
from migrated_db import migrated_database, seed_paper_account

ACCOUNT = "PAPER-ACCOUNT-001"
DEPLOYED = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


def test_slippage_is_signed_against_the_order():
    assert adverse_slippage_bps("BUY", 100.0, 100.1) == pytest.approx(10.0)  # paid more than the limit: worse
    assert adverse_slippage_bps("SELL", 100.0, 100.1) == pytest.approx(-10.0)  # sold above the limit: better
    assert adverse_slippage_bps("BUY", 0.0, 100.0) is None


def test_summarize_trades():
    assert summarize_trades([0.02, -0.01], [20.0, -10.0]) == {"trades": 2, "win_rate": 0.5, "mean_return_per_trade": 0.005}
    assert summarize_trades([], []) == {"trades": 0, "win_rate": None, "mean_return_per_trade": None}


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_lvbtest") as url:
        yield url


async def _client_get(pool, path):
    app.state.pool = pool
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


async def test_no_instances_gives_an_empty_list(db_url, operator_auth):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)  # only SYSTEM instances: not strategy instances
        resp = await _client_get(pool, "/api/v1/research/live-vs-backtest")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True and body["instances"] == [] and body["without_backtest"] == []
    finally:
        await pool.close()


def _write_artifacts(base, run_id):
    run_dir = base / str(run_id)
    run_dir.mkdir(parents=True)
    pq.write_table(pa.table({"position_id": ["p1", "p2"], "realized_return": [0.03, -0.01], "realized_pnl": [30.0, -10.0]}),
                   run_dir / "trades.parquet")
    pq.write_table(pa.table({"side": ["BUY", "SELL", "BUY"], "price": [100.0, 0.0, 200.0], "avg_px": [100.05, 101.0, 200.0],
                             "filled_qty": [1.0, 1.0, 0.0]}), run_dir / "orders.parquet")


async def test_a_running_instance_is_compared_with_its_latest_backtest(db_url, operator_auth, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKTEST_ARTIFACTS_PATH", str(tmp_path))
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)
            strat, version, other_version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute("INSERT INTO strategies (id, slug, name) VALUES ($1, $2, $2)", strat, f"lvb-{strat.hex[:6]}")
            for vid, num in ((version, 1), (other_version, 2)):
                await conn.execute("INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit) "
                                   "VALUES ($1, $2, $3, 'h', 'c')", vid, strat, num)
            for iid, vid, instrument in (("LVB-1", version, "BTCUSDT.BINANCE"), ("LVB-2", other_version, "ETHUSDT.BINANCE")):
                await conn.execute(
                    """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_name, strategy_version_id,
                           strategy_source_hash, risk_policy_hash, instrument_id, timeframe, resolved_parameters,
                           allocation_usd, status, created_at)
                       VALUES ($1, $2, 'EmaCross', $3, 'h', 'h', $4, '1m', '{}'::jsonb, 1000, 'RUNNING', $5)""",
                    iid, ACCOUNT, vid, instrument, DEPLOYED)

            async def backtest(completed_days: int, instrument: str, win_rate: str, fees: float):
                run = await conn.fetchval(
                    """INSERT INTO backtest_runs (strategy_version_id, instrument_id, bar_type, timeframe, warmup_start_ns,
                           evaluation_start_ns, evaluation_end_ns, execution_profile, run_config_hash, status, completed_at)
                       VALUES ($1, $2, 'b', '1m', 0, 1, 2, 'conservative', 'x', 'COMPLETED', $3) RETURNING id""",
                    version, instrument, DEPLOYED - datetime.timedelta(days=completed_days))
                await conn.execute(
                    """INSERT INTO backtest_results (backtest_run_id, total_return, max_drawdown, win_rate, total_trades,
                           winning_trades, losing_trades, avg_trade_pnl, max_consecutive_losses, downsampled_equity,
                           monthly_returns, catalog_manifest, artifact_manifest)
                       VALUES ($1, 0.1, 0.05, $2, 40, 22, 18, 5, 3, '[]', '{}', '[]', $3::jsonb)""",
                    run, Decimal(win_rate), json.dumps({"metrics": {"metrics_version": 2, "fees_paid": fees}}))
                return run

            latest = await backtest(2, "BTCUSDT.BINANCE", "0.55", 12.5)
            await backtest(1, "ETHUSDT.BINANCE", "0.9", 1.0)  # newer, but another instrument: ranked after
            await backtest(9, "BTCUSDT.BINANCE", "0.1", 99.0)  # older
            _write_artifacts(tmp_path, latest)

            # live: one winning and one losing round trip since deploy, one before it (ignored)
            async def round_trip(n: int, entry: str, exit_: str, pnl: str, at: datetime.datetime, limit: str | None):
                buy, sell = f"o-{n}-b", f"o-{n}-s"
                for oid, side, px in ((buy, "BUY", entry), (sell, "SELL", exit_)):
                    await conn.execute(
                        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                               quantity, price, filled_qty, avg_fill_price, status, created_at)
                           VALUES ($1, 'LVB-1', $2, 'BTCUSDT.BINANCE', $3, $4, 2, $5, 2, $6, 'FILLED', $7)""",
                        oid, ACCOUNT, side, "LIMIT" if limit and side == "BUY" else "MARKET",
                        Decimal(limit) if limit and side == "BUY" else None, Decimal(px), at)
                    await conn.execute(
                        """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id, side,
                               price, quantity, commission, executed_at)
                           VALUES ($1, $2, $3, 'LVB-1', 'BTCUSDT.BINANCE', $4, $5, 2, 0.5, $6)""",
                        f"t-{oid}", ACCOUNT, oid, side, Decimal(px), at)
                await conn.execute(
                    """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
                           quantity, current_price, realized_pnl, status, opened_at, closed_at)
                       VALUES ($1, $2, 'LVB-1', 'BTCUSDT.BINANCE', 'FLAT', $3, 0, $4, $5, 'CLOSED', $6, $6)""",
                    f"pos-{n}", ACCOUNT, Decimal(entry), Decimal(exit_), Decimal(pnl), at)

            await round_trip(1, "100", "102", "3", DEPLOYED + datetime.timedelta(days=1), limit="99.9")
            await round_trip(2, "100", "99", "-3", DEPLOYED + datetime.timedelta(days=2), limit=None)
            await round_trip(3, "100", "150", "99", DEPLOYED - datetime.timedelta(days=1), limit=None)

        body = (await _client_get(pool, "/api/v1/research/live-vs-backtest")).json()
        assert body["available"] is True
        assert [w["instance_id"] for w in body["without_backtest"]] == ["LVB-2"]
        [row] = body["instances"]
        assert row["instance_id"] == "LVB-1" and row["deployed_at"].startswith("2026-09-01")
        live, bt = row["live"], row["backtest"]
        assert (live["trades"], live["win_rate"]) == (2, 0.5)
        assert live["mean_return_per_trade"] == pytest.approx(0.0)  # +3 and -3 on 200 notional each
        assert live["fees"] == pytest.approx(2.0)  # 4 fills since deploy x 0.5
        assert live["slippage_bps"] == pytest.approx(10.01, abs=0.01) and live["slippage_orders"] == 1
        assert bt["run_id"] == str(latest) and bt["instrument_id"] == "BTCUSDT.BINANCE"
        assert (bt["trades"], bt["win_rate"], bt["fees"]) == (40, 0.55, 12.5)
        assert bt["mean_return_per_trade"] == pytest.approx(0.01)
        assert bt["slippage_bps"] == pytest.approx(5.0) and bt["slippage_orders"] == 1
        assert row["delta"]["win_rate"] == pytest.approx(-0.05)

        lines = live_vs_backtest_lines(body)
        assert lines[0].startswith("Live vs backtest") and "LVB-1 BTCUSDT.BINANCE: trades 2 vs 40" in lines[1]
        assert "win rate 50.0% vs 55.0%" in lines[1]
    finally:
        await pool.close()


def test_digest_section_with_and_without_instances():
    empty = build_digest(hours=24, reports=[], live_vs_backtest={"available": True, "instances": [], "without_backtest": []})
    assert "Live vs backtest: no running strategy instances with a completed backtest." in empty["text"]
    assert build_digest(hours=24, reports=[])["live_vs_backtest"] == ["Live vs backtest: not available."]
    row = {"instance_id": "I-1", "instrument_id": "BTCUSDT.BINANCE",
           "live": {"trades": 3, "win_rate": 0.666, "mean_return_per_trade": 0.004, "fees": 1.5, "slippage_bps": None},
           "backtest": {"trades": 30, "win_rate": 0.6, "mean_return_per_trade": None, "fees": 9.0, "slippage_bps": 2.5}}
    text = build_digest(hours=24, reports=[], live_vs_backtest={"available": True, "instances": [row]})["text"]
    assert "- I-1 BTCUSDT.BINANCE: trades 3 vs 30, win rate 66.6% vs 60.0%, mean return/trade 0.4% vs n/a" in text
    assert "slippage n/a vs 2.5 bps" in text


async def test_publish_digest_survives_a_failing_report():
    class Redis:
        published = []

        async def publish(self, channel, message):
            self.published.append(json.loads(message))

    class Client:
        async def list_reports(self, hours):
            return []

        async def live_vs_backtest(self):
            raise RuntimeError("api down")

    redis = Redis()
    digest = await publish_digest(redis, client=Client())
    assert digest["live_vs_backtest"] == ["Live vs backtest: not available."]
    assert redis.published[0]["type"] == "RESEARCH_DIGEST"
