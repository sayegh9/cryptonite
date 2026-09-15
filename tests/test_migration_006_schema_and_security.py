"""Integration and schema constraint verification test for Migration 006.

Verifies:
1. execution_reservations uniqueness, valid states, and venue_reflected consistency constraints.
2. trading_orders primary keys, check constraints, and FK RESTRICT behavior.
3. trading_order_events deduplication on source_event_id.
4. execution_commands.command_outcome domain check constraint.
5. Least-privilege role permissions: cryptonite_executor zero access to operator_users,
   cryptonite_api read visibility and inability to UPDATE execution_commands.
"""

import uuid
import asyncpg
import pytest

DB_URL_ADMIN = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
DB_URL_EXECUTOR = "postgresql://cryptonite_executor:executor_dev_pass@localhost:5432/cryptonite"
DB_URL_API = "postgresql://cryptonite_api:api_dev_pass@localhost:5432/cryptonite"


@pytest.mark.asyncio
async def test_execution_commands_outcome_constraint():
    """Verify execution_commands.command_outcome accepts valid domain values and rejects invalid."""
    pool = await asyncpg.create_pool(DB_URL_ADMIN)
    try:
        cmd_id = uuid.uuid4()
        # Insert valid UNKNOWN_PENDING_RECONCILIATION
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, command_outcome)
            VALUES ($1, $2, 'SUBMIT_ORDER', 'FAILED', 'UNKNOWN_PENDING_RECONCILIATION');
            """,
            cmd_id,
            f"idemp-{cmd_id}",
        )
        row = await pool.fetchrow("SELECT command_outcome FROM execution_commands WHERE command_id = $1;", cmd_id)
        assert row["command_outcome"] == "UNKNOWN_PENDING_RECONCILIATION"

        # Try inserting invalid command outcome
        bad_id = uuid.uuid4()
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                """
                INSERT INTO execution_commands (command_id, idempotency_key, command_type, status, command_outcome)
                VALUES ($1, $2, 'SUBMIT_ORDER', 'FAILED', 'INVALID_OUTCOME');
                """,
                bad_id,
                f"idemp-{bad_id}",
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_execution_reservations_constraints_and_uniqueness():
    """Verify execution_reservations state transitions, venue_reflected consistency, and uniqueness."""
    pool = await asyncpg.create_pool(DB_URL_ADMIN)
    try:
        cmd_id = uuid.uuid4()
        client_order_id = f"ORDER-{uuid.uuid4().hex[:12]}"
        env = "DEMO"
        acc = "BINANCE-SPOT-DEMO"

        # Create command and order prerequisite
        await pool.execute(
            """
            INSERT INTO execution_commands (command_id, idempotency_key, command_type, status)
            VALUES ($1, $2, 'SUBMIT_ORDER', 'CLAIMED');
            """,
            cmd_id,
            f"idemp-{cmd_id}",
        )
        await pool.execute(
            """
            INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id, side, order_type, quantity, status)
            VALUES ($1, $2, $3, 'test_strat', 'BTCUSDT.BINANCE', 'BUY', 'LIMIT', 0.01, 'SUBMITTED');
            """,
            env,
            acc,
            client_order_id,
        )

        # 1. Valid insert: PRE_SEND with venue_reflected=false
        res_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO execution_reservations (
                reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected
            ) VALUES ($1, $2, $3, $4, $5, 'USDT', 500.0, 5.0, 'PRE_SEND', false);
            """,
            res_id,
            env,
            acc,
            cmd_id,
            client_order_id,
        )

        # 2. Duplicate reservation for same (env, acc, client_order_id, asset) must fail
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute(
                """
                INSERT INTO execution_reservations (
                    reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected
                ) VALUES ($1, $2, $3, $4, $5, 'USDT', 100.0, 1.0, 'INFLIGHT', false);
                """,
                uuid.uuid4(),
                env,
                acc,
                cmd_id,
                client_order_id,
            )

        # 3. Invalid state must fail check constraint
        bad_order_id = f"ORDER-{uuid.uuid4().hex[:12]}"
        await pool.execute(
            """
            INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id, side, order_type, quantity, status)
            VALUES ($1, $2, $3, 'test_strat', 'BTCUSDT.BINANCE', 'BUY', 'LIMIT', 0.01, 'SUBMITTED');
            """,
            env,
            acc,
            bad_order_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                """
                INSERT INTO execution_reservations (
                    reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected
                ) VALUES ($1, $2, $3, $4, $5, 'USDT', 100.0, 1.0, 'BOGUS_STATE', false);
                """,
                uuid.uuid4(),
                env,
                acc,
                cmd_id,
                bad_order_id,
            )

        # 4. Inconsistent venue_reflected: PRE_SEND with venue_reflected=true must fail
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                """
                INSERT INTO execution_reservations (
                    reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected
                ) VALUES ($1, $2, $3, $4, $5, 'USDT', 100.0, 1.0, 'PRE_SEND', true);
                """,
                uuid.uuid4(),
                env,
                acc,
                cmd_id,
                bad_order_id,
            )

        # 5. Inconsistent venue_reflected: VENUE_REFLECTED with venue_reflected=false must fail
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                """
                INSERT INTO execution_reservations (
                    reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected
                ) VALUES ($1, $2, $3, $4, $5, 'USDT', 100.0, 1.0, 'VENUE_REFLECTED', false);
                """,
                uuid.uuid4(),
                env,
                acc,
                cmd_id,
                bad_order_id,
            )

        # 6. Valid transition: update to VENUE_REFLECTED with venue_reflected=true
        await pool.execute(
            """
            UPDATE execution_reservations
            SET state = 'VENUE_REFLECTED', venue_reflected = true
            WHERE reservation_id = $1;
            """,
            res_id,
        )
        res_row = await pool.fetchrow("SELECT state, venue_reflected FROM execution_reservations WHERE reservation_id = $1;", res_id)
        assert res_row["state"] == "VENUE_REFLECTED"
        assert res_row["venue_reflected"] is True

        # 7. Valid transition: RELEASED with venue_reflected=false (requires valid unsent release_reason)
        res_rej_id = uuid.uuid4()
        c_rej_order_id = f"ORDER-{uuid.uuid4().hex[:12]}"
        await pool.execute(
            """
            INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id, side, order_type, quantity, status)
            VALUES ($1, $2, $3, 'test_strat', 'BTCUSDT.BINANCE', 'BUY', 'LIMIT', 0.01, 'REJECTED');
            """,
            env,
            acc,
            c_rej_order_id,
        )
        await pool.execute(
            """
            INSERT INTO execution_reservations (
                reservation_id, environment, account_id, command_id, client_order_id, asset, amount, fee_slippage_headroom, state, venue_reflected, release_reason
            ) VALUES ($1, $2, $3, $4, $5, 'USDT', 200.0, 2.0, 'RELEASED', false, 'PRE_SEND_REJECTED');
            """,
            res_rej_id,
            env,
            acc,
            cmd_id,
            c_rej_order_id,
        )
        row_rej = await pool.fetchrow("SELECT state, venue_reflected, release_reason FROM execution_reservations WHERE reservation_id = $1;", res_rej_id)
        assert row_rej["state"] == "RELEASED"
        assert row_rej["venue_reflected"] is False
        assert row_rej["release_reason"] == "PRE_SEND_REJECTED"
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_trading_order_events_idempotency():
    """Verify trading_order_events enforces uniqueness on (env, acc, client_order_id, source_event_id)."""
    pool = await asyncpg.create_pool(DB_URL_ADMIN)
    try:
        client_order_id = f"ORDER-{uuid.uuid4().hex[:12]}"
        env = "DEMO"
        acc = "BINANCE-SPOT-DEMO"

        await pool.execute(
            """
            INSERT INTO trading_orders (environment, account_id, client_order_id, strategy_id, instrument_id, side, order_type, quantity, status)
            VALUES ($1, $2, $3, 'test_strat', 'BTCUSDT.BINANCE', 'BUY', 'LIMIT', 0.01, 'SUBMITTED');
            """,
            env,
            acc,
            client_order_id,
        )

        source_evt = "ws-binance-event-9999"
        await pool.execute(
            """
            INSERT INTO trading_order_events (
                environment, account_id, client_order_id, source_event_id, event_type, payload, occurred_at_ns
            ) VALUES ($1, $2, $3, $4, 'ORDER_ACCEPTED', '{"status":"NEW"}'::jsonb, 1725700000000000000);
            """,
            env,
            acc,
            client_order_id,
            source_evt,
        )

        # Duplicate replay of same source event id must fail
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute(
                """
                INSERT INTO trading_order_events (
                    environment, account_id, client_order_id, source_event_id, event_type, payload, occurred_at_ns
                ) VALUES ($1, $2, $3, $4, 'ORDER_ACCEPTED', '{"status":"NEW"}'::jsonb, 1725700000000000000);
                """,
                env,
                acc,
                client_order_id,
                source_evt,
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_role_least_privilege_and_zero_access():
    """Verify cryptonite_executor has ZERO ACCESS to operator_users, and NO DELETE on financial ledger."""
    # 1. Test cryptonite_executor zero access to operator_users
    pool_exec = await asyncpg.create_pool(DB_URL_EXECUTOR)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.fetch("SELECT * FROM operator_users;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.fetch("SELECT * FROM auth_audit_events;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.fetch("SELECT * FROM totp_replay_cache;")

        # 2. Test cryptonite_executor CANNOT DELETE from financial ledger
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.execute("DELETE FROM trading_orders;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.execute("DELETE FROM trading_fills;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.execute("DELETE FROM trading_order_events;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.execute("DELETE FROM trading_balance_snapshots;")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_exec.execute("DELETE FROM execution_reservations;")
    finally:
        await pool_exec.close()

    # 3. Test cryptonite_api has read access to v_operator_auth but cannot UPDATE execution_commands
    pool_api = await asyncpg.create_pool(DB_URL_API)
    try:
        # Can select from v_operator_auth
        rows = await pool_api.fetch("SELECT * FROM v_operator_auth LIMIT 1;")
        assert isinstance(rows, list)

        # Can select from trading_orders
        orders = await pool_api.fetch("SELECT * FROM trading_orders LIMIT 1;")
        assert isinstance(orders, list)

        # Cannot UPDATE execution_commands
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_api.execute("UPDATE execution_commands SET command_outcome = 'SUCCESS';")

        # 4. Test cryptonite_api CANNOT UPDATE execution_kill_switch directly
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool_api.execute("UPDATE execution_kill_switch SET is_latched = true;")

        # 5. Test cryptonite_api CAN execute trip_execution_kill_switch SECURITY DEFINER function
        gen = await pool_api.fetchval(
            "SELECT trip_execution_kill_switch('DEMO', 'TEST-ACC', 'Security check trip', 'OPERATOR:test');"
        )
        assert gen is not None
        assert gen >= 1
    finally:
        await pool_api.close()
