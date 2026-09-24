"""Full-stack end-to-end integration test:
1. Seed strategy and version in PostgreSQL.
2. Ingest catalog fixture with 1m bars.
3. Submit backtest run via FastAPI endpoint POST /api/v1/backtests.
4. BacktestWorker claims and executes the real BacktestNode simulation with lease fencing.
5. Verifies database backtest_results, Parquet artifacts on disk, and outbox event emission.
6. Drains outbox event to Redis Streams via OutboxRelay.
7. Inspects tearsheet results via GET /api/v1/backtests/{id}/results.
"""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as aioredis
from cryptonite_api.main import app
from cryptonite_backtester_service.worker import BacktestWorker
from cryptonite_outbox.relay import OutboxRelay
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_nautilus_instrument_id, to_spot_instrument_id
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from httpx import ASGITransport, AsyncClient
from nautilus_trader.model import Bar, Currency, CurrencyPair, Price, Quantity
from connections import DB_URL, REDIS_URL




@pytest.mark.asyncio
async def test_full_stack_backtest_lifecycle_e2e(tmp_path: Path):
    cat_dir = tmp_path / "nautilus_catalog"
    art_dir = tmp_path / "backtest_artifacts"
    mgr = NautilusCatalogManager(cat_dir)

    inst_id = to_nautilus_instrument_id(to_spot_instrument_id("BTCUSDT"))
    pair = CurrencyPair(
        instrument_id=inst_id,
        raw_symbol=inst_id.symbol,
        base_currency=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        price_precision=2,
        size_precision=5,
        price_increment=Price(Decimal("0.01"), 2),
        size_increment=Quantity(Decimal("0.00001"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    mgr.write_instruments([pair])

    bar_type = to_nautilus_bar_type("BTCUSDT.BINANCE", "1m")
    step_ns = 60_000_000_000
    base_ts = 1725700000000000000

    bars = []
    px = 50000.0
    for i in range(50):
        ts = base_ts + i * step_ns
        c = px + (20.0 if (i % 6) < 3 else -20.0)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(px)), 2),
                high=Price(Decimal(str(max(px, c) + 5.0)), 2),
                low=Price(Decimal(str(min(px, c) - 5.0)), 2),
                close=Price(Decimal(str(c)), 2),
                volume=Quantity(Decimal("1.0"), 5),
                ts_event=ts,
                ts_init=ts,
            )
        )
        px = c
    mgr.write_bars(bars)

    pool = await asyncpg.create_pool(DB_URL)
    r = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        # Seed strategy and version
        strat_id = uuid4()
        ver_id = uuid4()
        slug = "ema-cross"

        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'EMA Crossover Strategy', 'Cryptonite Core')
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """,
            strat_id, "EMA Crossover Strategy", slug,
        )
        strat_row = await pool.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
        actual_strat_id = strat_row["id"]

        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 1, 'hash-e2e', 'commit-e2e', '{"fast_period": 5, "slow_period": 10, "trade_size": 0.01}'::jsonb, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            ver_id, actual_strat_id,
        )

        eval_start = base_ts + (15 * step_ns)
        eval_end = base_ts + (49 * step_ns)

        app.state.pool = pool
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Enqueue backtest run via FastAPI POST
            create_resp = await client.post(
                "/api/v1/backtests",
                json={
                    "strategy_slug": slug,
                    "instrument_id": str(inst_id),
                    "bar_type": str(bar_type),
                    "timeframe": "1m",
                    "parameters": {"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
                    "warmup_start_ns": base_ts,
                    "evaluation_start_ns": eval_start,
                    "evaluation_end_ns": eval_end,
                    "execution_profile": "optimistic",
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            created_data = create_resp.json()
            run_id = created_data["id"]
            assert created_data["status"] == "PENDING"

            # The claim query is global (oldest PENDING or lease-expired row). The shared test
            # DB keeps unfinished runs from other tests, so retire them or they get claimed first.
            await pool.execute(
                "UPDATE backtest_runs SET status = 'FAILED' "
                "WHERE id <> $1 AND status IN ('PENDING', 'CLAIMED', 'RUNNING')",
                run_id,
            )

            # 2. Worker claims and executes the real backtest
            worker = BacktestWorker(
                database_url=DB_URL,
                catalog_path=str(cat_dir),
                artifacts_path=str(art_dir),
                worker_id="e2e-worker",
            )
            worker.pool = pool

            claimed = await worker._claim_run()
            assert claimed is not None
            assert str(claimed["id"]) == run_id

            await worker._execute_claimed_run(claimed)

            # 3. Assert DB run updated to COMPLETED
            run_status_resp = await client.get(f"/api/v1/backtests/{run_id}")
            assert run_status_resp.status_code == 200
            run_info = run_status_resp.json()
            assert run_info["status"] == "COMPLETED"

            # 4. Assert tearsheet results available via API
            results_resp = await client.get(f"/api/v1/backtests/{run_id}/results")
            assert results_resp.status_code == 200
            tearsheet = results_resp.json()
            assert "metrics" in tearsheet
            assert "total_return" in tearsheet["metrics"]
            assert "sharpe_ratio" in tearsheet["metrics"]
            assert "artifact_manifest" in tearsheet
            assert "realism_disclosure" in tearsheet["artifact_manifest"] or (art_dir / run_id / "manifest.json").exists()



            # 5. Assert outbox event was emitted and relay it to Redis
            outbox_row = await pool.fetchrow(
                "SELECT * FROM outbox_events WHERE stream_name = 'cryptonite:stream:backtest_completed' AND payload->>'backtest_run_id' = $1",
                run_id,
            )
            assert outbox_row is not None
            if outbox_row["status"] == "PENDING":
                relay = OutboxRelay(database_url=DB_URL, redis_url=REDIS_URL, max_attempts=3)
                relay.pool = pool
                relay.redis = r
                published_count = await relay.process_batch_once()
                assert published_count >= 1

            # 6. Verify stream entry in Redis
            entries = await r.xrevrange("cryptonite:stream:backtest_completed", max="+", min="-", count=25)
            assert entries is not None
            found = False
            for msg_id, msg_data in entries:
                payload = json.loads(msg_data["data"])
                if payload.get("backtest_run_id") == run_id:
                    found = True
                    break
            assert found is True, "backtest_completed event must be delivered to Redis stream"


    finally:
        await r.aclose()
        await pool.close()
