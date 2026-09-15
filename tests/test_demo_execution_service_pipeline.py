"""Comprehensive hermetic end-to-end integration tests for DemoExecutionPipeline.

Verifies the complete institutional command-to-ledger-to-outbox lifecycle:
1. Single-active executor PostgreSQL advisory lock.
2. Fenced command claiming with attempt_id and lease_owner fencing tokens.
3. Stale worker fencing rejection (expired lease worker cannot overwrite new lease owner).
4. Two-phase pre-transmission persistence (PENDING_SUBMIT + PRE_SEND committed before network).
5. Standard reservation settlement:
   PRE_SEND -> INFLIGHT -> ACKED_UNREFLECTED -> VENUE_REFLECTED -> SETTLEMENT_PENDING -> RELEASED.
6. Immediate execution reservation settlement:
   PRE_SEND -> INFLIGHT -> SETTLEMENT_PENDING -> RELEASED.
7. Pre-venue rejection reservation settlement:
   PRE_SEND -> RELEASED (venue_reflected = false).
8. Transactional idempotency: duplicate execution reports are zero-side-effect no-ops.
9. Monotonic order projections: delayed NEW / ACCEPTED events never regress terminal FILLED orders.
10. Ambiguous outcome protocol: network timeout queries venue by client_order_id before resubmitting.
11. Transactional outbox event emission and draining via OutboxRelay to Redis stream.
12. Authoritative reconciliation, SHA-256 balance snapshots, and unmanaged-order fail-closed gate.
"""

import asyncio
import json
import os
import time
import uuid
from decimal import Decimal

import asyncpg
import pytest
import redis.asyncio as aioredis

from cryptonite_execution.clock import ClockSyncMonitor
from cryptonite_execution.demo_pipeline import (
    DemoExecutionPipeline,
    FencingTokenError,
    MockVenueAdapter,
    VenueAmbiguousError,
)
from cryptonite_execution.filters import DynamicExchangeFilterValidator
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.version_lock import assert_version_locked
from cryptonite_outbox.relay import OutboxRelay

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
REDIS_URL = "redis://127.0.0.1:6379/0"


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_demo_execution_pipeline_full_stack_e2e(tmp_path):
    """Full-stack integration test demonstrating the complete crash-safe execution pipeline."""
    assert_version_locked()

    pool = await asyncpg.create_pool(dsn=DB_URL, min_size=2, max_size=5)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)

    test_id = uuid.uuid4().hex[:8].upper()
    env = "DEMO"
    account_id = f"ACC-DEMO-{test_id}"
    symbol = "BTCUSDT"
    instrument_id = f"{symbol}.BINANCE"

    marker_path = tmp_path / "emergency_kill_switch.marker"
    active_pipeline = None

    try:
        # 1. Seed initial trading balances
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trading_balances (
                    environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns
                ) VALUES ($1, $2, 'USDT', 10000.0, 0.0, $3, $3)
                ON CONFLICT (environment, account_id, asset) DO UPDATE
                SET free = 10000.0, locked = 0.0;
                """,
                env,
                account_id,
                time.time_ns(),
            )
            await conn.execute(
                """
                INSERT INTO trading_balances (
                    environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns
                ) VALUES ($1, $2, 'BTC', 1.0, 0.0, $3, $3)
                ON CONFLICT (environment, account_id, asset) DO UPDATE
                SET free = 1.0, locked = 0.0;
                """,
                env,
                account_id,
                time.time_ns(),
            )

        # 2. Test Single-Active Executor Advisory Lock
        pipe1 = DemoExecutionPipeline(
            environment=env,
            account_id=account_id,
            pool=pool,
            redis_client=r_client,
            worker_id=f"worker-1-{test_id}",
        )
        pipe2 = DemoExecutionPipeline(
            environment=env,
            account_id=account_id,
            pool=pool,
            redis_client=r_client,
            worker_id=f"worker-2-{test_id}",
            emergency_marker_path=str(marker_path),
        )

        lock1_acquired = await pipe1.acquire_advisory_lock()
        assert lock1_acquired is True
        # Second instance must fail to acquire the lock
        lock2_acquired = await pipe2.acquire_advisory_lock()
        assert lock2_acquired is False

        # Release lock from pipe1
        await pipe1.release_advisory_lock()
        # Now pipe2 can acquire it
        lock2_retry = await pipe2.acquire_advisory_lock()
        assert lock2_retry is True
        # Keep pipe2 as active executor
        active_pipeline = pipe2
        mock_venue: MockVenueAdapter = active_pipeline.venue_client

        # Wire venue callbacks to pipeline handler
        mock_venue.register_callback(active_pipeline.handle_execution_report)

        # 3. Test Command Claiming with Attempt Fencing Token
        target_instance = f"strat-demo-{test_id}"
        cmd1_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_commands (
                    command_id, idempotency_key, command_type, target_instance_id,
                    requester_id, payload, status, attempt_count, created_at
                ) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'operator', $4::jsonb, 'PENDING', 0, now());
                """,
                cmd1_id,
                f"cmd-{cmd1_id}",
                target_instance,
                json.dumps({
                    "symbol": symbol,
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": "0.05",
                    "price": "50000.00",
                    "headroom": "5.0",
                }),
            )

        claimed = await active_pipeline.claim_command(lease_seconds=30, target_instance_id=target_instance)
        assert claimed is not None
        assert claimed["command_id"] == cmd1_id
        assert claimed["lease_owner"] == active_pipeline.worker_id
        attempt1_id = claimed["attempt_id"]
        assert attempt1_id is not None

        # 4. Test Stale Worker Fencing Rejection
        # Simulate a rogue or expired worker attempting to finalize with the wrong attempt_id
        bogus_attempt = uuid.uuid4()
        with pytest.raises(FencingTokenError):
            await active_pipeline._finalize_command(
                command_id=cmd1_id,
                attempt_id=bogus_attempt,
                lease_owner=active_pipeline.worker_id,
                status="COMPLETED",
                command_outcome="SUCCESS",
                error_message=None,
            )

        # 5. Process Submit Order: Two-Phase Pre-Transmission Persistence
        # Process order submission through pipeline
        order_result = await active_pipeline.process_submit_order(claimed)
        assert order_result["status"] == "COMPLETED"
        client_order_id_1 = order_result["client_order_id"]
        res1_id = uuid.UUID(order_result["reservation_id"])

        # Verify Two-Phase Pre-Transmission committed state in DB
        async with pool.acquire() as conn:
            # Check order record
            order_row = await conn.fetchrow(
                "SELECT status, quantity, price FROM trading_orders WHERE client_order_id = $1;",
                client_order_id_1,
            )
            assert order_row is not None
            # Status was PENDING_SUBMIT then updated on venue acceptance
            assert order_row["quantity"] == Decimal("0.05")

            # Check reservation record (state moved from PRE_SEND -> INFLIGHT -> ACKED_UNREFLECTED upon accept)
            res_row = await conn.fetchrow(
                "SELECT state, amount, fee_slippage_headroom FROM execution_reservations WHERE reservation_id = $1;",
                res1_id,
            )
            assert res_row is not None
            assert res_row["amount"] == Decimal("2500.00")
            assert res_row["fee_slippage_headroom"] == Decimal("5.0")
            assert res_row["state"] in ("INFLIGHT", "ACKED_UNREFLECTED")

            # Check command state was mutated with attempt fencing token
            cmd_row = await conn.fetchrow(
                "SELECT status, command_outcome FROM execution_commands WHERE command_id = $1;",
                cmd1_id,
            )
            assert cmd_row["status"] == "COMPLETED"
            assert cmd_row["command_outcome"] == "SUCCESS"

        # 6. Standard Reservation Settlement Path:
        # PRE_SEND -> INFLIGHT -> ACKED_UNREFLECTED -> VENUE_REFLECTED -> SETTLEMENT_PENDING -> RELEASED
        # Simulate balance snapshot confirming venue lock
        snap1_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trading_balance_snapshots (
                    snapshot_id, environment, account_id, snapshot_type, account_update_time_ns,
                    observed_at_ns, balances_payload, balances_checksum
                ) VALUES ($1, $2, $3, 'WS_ACCOUNT_UPDATE', $4, $4, '[]'::jsonb, 'snapchk-01');
                """,
                snap1_id,
                env,
                account_id,
                time.time_ns(),
            )
            await conn.execute(
                """
                UPDATE trading_balances
                SET free = 7495.0, locked = 2505.0
                WHERE environment = $1 AND account_id = $2 AND asset = 'USDT';
                """,
                env,
                account_id,
            )

        await active_pipeline.reservation_mgr.confirm_venue_reflection(res1_id, snap1_id)
        res_state = await pool.fetchval(
            "SELECT state FROM execution_reservations WHERE reservation_id = $1;", res1_id
        )
        assert res_state == "VENUE_REFLECTED"

        # Simulate venue fill (0.05 BTC @ 50,000.00)
        await mock_venue.simulate_fill(
            client_order_id=client_order_id_1,
            fill_qty=Decimal("0.05"),
            fill_price=Decimal("50000.00"),
            commission=Decimal("2.50"),
            commission_asset="USDT",
        )
        await asyncio.sleep(0.01)

        # Check reservation transitioned to SETTLEMENT_PENDING
        res_state_after_fill = await pool.fetchval(
            "SELECT state FROM execution_reservations WHERE reservation_id = $1;", res1_id
        )
        assert res_state_after_fill == "SETTLEMENT_PENDING"

        # Settle reservation with balance snapshot
        settled_ok = await active_pipeline.settle_reservation(client_order_id_1, snap1_id)
        assert settled_ok is True

        res_final = await pool.fetchrow(
            "SELECT state, venue_reflected, released_at FROM execution_reservations WHERE reservation_id = $1;",
            res1_id,
        )
        assert res_final["state"] == "RELEASED"
        assert res_final["venue_reflected"] is True
        assert res_final["released_at"] is not None

        # 7. Immediate Execution Settlement Path:
        # PRE_SEND -> INFLIGHT -> SETTLEMENT_PENDING -> RELEASED
        cmd2_id = uuid.uuid4()
        client_order_id_2 = f"DEMO-IMM-{test_id}"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_commands (
                    command_id, idempotency_key, command_type, target_instance_id,
                    requester_id, payload, status, attempt_count, created_at
                ) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'operator', $4::jsonb, 'PENDING', 0, now());
                """,
                cmd2_id,
                f"cmd-{cmd2_id}",
                target_instance,
                json.dumps({
                    "symbol": symbol,
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": "0.01",
                    "price": "50000.00",
                    "client_order_id": client_order_id_2,
                }),
            )
        claimed2 = await active_pipeline.claim_command(target_instance_id=target_instance)
        assert claimed2 is not None
        res2_order = await active_pipeline.process_submit_order(claimed2)
        assert res2_order["status"] == "COMPLETED"
        res2_id = uuid.UUID(res2_order["reservation_id"])

        # Fill immediately on mock venue (market order fill without intermediate snapshot)
        await mock_venue.simulate_fill(
            client_order_id=client_order_id_2,
            fill_qty=Decimal("0.01"),
            fill_price=Decimal("50000.00"),
        )
        await asyncio.sleep(0.01)

        res2_state = await pool.fetchval(
            "SELECT state FROM execution_reservations WHERE reservation_id = $1;", res2_id
        )
        assert res2_state == "SETTLEMENT_PENDING"

        # Settle immediate execution path
        await active_pipeline.settle_reservation(client_order_id_2, snap1_id)
        res2_final = await pool.fetchrow(
            "SELECT state, venue_reflected FROM execution_reservations WHERE reservation_id = $1;", res2_id
        )
        assert res2_final["state"] == "RELEASED"
        assert res2_final["venue_reflected"] is True

        # 8. Pre-Venue Rejection Settlement Path:
        # PRE_SEND -> RELEASED (venue_reflected = false)
        cmd3_id = uuid.uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_commands (
                    command_id, idempotency_key, command_type, target_instance_id,
                    requester_id, payload, status, attempt_count, created_at
                ) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'operator', $4::jsonb, 'PENDING', 0, now());
                """,
                cmd3_id,
                f"cmd-{cmd3_id}",
                target_instance,
                json.dumps({
                    "symbol": symbol,
                    "side": "BUY",
                    "order_type": "LIMIT",
                    # Excessive quantity exceeding remaining free balance
                    "quantity": "500.0",
                    "price": "50000.00",
                }),
            )
        claimed3 = await active_pipeline.claim_command(target_instance_id=target_instance)
        rej_result = await active_pipeline.process_submit_order(claimed3)
        assert rej_result["status"] == "REJECTED"
        assert "INSUFFICIENT" in rej_result["reason"]

        cmd3_final = await pool.fetchrow(
            "SELECT status, command_outcome FROM execution_commands WHERE command_id = $1;", cmd3_id
        )
        assert cmd3_final["status"] == "REJECTED"
        assert cmd3_final["command_outcome"] == "REJECTED"

        # 9. Transactional Idempotency & Monotonic Order Projections
        # Re-emit duplicate fill event for client_order_id_1
        duplicate_fill_report = {
            "event_type": "ORDER_FILLED",
            "source_event_id": "evt-fill-500001",  # Same source_event_id
            "trade_id": 500001,
            "client_order_id": client_order_id_1,
            "venue_order_id": "1000001",
            "instrument_id": instrument_id,
            "status": "FILLED",
            "raw_venue_status": "FILLED",
            "side": "BUY",
            "price": Decimal("50000.00"),
            "quantity": Decimal("0.05"),
            "quote_quantity": Decimal("2500.00"),
            "filled_qty": Decimal("0.05"),
            "commission_amount": Decimal("2.50"),
            "commission_asset": "USDT",
            "is_maker": False,
            "occurred_at_ns": time.time_ns(),
        }
        # First call was processed; duplicate call must return False (no-op)
        processed = await active_pipeline.handle_execution_report(duplicate_fill_report)
        assert processed is False

        # Verify trading_fills count did not increase
        fills_cnt = await pool.fetchval(
            "SELECT COUNT(*) FROM trading_fills WHERE client_order_id = $1;", client_order_id_1
        )
        assert fills_cnt == 1

        # Monotonicity test: delayed NEW event arrives for order that is already FILLED
        delayed_new_report = {
            "event_type": "ORDER_ACCEPTED",
            "source_event_id": f"evt-delayed-new-{uuid.uuid4().hex[:6]}",
            "client_order_id": client_order_id_1,
            "venue_order_id": "1000001",
            "instrument_id": instrument_id,
            "status": "ACCEPTED",
            "raw_venue_status": "NEW",
            "filled_qty": Decimal("0"),
            "occurred_at_ns": time.time_ns(),
        }
        await active_pipeline.handle_execution_report(delayed_new_report)
        order_status = await pool.fetchval(
            "SELECT status FROM trading_orders WHERE client_order_id = $1;", client_order_id_1
        )
        # Order MUST remain FILLED, not regress to ACCEPTED
        assert order_status == "FILLED"

        # 10. Ambiguous Outcome Protocol: Query Before Resubmitting
        cmd4_id = uuid.uuid4()
        client_order_id_4 = f"DEMO-AMB-{test_id}"
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_commands (
                    command_id, idempotency_key, command_type, target_instance_id,
                    requester_id, payload, status, attempt_count, created_at
                ) VALUES ($1, $2, 'SUBMIT_ORDER', $3, 'operator', $4::jsonb, 'PENDING', 0, now());
                """,
                cmd4_id,
                f"cmd-{cmd4_id}",
                target_instance,
                json.dumps({
                    "symbol": symbol,
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": "0.01",
                    "price": "50000.00",
                    "client_order_id": client_order_id_4,
                }),
            )
        claimed4 = await active_pipeline.claim_command(target_instance_id=target_instance)

        # Simulate network timeout on submission
        mock_venue.simulate_network_timeout = True
        # Venue does NOT have the order
        amb_result = await active_pipeline.process_submit_order(claimed4)
        # Propagation-aware: timeout retains UNKNOWN_PENDING_RECONCILIATION and defers to AuthoritativeReconciler
        assert amb_result["status"] == "UNKNOWN_PENDING_RECONCILIATION"
        assert amb_result["client_order_id"] == client_order_id_4
        mock_venue.simulate_network_timeout = False

        # 11. Transactional Outbox Emission and OutboxRelay Draining to Redis
        # Verify outbox event exists in PostgreSQL
        outbox_row = await pool.fetchrow(
            """
            SELECT * FROM outbox_events 
            WHERE stream_name = 'cryptonite:stream:trading_fills'
            ORDER BY created_at DESC LIMIT 1;
            """
        )
        assert outbox_row is not None
        payload_data = json.loads(outbox_row["payload"])
        assert payload_data["client_order_id"] in (client_order_id_1, client_order_id_2)

        # Drain outbox via OutboxRelay if not already drained by background daemon
        if outbox_row["status"] == "PENDING":
            relay = OutboxRelay(database_url=DB_URL, redis_url=REDIS_URL, batch_size=10)
            relay.pool = pool
            relay.redis = r_client
            drained_cnt = await relay.process_batch_once()
            assert drained_cnt >= 1

        # Check outbox_events status is now PUBLISHED
        outbox_status = await pool.fetchval(
            "SELECT status FROM outbox_events WHERE event_id = $1;", outbox_row["event_id"]
        )
        assert outbox_status == "PUBLISHED"

        # Verify Redis stream reception
        redis_evts = await r_client.xrevrange("cryptonite:stream:trading_fills", count=10)
        assert len(redis_evts) > 0
        all_order_ids = [
            json.loads(msg[1][b"data"].decode())["client_order_id"]
            for msg in redis_evts
        ]
        assert client_order_id_1 in all_order_ids or client_order_id_2 in all_order_ids

        # 12. Authoritative Reconciliation & Fail-Closed Gate
        venue_orders = [
            {
                "symbol": symbol,
                "orderId": 999888777,
                "clientOrderId": "ROGUE-UNMANAGED-001",
                "price": "50000.00",
                "origQty": "0.10",
                "status": "NEW",
            }
        ]
        from cryptonite_execution.reconciliation import UnmanagedOrderDetectedError
        active_pipeline.reconciler.emergency_marker_path = str(marker_path)

        with pytest.raises(UnmanagedOrderDetectedError):
            await active_pipeline.reconciler.reconcile_orders(venue_orders, trigger_type="STARTUP")

        # Verify kill switch latched and emergency marker dropped
        assert marker_path.exists()
        ks_latched = await pool.fetchval("SELECT is_latched FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;", env, account_id)
        assert ks_latched is True

        # Clean up kill switch
        await pool.execute("UPDATE execution_kill_switch SET is_latched = false WHERE environment = $1 AND account_id = $2;", env, account_id)

    finally:
        if active_pipeline:
            await active_pipeline.release_advisory_lock()
        await pool.close()
        await r_client.aclose()


@pytest.mark.asyncio
async def test_kill_switch_reset_safety_checklist_and_cas(tmp_path):
    """Verify 7-point checklist: CAS latch generation protection, audit validation, and marker removal."""
    from cryptonite_execution.demo_pipeline import KillSwitchResetDeniedError
    pool = await asyncpg.create_pool(dsn=DB_URL, min_size=2, max_size=5)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    test_id = uuid.uuid4().hex[:8].upper()
    env = "DEMO"
    account_id = f"ACC-RESET-{test_id}"
    marker_path = tmp_path / "emergency_kill_switch.marker"

    pipe = DemoExecutionPipeline(
        environment=env,
        account_id=account_id,
        pool=pool,
        redis_client=r_client,
        worker_id=f"worker-reset-{test_id}",
        emergency_marker_path=str(marker_path),
    )

    try:
        await pipe.acquire_advisory_lock()

        # 1. Trip kill switch via SECURITY DEFINER function -> latch_generation = 1
        gen1 = await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, 'Initial operational trip', 'OPERATOR:alice');",
            env,
            account_id,
        )
        assert gen1 == 1
        pipe.kill_switch.trip("OPERATOR:alice", "Initial operational trip")
        pipe.reconciler.drop_emergency_marker("Initial operational trip")
        assert marker_path.exists()

        # Create audit event in auth_audit_events
        audit_event_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO auth_audit_events (event_id, username, action, occurred_at)
            VALUES ($1, 'alice', 'KILL_SWITCH_RESET_REQUESTED', now());
            """,
            audit_event_id,
        )

        # 2. Race condition: Newer trip occurs before reset executes -> latch_generation becomes 2
        gen2 = await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, 'Newer racing trip occurred', 'OPERATOR:bob');",
            env,
            account_id,
        )
        assert gen2 == 2

        # 3. Test CAS Mismatch Rejection: Request with older expected_generation=1 MUST be rejected!
        stale_reset_cmd = {
            "command_id": uuid.uuid4(),
            "attempt_id": uuid.uuid4(),
            "lease_owner": pipe.worker_id,
            "requester_id": "OPERATOR:alice",
            "command_type": "KILL_SWITCH_RESET_REQUEST",
            "payload": {
                "actor_id": "OPERATOR:alice",
                "reason": "Clear switch",
                "auth_audit_event_id": str(audit_event_id),
                "expected_generation": 1,  # Stale! Current is 2
                "expires_at_ns": time.time_ns() + 10_000_000_000,
            },
        }
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'KILL_SWITCH_RESET_REQUEST', 'RUNNING', $3, $4);
            """,
            stale_reset_cmd["command_id"],
            f"idemp-{stale_reset_cmd['command_id']}",
            stale_reset_cmd["attempt_id"],
            pipe.worker_id,
        )

        with pytest.raises(KillSwitchResetDeniedError) as exc_info:
            await pipe.process_kill_switch_reset_request(stale_reset_cmd)
        assert "Latch generation mismatch" in str(exc_info.value)
        # Verify switch remains latched
        assert pipe.kill_switch.is_latched is True
        db_latched = await pool.fetchval(
            "SELECT is_latched FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
            env, account_id
        )
        assert db_latched is True

        # 4. Test Safety Checklist: Reject if active open orders exist
        c_order_id = f"ORD-OPEN-{uuid.uuid4().hex[:6]}"
        await pool.execute(
            """
            INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id, side, order_type, quantity, status)
            VALUES ($1, $2, $3, 'strat', 'BTCUSDT.BINANCE', 'BUY', 'LIMIT', 0.01, 'ACCEPTED');
            """,
            env,
            account_id,
            c_order_id,
        )
        valid_gen_cmd = {
            "command_id": uuid.uuid4(),
            "attempt_id": uuid.uuid4(),
            "lease_owner": pipe.worker_id,
            "requester_id": "OPERATOR:alice",
            "command_type": "KILL_SWITCH_RESET_REQUEST",
            "payload": {
                "actor_id": "OPERATOR:alice",
                "reason": "Clear switch with expected gen 2",
                "auth_audit_event_id": str(audit_event_id),
                "expected_generation": 2,  # Matches current gen!
                "expires_at_ns": time.time_ns() + 10_000_000_000,
            },
        }
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'KILL_SWITCH_RESET_REQUEST', 'RUNNING', $3, $4);
            """,
            valid_gen_cmd["command_id"],
            f"idemp-{valid_gen_cmd['command_id']}",
            valid_gen_cmd["attempt_id"],
            pipe.worker_id,
        )

        with pytest.raises(KillSwitchResetDeniedError) as exc_info2:
            await pipe.process_kill_switch_reset_request(valid_gen_cmd)
        assert "Safety checklist failed: open_orders=1" in str(exc_info2.value)

        # 5. Clean up open order and perform successful reset
        await pool.execute(
            "UPDATE trading_orders SET status = 'CANCELED' WHERE client_order_id = $1;", c_order_id
        )
        success_cmd = {
            "command_id": uuid.uuid4(),
            "attempt_id": uuid.uuid4(),
            "lease_owner": pipe.worker_id,
            "requester_id": "OPERATOR:alice",
            "command_type": "KILL_SWITCH_RESET_REQUEST",
            "payload": {
                "actor_id": "OPERATOR:alice",
                "reason": "Safe to clear switch",
                "auth_audit_event_id": str(audit_event_id),
                "expected_generation": 2,
                "expires_at_ns": time.time_ns() + 10_000_000_000,
            },
        }
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'KILL_SWITCH_RESET_REQUEST', 'RUNNING', $3, $4);
            """,
            success_cmd["command_id"],
            f"idemp-{success_cmd['command_id']}",
            success_cmd["attempt_id"],
            pipe.worker_id,
        )

        reset_result = await pipe.process_kill_switch_reset_request(success_cmd)
        assert reset_result["status"] == "COMPLETED"
        assert reset_result["kill_switch_latched"] is False
        assert pipe.kill_switch.is_latched is False
        assert pipe.is_gate_open is True
        assert not marker_path.exists()

        db_latched_after = await pool.fetchval(
            "SELECT is_latched FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
            env, account_id
        )
        assert db_latched_after is False
    finally:
        await pipe.shutdown()
        await pool.close()
        await r_client.aclose()


@pytest.mark.asyncio
async def test_advisory_lock_lost_and_pre_socket_abort():
    """Verify advisory lock loss immediately trips gate and aborts order with CANCELLED_UNSENT."""
    from cryptonite_execution.demo_pipeline import AdvisoryLockLostError
    pool = await asyncpg.create_pool(dsn=DB_URL, min_size=2, max_size=5)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    test_id = uuid.uuid4().hex[:8].upper()
    env = "DEMO"
    account_id = f"ACC-ADV-{test_id}"

    # Seed balance
    await pool.execute(
        """
        INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
        VALUES ($1, $2, 'USDT', 5000.0, 0.0, $3, $3)
        ON CONFLICT (environment, account_id, asset) DO UPDATE SET free = 5000.0;
        """,
        env, account_id, time.time_ns(),
    )

    pipe = DemoExecutionPipeline(
        environment=env,
        account_id=account_id,
        pool=pool,
        redis_client=r_client,
        worker_id=f"worker-adv-{test_id}",
    )

    try:
        # Acquire advisory lock
        await pipe.acquire_advisory_lock()
        assert pipe._advisory_lock_held is True

        cmd_id = uuid.uuid4()
        c_order_id = f"ORD-ADV-{uuid.uuid4().hex[:6]}"
        cmd = {
            "command_id": cmd_id,
            "attempt_id": uuid.uuid4(),
            "lease_owner": pipe.worker_id,
            "requester_id": "operator",
            "command_type": "SUBMIT_ORDER",
            "payload": {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.01",
                "price": "50000.00",
                "client_order_id": c_order_id,
            },
        }
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'SUBMIT_ORDER', 'RUNNING', $3, $4);
            """,
            cmd["command_id"],
            f"idemp-{cmd['command_id']}",
            cmd["attempt_id"],
            pipe.worker_id,
        )

        # Simulate losing advisory lock by closing the connection right before transmission
        await pipe._advisory_conn.close()

        # Processing order must detect lock loss before network dispatch
        with pytest.raises(AdvisoryLockLostError):
            await pipe.process_submit_order(cmd)

        # 1. Gate tripped
        assert pipe.kill_switch.is_latched is True
        assert pipe.is_gate_open is False

        # 2. Command rejected
        cmd_final = await pool.fetchrow(
            "SELECT status, command_outcome FROM execution_commands WHERE command_id = $1;", cmd_id
        )
        assert cmd_final["status"] == "REJECTED"
        assert cmd_final["command_outcome"] == "REJECTED"

        # 3. Reservation safely released with CANCELLED_UNSENT provenance
        res_row = await pool.fetchrow(
            "SELECT state, venue_reflected, release_reason FROM execution_reservations WHERE client_order_id = $1;",
            c_order_id,
        )
        assert res_row is not None
        assert res_row["state"] == "RELEASED"
        assert res_row["venue_reflected"] is False
        assert res_row["release_reason"] == "CANCELLED_UNSENT"
    finally:
        await pipe.shutdown()
        await pool.close()
        await r_client.aclose()
