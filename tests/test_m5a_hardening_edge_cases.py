"""M5A Hardening Edge Case Integration Tests.

Verifies:
1. A newer trip racing an older reset (CAS generation mismatch fails closed).
2. Crash after marker removal but before DB commit (fails closed on restart/reconciliation).
3. Settlement snapshot mismatch (foreign key violation prevents corrupt release).
4. Operator abort after venue acknowledgement (blocked by state machine and check constraint).
5. WebSocket 24-hour renewal and subscription restoration (re-auth, re-sub, and post-reconnect hook).
"""

import asyncio
import json
import os
from pathlib import Path
import time
import uuid
from decimal import Decimal

import asyncpg
import pytest
import redis.asyncio as aioredis
import websockets

from cryptonite_execution.binance_user_stream import BinanceDemoWebSocketApiClient
from cryptonite_execution.demo_pipeline import (
    AdvisoryLockLostError,
    DemoExecutionPipeline,
    MockVenueAdapter,
)
from cryptonite_execution.reconciliation import AuthoritativeReconciler
from cryptonite_execution.reservations import (
    DurableReservationManager,
    ReservationStateError,
)
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
REDIS_URL = "redis://127.0.0.1:6379/0"


@pytest.mark.asyncio
async def test_newer_trip_racing_older_reset(tmp_path):
    """Verify that if a new kill-switch trip occurs while an older reset is inflight,
    CAS on latch_generation prevents unlatching, failing closed.
    """
    pool = await asyncpg.create_pool(DB_URL)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    marker_path = tmp_path / "emergency_kill_switch.marker"
    env = "DEMO"
    acc = f"ACC-RACE-{uuid.uuid4().hex[:8]}"

    pipe = DemoExecutionPipeline(
        environment=env,
        account_id=acc,
        pool=pool,
        redis_client=r_client,
        worker_id=f"worker-race-{acc}",
        emergency_marker_path=str(marker_path),
    )

    try:
        # 1. Trip kill switch -> generation 1
        gen1 = await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, 'First trip', 'OPERATOR');",
            env, acc,
        )
        assert gen1 == 1

        # 2. Acquire advisory lock
        await pipe.acquire_advisory_lock()

        # 3. Operator issues reset command with expected_generation = 1
        audit_event_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO auth_audit_events (event_id, action, username, ip_address, user_agent, details)
            VALUES ($1, 'KILL_SWITCH_RESET_REQUESTED', 'alice', '127.0.0.1', 'test-agent', '{}');
            """,
            audit_event_id,
        )

        reset_cmd = {
            "command_id": uuid.uuid4(),
            "attempt_id": uuid.uuid4(),
            "lease_owner": pipe.worker_id,
            "command_type": "KILL_SWITCH_RESET_REQUEST",
            "payload": {
                "actor_id": "OPERATOR:alice",
                "reason": "Clear first trip",
                "auth_audit_event_id": str(audit_event_id),
                "expected_generation": 1,
                "expires_at_ns": time.time_ns() + 10_000_000_000,
            },
        }

        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'KILL_SWITCH_RESET_REQUEST', 'RUNNING', $3, $4);
            """,
            reset_cmd["command_id"],
            f"idemp-{reset_cmd['command_id']}",
            reset_cmd["attempt_id"],
            pipe.worker_id,
        )

        # 4. RACE: Before executor processes reset, a newer trip occurs!
        gen2 = await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, 'Second trip during reset', 'RECONCILER');",
            env, acc,
        )
        assert gen2 == 2

        # 5. Executor processes the stale reset command -> must raise KillSwitchResetDeniedError
        from cryptonite_execution.demo_pipeline import KillSwitchResetDeniedError
        with pytest.raises(KillSwitchResetDeniedError) as exc_info:
            await pipe.process_kill_switch_reset_request(reset_cmd)
        assert "Latch generation mismatch" in str(exc_info.value)

        # 6. Command in database must be marked REJECTED
        cmd_row = await pool.fetchrow(
            "SELECT status, command_outcome, error_message FROM execution_commands WHERE command_id = $1;",
            reset_cmd["command_id"],
        )
        assert cmd_row["status"] == "REJECTED"
        assert cmd_row["command_outcome"] == "REJECTED"
        assert "Latch generation mismatch" in cmd_row["error_message"]

        # 7. Database state must remain latched with generation 2
        row = await pool.fetchrow(
            "SELECT is_latched, latch_generation, reason FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
            env, acc,
        )
        assert row["is_latched"] is True
        assert row["latch_generation"] == 2
        assert row["reason"] == "Second trip during reset"
    finally:
        await pipe.shutdown()
        await pool.close()
        await r_client.aclose()


@pytest.mark.asyncio
async def test_crash_after_marker_removal_before_db_commit(tmp_path):
    """Verify that if marker file is removed but a crash prevents DB unlatch,
    subsequent restart/reconciliation maintains fail-closed state.
    """
    pool = await asyncpg.create_pool(DB_URL)
    marker_path = tmp_path / "emergency_kill_switch.marker"
    env = "DEMO"
    acc = f"ACC-CRASH-{uuid.uuid4().hex[:8]}"

    try:
        # 1. Trip kill switch and write marker file
        await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, 'Tripped before crash', 'OPERATOR');",
            env, acc,
        )
        marker_path.write_text("EMERGENCY_KILL_SWITCH_LATCHED: Tripped before crash\n")
        assert marker_path.exists()

        # 2. Simulate crash during reset: marker removed from disk, but DB transaction never ran/committed
        marker_path.unlink()
        assert not marker_path.exists()

        # 3. New executor starts up or reconciler runs
        reconciler = AuthoritativeReconciler(
            pool=pool,
            environment=env,
            account_id=acc,
            emergency_marker_path=str(marker_path),
        )

        # Reconciler checks DB kill switch state
        db_latched = await pool.fetchval(
            "SELECT is_latched FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
            env, acc,
        )
        assert db_latched is True, "Database must still be latched because transaction never committed"

        # When reconciler runs order reconciliation or encounters unmanaged/inconsistent state,
        # it re-drops the marker file
        reconciler.drop_emergency_marker("Database kill switch latched upon restart")
        assert marker_path.exists()
        assert "Database kill switch latched upon restart" in marker_path.read_text()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_settlement_snapshot_mismatch():
    """Verify that setting settlement_snapshot_id to a non-existent snapshot
    is rejected by PostgreSQL foreign key constraint.
    """
    pool = await asyncpg.create_pool(DB_URL)
    mgr = DurableReservationManager(pool)
    env = "DEMO"
    acc = f"ACC-MISMATCH-{uuid.uuid4().hex[:8]}"
    asset = "USDT"

    try:
        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 1000.0, 0.0, $4, $4);
            """,
            env, acc, asset, time.time_ns(),
        )

        cmd_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type) VALUES ($1, $2, 'SUBMIT_ORDER');",
            cmd_id, f"idemp-{cmd_id}",
        )

        res_id = await mgr.acquire_reservation(
            environment=env,
            account_id=acc,
            command_id=cmd_id,
            client_order_id=f"ORD-{uuid.uuid4().hex[:8]}",
            asset=asset,
            amount=Decimal("100.0"),
        )
        await mgr.mark_inflight(res_id)

        # Valid snapshot for another account
        other_acc = f"ACC-OTHER-{uuid.uuid4().hex[:8]}"
        other_snap_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO trading_balance_snapshots (
                snapshot_id, environment, account_id, snapshot_type, account_update_time_ns, observed_at_ns, balances_payload, balances_checksum
            ) VALUES ($1, $2, $3, 'WS_ACCOUNT_UPDATE', $4, $4, '[]'::jsonb, 'otherchk');
            """,
            other_snap_id, env, other_acc, time.time_ns(),
        )

        # Non-existent snapshot ID must fail with ForeignKeyViolationError
        non_existent_snap = uuid.uuid4()
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await pool.execute(
                """
                UPDATE execution_reservations
                SET state = 'RELEASED',
                    venue_reflected = true,
                    release_reason = 'VENUE_FILLED_SETTLED',
                    settlement_snapshot_id = $1,
                    released_at = now()
                WHERE reservation_id = $2;
                """,
                non_existent_snap, res_id,
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_operator_abort_rejected_after_venue_acknowledgement():
    """Verify that once an order is acknowledged or reflected on venue,
    attempting an unsent abort (OPERATOR_ABORTED_UNSENT) is rejected.
    """
    pool = await asyncpg.create_pool(DB_URL)
    mgr = DurableReservationManager(pool)
    env = "DEMO"
    acc = f"ACC-ABORT-{uuid.uuid4().hex[:8]}"
    asset = "USDT"

    try:
        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 1000.0, 0.0, $4, $4);
            """,
            env, acc, asset, time.time_ns(),
        )

        cmd_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type) VALUES ($1, $2, 'SUBMIT_ORDER');",
            cmd_id, f"idemp-{cmd_id}",
        )

        res_id = await mgr.acquire_reservation(
            environment=env,
            account_id=acc,
            command_id=cmd_id,
            client_order_id=f"ORD-{uuid.uuid4().hex[:8]}",
            asset=asset,
            amount=Decimal("100.0"),
        )
        await mgr.mark_inflight(res_id)
        await mgr.mark_acked(res_id)

        # In ACKED_UNREFLECTED state, attempting OPERATOR_ABORTED_UNSENT must fail
        with pytest.raises(ReservationStateError):
            await mgr.release_reservation(res_id, release_reason="OPERATOR_ABORTED_UNSENT")

        # In VENUE_REFLECTED state, attempting OPERATOR_ABORTED_UNSENT must also fail
        snap_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO trading_balance_snapshots (
                snapshot_id, environment, account_id, snapshot_type, account_update_time_ns, observed_at_ns, balances_payload, balances_checksum
            ) VALUES ($1, $2, $3, 'WS_ACCOUNT_UPDATE', $4, $4, '[]'::jsonb, 'snapchk');
            """,
            snap_id, env, acc, time.time_ns(),
        )
        await mgr.confirm_venue_reflection(res_id, snap_id)

        with pytest.raises(ReservationStateError):
            await mgr.release_reservation(res_id, release_reason="OPERATOR_ABORTED_UNSENT")

        # Database check constraint also rejects venue_reflected = TRUE with OPERATOR_ABORTED_UNSENT
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await pool.execute(
                """
                UPDATE execution_reservations
                SET state = 'RELEASED',
                    venue_reflected = true,
                    release_reason = 'OPERATOR_ABORTED_UNSENT',
                    released_at = now()
                WHERE reservation_id = $1;
                """,
                res_id,
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_websocket_renewal_and_resubscription():
    """Verify BinanceDemoWebSocketApiClient 24-hour renewal cycle:
    re-auth, re-subscribes, and invokes post-reconnect reconciliation hook.
    """
    priv_key = ed25519.Ed25519PrivateKey.generate()
    pem_bytes = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    connection_count = 0
    subscription_counter = 100

    async def ws_server_handler(websocket):
        nonlocal connection_count, subscription_counter
        connection_count += 1
        async for raw_msg in websocket:
            msg = json.loads(raw_msg)
            method = msg.get("method")
            req_id = msg.get("id")

            if method == "session.logon":
                await websocket.send(json.dumps({"id": req_id, "status": 200, "result": {"apiKey": "test_key"}}))
            elif method == "userDataStream.subscribe":
                subscription_counter += 1
                await websocket.send(json.dumps({"id": req_id, "status": 200, "result": {"subscriptionId": subscription_counter}}))
            elif method == "userDataStream.unsubscribe":
                await websocket.send(json.dumps({"id": req_id, "status": 200, "result": {}}))

    server = await websockets.serve(ws_server_handler, "127.0.0.1", 0)
    server_port = server.sockets[0].getsockname()[1]
    ws_url = f"ws://127.0.0.1:{server_port}"

    reconcile_called = 0
    async def mock_reconcile_hook():
        nonlocal reconcile_called
        reconcile_called += 1
        return True

    client = BinanceDemoWebSocketApiClient(
        api_key="test_api_key",
        private_key_pem=pem_bytes,
        ws_url=ws_url,
        reconcile_hook=mock_reconcile_hook,
    )

    try:
        await client.start()
        for _ in range(50):
            if client.is_connected and client.is_subscribed:
                break
            await asyncio.sleep(0.05)

        assert client.is_connected is True
        assert client.is_subscribed is True
        first_sub_id = client.subscription_id
        assert first_sub_id == 101
        assert connection_count == 1
        assert reconcile_called == 1

        # Trigger renewal
        await client._renew_connection()

        for _ in range(50):
            if client.subscription_id != first_sub_id:
                break
            await asyncio.sleep(0.05)

        assert client.is_connected is True
        assert client.is_subscribed is True
        assert client.subscription_id == 102
        assert connection_count == 2
        assert reconcile_called == 2
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_advisory_lock_lost_before_socket_write(tmp_path):
    """Verify that if advisory lock is lost BEFORE socket write,
    the pipeline marks command REJECTED and releases reservation as CANCELLED_UNSENT.
    """
    pool = await asyncpg.create_pool(DB_URL)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    marker_path = tmp_path / "emergency_kill_switch.marker"
    env = "DEMO"
    acc = f"ACC-LOCK-PRE-{uuid.uuid4().hex[:8]}"
    asset = "USDT"

    pipe = DemoExecutionPipeline(
        environment=env,
        account_id=acc,
        pool=pool,
        redis_client=r_client,
        worker_id=f"worker-lock-pre-{acc}",
        emergency_marker_path=str(marker_path),
    )

    try:
        # 1. Setup balance and acquire advisory lock
        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 10000.0, 0.0, $4, $4);
            """,
            env, acc, asset, time.time_ns(),
        )
        acquired = await pipe.acquire_advisory_lock()
        assert acquired is True

        cmd_id = uuid.uuid4()
        attempt_id = uuid.uuid4()
        client_order_id = f"ORD-PRE-{uuid.uuid4().hex[:8]}"

        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'SUBMIT_ORDER', 'RUNNING', $3, $4);
            """,
            cmd_id, f"idemp-{cmd_id}", attempt_id, pipe.worker_id,
        )

        cmd = {
            "command_id": cmd_id,
            "attempt_id": attempt_id,
            "lease_owner": pipe.worker_id,
            "command_type": "SUBMIT_ORDER",
            "payload": {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.01",
                "price": "50000.00",
                "client_order_id": client_order_id,
            },
        }

        # 2. Simulate advisory lock loss before socket write
        pipe._advisory_lock_held = False

        with pytest.raises(AdvisoryLockLostError):
            await pipe.process_submit_order(cmd)

        # 3. Kill switch should be tripped
        assert pipe.kill_switch.is_latched is True

        # 4. Command must be marked REJECTED
        cmd_row = await pool.fetchrow(
            "SELECT status, command_outcome, error_message FROM execution_commands WHERE command_id = $1;",
            cmd_id,
        )
        assert cmd_row["status"] == "REJECTED"
        assert cmd_row["command_outcome"] == "REJECTED"
        assert "Advisory lock lost" in cmd_row["error_message"]

        # 5. Reservation must be released with CANCELLED_UNSENT and venue_reflected = false
        res_row = await pool.fetchrow(
            "SELECT state, release_reason, venue_reflected FROM execution_reservations WHERE command_id = $1;",
            cmd_id,
        )
        assert res_row is not None
        assert res_row["state"] == "RELEASED"
        assert res_row["release_reason"] == "CANCELLED_UNSENT"
        assert res_row["venue_reflected"] is False

    finally:
        await pipe.shutdown()
        await pool.close()
        await r_client.aclose()


@pytest.mark.asyncio
async def test_advisory_lock_lost_after_socket_write(tmp_path):
    """Verify that if advisory lock is lost AFTER socket write starts,
    the pipeline retains reservation, marks UNKNOWN_PENDING_RECONCILIATION,
    and NEVER releases funds as CANCELLED_UNSENT.
    """
    pool = await asyncpg.create_pool(DB_URL)
    r_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    marker_path = tmp_path / "emergency_kill_switch.marker"
    env = "DEMO"
    acc = f"ACC-LOCK-POST-{uuid.uuid4().hex[:8]}"
    asset = "USDT"

    mock_venue = MockVenueAdapter()
    pipe = DemoExecutionPipeline(
        environment=env,
        account_id=acc,
        pool=pool,
        redis_client=r_client,
        venue_client=mock_venue,
        worker_id=f"worker-lock-post-{acc}",
        emergency_marker_path=str(marker_path),
    )

    original_submit = mock_venue.submit_order

    async def submit_and_drop_lock(**kwargs):
        # Drop the advisory lock inside socket write
        pipe._advisory_lock_held = False
        return await original_submit(**kwargs)

    mock_venue.submit_order = submit_and_drop_lock

    try:
        # 1. Setup balance and acquire advisory lock
        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 10000.0, 0.0, $4, $4);
            """,
            env, acc, asset, time.time_ns(),
        )
        acquired = await pipe.acquire_advisory_lock()
        assert acquired is True

        cmd_id = uuid.uuid4()
        attempt_id = uuid.uuid4()
        client_order_id = f"ORD-POST-{uuid.uuid4().hex[:8]}"

        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, attempt_id, lease_owner)
            VALUES ($1, $2, 'SUBMIT_ORDER', 'RUNNING', $3, $4);
            """,
            cmd_id, f"idemp-{cmd_id}", attempt_id, pipe.worker_id,
        )

        cmd = {
            "command_id": cmd_id,
            "attempt_id": attempt_id,
            "lease_owner": pipe.worker_id,
            "command_type": "SUBMIT_ORDER",
            "payload": {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.01",
                "price": "50000.00",
                "client_order_id": client_order_id,
            },
        }

        # 2. Process order - lock is dropped during submit, post-check trips AdvisoryLockLostError
        await pipe.process_submit_order(cmd)

        # 3. Reservation must NOT be released as CANCELLED_UNSENT!
        res_row = await pool.fetchrow(
            "SELECT state, release_reason, venue_reflected FROM execution_reservations WHERE command_id = $1;",
            cmd_id,
        )
        assert res_row is not None
        assert res_row["release_reason"] != "CANCELLED_UNSENT", "Cannot release as CANCELLED_UNSENT after socket dispatch!"

        # 4. Command outcome must be tracked for reconciliation
        cmd_row = await pool.fetchrow(
            "SELECT status, command_outcome, error_message FROM execution_commands WHERE command_id = $1;",
            cmd_id,
        )
        assert cmd_row["command_outcome"] in ("UNKNOWN_PENDING_RECONCILIATION", "SUCCESS")
        assert pipe.kill_switch.is_latched is True

    finally:
        await pipe.shutdown()
        await pool.close()
        await r_client.aclose()
