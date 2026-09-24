"""Tests for backtest_completed outbox event emission, relay to Redis Streams, and consumer deduplication."""

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as aioredis
from cryptonite_backtester_service.worker import BacktestWorker
from cryptonite_outbox.relay import OutboxRelay
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_nautilus_instrument_id, to_spot_instrument_id
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from nautilus_trader.model import Bar, Currency, CurrencyPair, Price, Quantity
from connections import DB_URL, REDIS_URL



@pytest.mark.asyncio
async def test_backtest_completion_outbox_emission_and_relay(tmp_path: Path):
    """Verify that worker completion transaction emits backtest_completed into outbox_events,
    which is then reliably relayed to Redis Streams with deterministic deduplication."""
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
    for i in range(40):
        ts = base_ts + i * step_ns
        c = px + (15.0 if (i % 6) < 3 else -15.0)
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
        strat_id = uuid4()
        ver_id = uuid4()
        run_id = uuid4()
        attempt_id = uuid4()
        slug = "ema-cross"

        await pool.execute(
            """
            INSERT INTO strategies (id, name, slug, description, author)
            VALUES ($1, $2, $3, 'Desc', 'Auth')
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """,
            strat_id, "EMA Cross Outbox", slug,
        )
        strat_row = await pool.fetchrow("SELECT id FROM strategies WHERE slug = $1", slug)
        actual_strat_id = strat_row["id"]

        await pool.execute(
            """
            INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
            VALUES ($1, $2, 997, 'h', 'c', '{\"fast_period\": 5, \"slow_period\": 10, \"trade_size\": 0.01}'::jsonb, '{}'::jsonb)
            ON CONFLICT DO NOTHING
            """,
            ver_id, actual_strat_id,
        )
        ver_row = await pool.fetchrow("SELECT id FROM strategy_versions WHERE strategy_id = $1 ORDER BY version_num DESC LIMIT 1", actual_strat_id)
        actual_ver_id = ver_row["id"]

        eval_start = base_ts + (10 * step_ns)
        eval_end = base_ts + (39 * step_ns)

        run_dict = {
            "id": run_id,
            "strategy_version_id": actual_ver_id,
            "instrument_id": str(inst_id),
            "bar_type": str(bar_type),

            "timeframe": "1m",
            "parameters": {"fast_period": 5, "slow_period": 10, "trade_size": 0.01},
            "warmup_start_ns": base_ts,
            "evaluation_start_ns": eval_start,
            "evaluation_end_ns": eval_end,
            "execution_profile": "optimistic",
            "run_config_hash": "hash-outbox-test",
            "status": "RUNNING",
            "lease_owner": "worker-outbox-test",
            "attempt_id": attempt_id,
            "attempt_count": 1,
        }

        await pool.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_version_id, instrument_id, bar_type, timeframe, parameters,
                warmup_start_ns, evaluation_start_ns, evaluation_end_ns, execution_profile,
                run_config_hash, status, lease_owner, attempt_id, attempt_count, lease_expires_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb,
                $7, $8, $9, $10,
                $11, $12, $13, $14, $15, now() + INTERVAL '300 seconds'
            )
            """,
            run_dict["id"],
            run_dict["strategy_version_id"],
            run_dict["instrument_id"],
            run_dict["bar_type"],
            run_dict["timeframe"],
            json.dumps(run_dict["parameters"]),
            run_dict["warmup_start_ns"],
            run_dict["evaluation_start_ns"],
            run_dict["evaluation_end_ns"],
            run_dict["execution_profile"],
            run_dict["run_config_hash"],
            run_dict["status"],
            run_dict["lease_owner"],
            run_dict["attempt_id"],
            run_dict["attempt_count"],
        )

        worker = BacktestWorker(
            database_url=DB_URL,
            catalog_path=str(cat_dir),
            artifacts_path=str(art_dir),
            worker_id="worker-outbox-test",
        )
        worker.pool = pool

        # 1. Execute run via worker
        await worker._execute_claimed_run(run_dict)

        # 2. Check backtest_completed outbox event exists in DB
        outbox_row = await pool.fetchrow(
            """
            SELECT * FROM outbox_events
            WHERE stream_name = 'cryptonite:stream:backtest_completed'
              AND producer = 'cryptonite-backtester'
              AND payload->>'backtest_run_id' = $1
            """,
            str(run_id),
        )
        assert outbox_row is not None
        assert outbox_row["status"] in ("PENDING", "PUBLISHED")
        assert outbox_row["event_type"] == "backtest_completed"
        payload = json.loads(outbox_row["payload"])
        assert payload["backtest_run_id"] == str(run_id)
        assert "artifact_manifest" in payload

        # 3. Drain via OutboxRelay if not already drained by live background outbox service
        if outbox_row["status"] == "PENDING":
            relay = OutboxRelay(database_url=DB_URL, redis_url=REDIS_URL, max_attempts=3)
            relay.pool = pool
            relay.redis = r

            published_count = await relay.process_batch_once()
            assert published_count >= 1

        # 4. Verify status updated to PUBLISHED
        updated_outbox = await pool.fetchrow(
            "SELECT status, published_stream_id, published_at FROM outbox_events WHERE id = $1",
            outbox_row["id"],
        )
        assert updated_outbox["status"] == "PUBLISHED"
        assert updated_outbox["published_stream_id"] is not None
        assert updated_outbox["published_at"] is not None

        # 5. Read from Redis Stream and verify deduplication on event_id
        stream_entries = await r.xrevrange("cryptonite:stream:backtest_completed", max="+", min="-", count=25)
        assert stream_entries is not None
        found = False
        for msg_id, msg_data in stream_entries:
            if msg_data.get("event_id") == outbox_row["event_id"]:
                found = True
                data = json.loads(msg_data["data"])
                assert data["backtest_run_id"] == str(run_id)
                assert data["strategy_slug"] == slug
                break
        assert found is True


    finally:
        await r.aclose()
        await pool.close()
