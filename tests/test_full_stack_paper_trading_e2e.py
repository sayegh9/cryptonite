"""Full-stack end-to-end paper trading integration test.

Verifies:
1. Real PostgreSQL database tables & foreign keys for Paper Trading (migration 005).
2. Real Redis streams for ingress telemetry, wire bus, and outbox relay.
3. Nautilus 2.0.0rc4 version lock assertion.
4. Lossless watermark handoff protocol and deduplicating bar router.
5. Synchronous GuardedOrderDispatcher with spot cash invariant and reservation.
6. Asynchronous cross-thread EventBridge micro-batch commit into PostgreSQL.
7. Outbox event generation and OutboxRelay draining to Redis stream.
8. Execution command poller with lease fencing (SKIP LOCKED) and node mailbox.
9. Emergency latched kill switch trip and audited net-zero reset lifecycle.
"""

import asyncio
import json
import os
import socket
import time
import uuid
from decimal import Decimal

import asyncpg
import pytest
import redis.asyncio as aioredis
from cryptonite_execution.bridge import EventBridge, EventBridgeWorker
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.risk import OrderIntent
from cryptonite_execution.version_lock import assert_version_locked
from cryptonite_outbox.relay import OutboxRelay
from cryptonite_schemas.identifiers import to_nautilus_bar_type, to_nautilus_instrument_id, to_spot_instrument_id
from nautilus_trader.model import Bar, BarType, Price, Quantity, QuoteTick
from connections import DB_URL, REDIS_URL



@pytest.mark.asyncio
async def test_full_stack_paper_trading_e2e():
    # 0. Enforce Nautilus version lock
    assert_version_locked()

    pool = await asyncpg.create_pool(dsn=DB_URL, min_size=2, max_size=5)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)

    test_id = uuid.uuid4().hex[:8].upper()
    account_id = f"ACC-E2E-{test_id}"
    instance_id = f"INST-E2E-{test_id}"
    engine_run_id = f"RUN-E2E-{test_id}"
    symbol = "BTCUSDT"
    instrument_id_str = "BTCUSDT.BINANCE"
    bar_type_str = "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL"
    nautilus_inst_id = to_nautilus_instrument_id(to_spot_instrument_id(symbol))
    nautilus_bar_type = to_nautilus_bar_type(instrument_id_str, "1m")

    # 1. Database Cleanup & Fixture Seeding
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Seed account
            await conn.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, venue, currency, starting_balance, cash_balance,
                    total_equity, realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at
                ) VALUES ($1, 'BINANCE', 'USDT', 20000.00, 20000.00, 20000.00, 0, 0, FALSE, now())
                ON CONFLICT (account_id) DO UPDATE
                SET cash_balance = 20000.00, total_equity = 20000.00;
                """,
                account_id,
            )

            # Ensure default strategy registered
            strat_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
            await conn.execute(
                """
                INSERT INTO strategies (id, slug, name, description, author, created_at, updated_at)
                VALUES ($1, 'ema-e2e', 'EMA Cross', 'E2E Strategy', 'Cryptonite', now(), now())
                ON CONFLICT (id) DO NOTHING;
                """,
                strat_id,
            )
            await conn.execute(
                """
                INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema, created_at)
                VALUES ($1, $1, 1, 'hash-e2e', 'commit-e2e', '{"fast": 9, "slow": 21}'::jsonb, '{}'::jsonb, now())
                ON CONFLICT (id) DO NOTHING;
                """,
                strat_id,
            )

            # Seed strategy instance
            await conn.execute(
                """
                INSERT INTO paper_strategy_instances (
                    instance_id, account_id, strategy_version_id, strategy_source_hash,
                    risk_policy_hash, strategy_name, instrument_id, timeframe,
                    resolved_parameters, allocation_usd, status, created_at, updated_at
                ) VALUES ($1, $2, $3, 'src-hash-e2e', 'risk-hash-e2e', 'EMACrossover', $4, '1m', '{"fast": 9, "slow": 21}'::jsonb, 5000.00, 'RUNNING', now(), now());
                """,
                instance_id,
                account_id,
                strat_id,
                instrument_id_str,
            )

            # Ensure kill switch row exists and is unlatched
            await conn.execute(
                """
                INSERT INTO execution_kill_switch (id, is_latched, updated_at)
                VALUES (1, FALSE, now())
                ON CONFLICT (id) DO UPDATE SET is_latched = FALSE;
                """
            )

    # 2. Redis Telemetry Ingress Setup
    telemetry_stream = "cryptonite:stream:ingress_health"
    await r_client.delete(telemetry_stream)
    await r_client.delete("cryptonite:stream:paper_trades")
    now_ns = time.time_ns()
    session_id = str(uuid.uuid4())

    # Publish fresh telemetry pulse
    await r_client.xadd(
        telemetry_stream,
        {
            b"session_id": session_id.encode(),
            b"publish_sequence": b"1",
            b"timestamp_ns": str(now_ns).encode(),
            b"instrument_id": instrument_id_str.encode(),
        },
    )

    # 3. Initialize PaperTradingEngine
    engine = PaperTradingEngine(
        account_id=account_id,
        trader_id=f"TRADER-{test_id}",
        engine_run_id=engine_run_id,
        pg_pool=pool,
        redis_client=r_client,
        starting_cash=Decimal("20000.00"),  # 0.05 BTC buy = 12.8% of equity, under the 25% concentration limit
    )

    class MockCache:
        def __init__(self, quote):
            self._quote = quote

        def quote(self, inst):
            return self._quote

    # 4. Mock Quote in cache for Dual Freshness verification
    fresh_quote = QuoteTick(
        instrument_id=nautilus_inst_id,
        bid_price=Price(Decimal("50000.00"), 2),
        ask_price=Price(Decimal("50010.00"), 2),
        bid_size=Quantity(Decimal("1.50000"), 5),
        ask_size=Quantity(Decimal("2.00000"), 5),
        ts_event=now_ns,
        ts_init=now_ns,
    )
    mock_cache = MockCache(fresh_quote)
    engine.dispatcher.set_node_cache(mock_cache)

    # Ingest health telemetry into engine's monitor
    engine.ingress_health.update_health(
        instrument_id=instrument_id_str,
        session_id=session_id,
        seq=1,
        event_ts_ns=now_ns,
        arrival_ts_ns=now_ns,
    )
    assert engine.ingress_health.is_fresh(instrument_id_str, mock_cache, now_ns=now_ns) is True

    # 5. Test Lossless Watermark Handoff Protocol
    catalog_watermark_c = 1725700060000000000
    # Create 2 historical bars <= C and 1 buffered bar > C
    hist_bar1 = Bar(
        bar_type=nautilus_bar_type,
        open=Price(Decimal("49900.00"), 2),
        high=Price(Decimal("50050.00"), 2),
        low=Price(Decimal("49850.00"), 2),
        close=Price(Decimal("50000.00"), 2),
        volume=Quantity(Decimal("10.0"), 5),
        ts_event=1725700000000000000,
        ts_init=1725700000000000000,
    )
    hist_bar2 = Bar(
        bar_type=nautilus_bar_type,
        open=Price(Decimal("50000.00"), 2),
        high=Price(Decimal("50100.00"), 2),
        low=Price(Decimal("49950.00"), 2),
        close=Price(Decimal("50050.00"), 2),
        volume=Quantity(Decimal("15.0"), 5),
        ts_event=catalog_watermark_c,
        ts_init=catalog_watermark_c,
    )
    buffered_live_bar = Bar(
        bar_type=nautilus_bar_type,
        open=Price(Decimal("50050.00"), 2),
        high=Price(Decimal("50200.00"), 2),
        low=Price(Decimal("50010.00"), 2),
        close=Price(Decimal("50150.00"), 2),
        volume=Quantity(Decimal("8.0"), 5),
        ts_event=1725700120000000000,  # > C
        ts_init=1725700120000000000,
    )

    # Buffer the live bar before handoff
    engine.on_live_bar(buffered_live_bar)
    assert len(engine._buffered_bars) == 1

    # Execute 8-step handoff protocol
    handoff_ok = await engine.execute_startup_handoff(
        symbol=symbol,
        bar_type_str=bar_type_str,
        catalog_watermark_c_ns=catalog_watermark_c,
        historical_warmup_bars=[hist_bar1, hist_bar2],
    )
    assert handoff_ok is True
    assert engine.is_gate_open is True
    assert engine.router.max_processed_bar_ns == 1725700120000000000
    engine._running = True
    assert engine.is_ready is True

    # 6. Synchronous Guarded Order Dispatcher
    # Buy 0.05 BTC at market (ask = 50,010.00 -> notional = 2,500.50 USD)
    client_order_id = f"ORD-E2E-{test_id}-001"
    intent = OrderIntent(
        client_order_id=client_order_id,
        instance_id=instance_id,
        instrument_id=instrument_id_str,
        side="BUY",
        order_type="MARKET",
        quantity=Decimal("0.05"),
        price=None,
    )

    # Pre-submission synchronous risk check and dispatch
    submitted_orders = []
    ok, reason = engine.dispatcher.dispatch(
        intent=intent,
        submit_fn=lambda o: submitted_orders.append(o),
        nautilus_order={"order_id": client_order_id},
    )
    assert ok is True
    assert reason == "SUBMITTED"
    assert len(submitted_orders) == 1
    # Verify cash reservation in memory
    assert client_order_id in engine.dispatcher.open_orders
    assert engine.dispatcher.reserved_cash > Decimal("2500.00")


    # Simulate node thread emitting order lifecycle events into EventBridge (ZERO DB I/O)
    evt_submitted = {
        "event_id": uuid.uuid4(),
        "native_event_id": f"evt-sub-{test_id}",
        "client_order_id": client_order_id,
        "event_type": "ORDER_SUBMITTED",
        "event_version": 1,
        "status": "SUBMITTED",
        "account_id": account_id,
        "instance_id": instance_id,
        "instrument_id": instrument_id_str,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": Decimal("0.05"),
        "payload": {"type": "OrderSubmitted"},
        "occurred_at_ns": time.time_ns(),
    }
    evt_accepted = {
        "event_id": uuid.uuid4(),
        "native_event_id": f"evt-acc-{test_id}",
        "client_order_id": client_order_id,
        "venue_order_id": f"BINANCE-{test_id}-001",
        "event_type": "ORDER_ACCEPTED",
        "event_version": 2,
        "status": "ACCEPTED",
        "account_id": account_id,
        "instance_id": instance_id,
        "instrument_id": instrument_id_str,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": Decimal("0.05"),
        "payload": {"type": "OrderAccepted"},
        "occurred_at_ns": time.time_ns(),
    }
    evt_filled = {
        "event_id": uuid.uuid4(),
        "native_event_id": f"evt-fil-{test_id}",
        "client_order_id": client_order_id,
        "venue_order_id": f"BINANCE-{test_id}-001",
        "event_type": "ORDER_FILLED",
        "event_version": 3,
        "status": "FILLED",
        "account_id": account_id,
        "instance_id": instance_id,
        "instrument_id": instrument_id_str,
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": Decimal("0.05"),
        "price": Decimal("50010.00"),
        "filled_qty": Decimal("0.05"),
        "avg_fill_price": Decimal("50010.00"),
        "commission": Decimal("2.5005"),
        "payload": {"type": "OrderFilled", "fill_price": "50010.00"},
        "occurred_at_ns": time.time_ns(),
    }

    # Initial order record inserted into DB before bridge drains updates
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO paper_orders (
                client_order_id, account_id, instance_id, instrument_id, side, order_type,
                quantity, status, last_event_version, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, 'BUY', 'MARKET', 0.05, 'PENDING', 0, now(), now());
            """,
            client_order_id,
            account_id,
            instance_id,
            instrument_id_str,
        )

    # Enqueue events into bridge
    assert engine.bridge.enqueue_event(evt_submitted) is True
    assert engine.bridge.enqueue_event(evt_accepted) is True
    assert engine.bridge.enqueue_event(evt_filled) is True

    # Dispatcher processes fill synchronously in node thread
    engine.dispatcher.handle_fill(
        client_order_id=client_order_id,
        fill_qty=Decimal("0.05"),
        fill_price=Decimal("50010.00"),
        commission=Decimal("2.5005"),
    )
    # Verify reservation released and position updated
    assert client_order_id not in engine.dispatcher.open_orders
    assert engine.dispatcher.get_position_qty(instrument_id_str) == Decimal("0.05")
    expected_cash = Decimal("20000.00") - (Decimal("0.05") * Decimal("50010.00")) - Decimal("2.5005")
    assert engine.dispatcher.cash_balance == expected_cash

    # 7. Asynchronous EventBridgeWorker drains and commits to PostgreSQL
    worker = EventBridgeWorker(engine.bridge, pool, engine_run_id, batch_size=10)
    committed = await worker.drain_and_commit_batch()
    assert committed == 3

    # Verify Database state
    async with pool.acquire() as conn:
        # Check paper_execution_events
        evts = await conn.fetch(
            "SELECT event_type, event_version FROM paper_execution_events WHERE client_order_id = $1 ORDER BY event_version;",
            client_order_id,
        )
        assert len(evts) == 3
        assert [e["event_type"] for e in evts] == ["ORDER_SUBMITTED", "ORDER_ACCEPTED", "ORDER_FILLED"]

        # Check paper_orders projection updated
        order_row = await conn.fetchrow("SELECT * FROM paper_orders WHERE client_order_id = $1;", client_order_id)
        assert order_row["status"] == "FILLED"
        assert order_row["filled_qty"] == Decimal("0.05000000")
        assert order_row["avg_fill_price"] == Decimal("50010.00")
        assert order_row["last_event_version"] == 3

        # Check paper_trades recorded
        trade_row = await conn.fetchrow("SELECT * FROM paper_trades WHERE client_order_id = $1;", client_order_id)
        assert trade_row is not None
        assert trade_row["side"] == "BUY"
        assert trade_row["quantity"] == Decimal("0.05000000")
        assert trade_row["price"] == Decimal("50010.00")

        # Check outbox_events emitted
        outbox_row = await conn.fetchrow(
            "SELECT * FROM outbox_events WHERE stream_name = 'cryptonite:stream:paper_trades' ORDER BY created_at DESC LIMIT 1;"
        )
        assert outbox_row is not None
        payload_data = json.loads(outbox_row["payload"])
        assert payload_data["client_order_id"] == client_order_id

    # 8. Outbox Relay draining to Redis (drain if not already drained by background daemon)
    if outbox_row["status"] == "PENDING":
        relay = OutboxRelay(database_url=DB_URL, redis_url=REDIS_URL, batch_size=10)
        relay.pool = pool
        relay.redis = r_client
        relayed_cnt = await relay.process_batch_once()
        assert relayed_cnt >= 1

    # Verify outbox event in PostgreSQL is now PUBLISHED
    async with pool.acquire() as conn:
        status_check = await conn.fetchval(
            "SELECT status FROM outbox_events WHERE event_id = $1;",
            outbox_row["event_id"],
        )
        assert status_check == "PUBLISHED"

    # Verify event reached Redis stream
    redis_messages = await r_client.xrange("cryptonite:stream:paper_trades", count=10)
    assert len(redis_messages) > 0
    trade_redis_payload = json.loads(redis_messages[-1][1][b"data"].decode())
    assert trade_redis_payload["client_order_id"] == client_order_id

    # 9. Test Execution Command Poller & Mailbox Fencing
    cmd_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO execution_commands (
                command_id, idempotency_key, command_type, target_instance_id,
                requester_id, payload, status, attempt_count, created_at
            ) VALUES ($1, $2, 'CLOSE_POSITION', $3, 'web-ui', $4::jsonb, 'PENDING', 0, now());
            """,
            cmd_id,
            f"CLOSE:{cmd_id}",
            instance_id,
            json.dumps({"instrument_id": instrument_id_str, "price": "52000.00"}),
        )

    # Run poller cycle with lease fencing
    lease_owner = f"{socket.gethostname()}-{os.getpid()}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            query = """
            WITH claimed AS (
                SELECT command_id
                FROM execution_commands
                WHERE status = 'PENDING' AND command_id = $1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE execution_commands
            SET status = 'RUNNING',
                lease_owner = $2,
                lease_expires_at = now() + INTERVAL '30 seconds',
                attempt_id = gen_random_uuid(),
                attempt_count = attempt_count + 1
            FROM claimed
            WHERE execution_commands.command_id = claimed.command_id
            RETURNING execution_commands.command_id, execution_commands.command_type,
                      execution_commands.target_instance_id, execution_commands.payload,
                      execution_commands.attempt_id;
            """
            row = await conn.fetchrow(query, cmd_id, lease_owner)
            assert row is not None
            pushed = engine.mailbox.push(dict(row))
            assert pushed is True
            assert engine.mailbox.qsize == 1

            # Mark completed in DB
            await conn.execute(
                """
                UPDATE execution_commands
                SET status = 'COMPLETED', processed_at = now(), lease_owner = NULL, lease_expires_at = NULL
                WHERE command_id = $1 AND attempt_id = $2;
                """,
                row["command_id"],
                row["attempt_id"],
            )

    # Process mailbox commands inside node thread
    engine.process_mailbox_commands()

    # Position must now be flattened
    assert engine.dispatcher.get_position_qty(instrument_id_str) == Decimal("0.00")

    # 10. Emergency Kill Switch Trip & Audited Reset
    assert engine.kill_switch.is_latched is False
    assert engine.is_ready is True

    # Trip kill switch
    engine.kill_switch.trip(latched_by="operator-risk", reason="Manual emergency drill")
    assert engine.kill_switch.is_latched is True
    assert engine.is_ready is False  # /ready endpoint trips 503

    # New order intent must now be rejected
    approved, reject_reason, _ = engine.dispatcher.evaluate_intent(intent)
    assert approved is False
    assert "KILL_SWITCH" in reject_reason

    # Audited reset requires net-zero open exposure
    reset_ok = engine.kill_switch.reset(
        reset_by="auditor-admin",
        open_orders_count=0,
        open_positions_count=0,
    )
    assert reset_ok is True
    assert engine.kill_switch.is_latched is False
    assert engine.is_ready is True

    # Teardown
    engine.stop()
    await pool.close()
    await r_client.aclose()
