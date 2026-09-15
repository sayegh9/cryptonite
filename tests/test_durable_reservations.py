"""Tests for DurableReservationManager."""

import uuid
from decimal import Decimal
import asyncpg
import pytest

from cryptonite_execution.reservations import (
    DurableReservationManager,
    InsufficientAvailableBalanceError,
    ReservationNotFoundError,
    ReservationStateError,
)

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"


@pytest.mark.asyncio
async def test_reservation_lifecycle_and_available_balance_formula():
    """Verify available balance deduction across PRE_SEND, INFLIGHT, ACKED, VENUE_REFLECTED, RELEASED."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        env = "DEMO"
        acc = f"ACC-{uuid.uuid4().hex[:8]}"
        asset = "USDT"

        # 1. Initialize trading_balances with 1000 USDT free
        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 1000.0, 0.0, 1725700000000000000, 1725700000000000000);
            """,
            env,
            acc,
            asset,
        )

        mgr = DurableReservationManager(pool)

        # Initially available balance is 1000
        avail = await mgr.get_available_balance(env, acc, asset)
        assert avail == Decimal("1000.0")

        # 2. Acquire reservation for 400 + 10 headroom = 410 required
        cmd_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type) VALUES ($1, $2, 'SUBMIT_ORDER');",
            cmd_id,
            f"idemp-{cmd_id}",
        )
        c_order_id = f"ORD-{uuid.uuid4().hex[:8]}"

        res_id = await mgr.acquire_reservation(
            environment=env,
            account_id=acc,
            command_id=cmd_id,
            client_order_id=c_order_id,
            asset=asset,
            amount=Decimal("400.0"),
            fee_slippage_headroom=Decimal("10.0"),
        )
        assert isinstance(res_id, uuid.UUID)

        # Available balance is now 1000 - 410 = 590
        avail = await mgr.get_available_balance(env, acc, asset)
        assert avail == Decimal("590.0")

        # 3. Transition to INFLIGHT -> available balance remains 590
        await mgr.mark_inflight(res_id)
        avail = await mgr.get_available_balance(env, acc, asset)
        assert avail == Decimal("590.0")

        # 4. Transition to ACKED_UNREFLECTED -> available balance remains 590
        await mgr.mark_acked(res_id)
        avail = await mgr.get_available_balance(env, acc, asset)
        assert avail == Decimal("590.0")

        # 5. Simulate balance snapshot confirming deduction
        snap_id = uuid.uuid4()
        await pool.execute(
            """
            INSERT INTO trading_balance_snapshots (
                snapshot_id, environment, account_id, snapshot_type, account_update_time_ns, observed_at_ns, balances_payload, balances_checksum
            ) VALUES ($1, $2, $3, 'WS_ACCOUNT_UPDATE', 1725700005000000000, 1725700005000000000, '[]'::jsonb, 'dummychk');
            """,
            snap_id,
            env,
            acc,
        )
        # Update trading_balances to reflect locked 400, free 600
        await pool.execute(
            """
            UPDATE trading_balances
            SET free = 600.0, locked = 400.0, account_update_time_ns = 1725700005000000000
            WHERE environment = $1 AND account_id = $2 AND asset = $3;
            """,
            env,
            acc,
            asset,
        )

        # Confirm venue reflection
        await mgr.confirm_venue_reflection(res_id, snap_id)

        # Now reservation is VENUE_REFLECTED: not in active reservation sum!
        # Available balance = free (600) - 0 = 600
        avail = await mgr.get_available_balance(env, acc, asset)
        assert avail == Decimal("600.0")

        # 6. Fill happens -> transition to SETTLEMENT_PENDING
        await mgr.mark_settlement_pending(res_id)

        # 7. Release reservation with provenance
        await mgr.release_reservation(
            res_id,
            release_reason="VENUE_FILLED_SETTLED",
            settlement_snapshot_id=snap_id,
        )

        # Verify database record state
        row = await pool.fetchrow(
            "SELECT state, venue_reflected, release_reason, settlement_snapshot_id, released_at FROM execution_reservations WHERE reservation_id = $1;",
            res_id,
        )
        assert row["state"] == "RELEASED"
        assert row["venue_reflected"] is True
        assert row["release_reason"] == "VENUE_FILLED_SETTLED"
        assert row["settlement_snapshot_id"] == snap_id
        assert row["released_at"] is not None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_insufficient_available_balance_rejection():
    """Verify that acquiring reservation exceeding available balance is rejected."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        env = "DEMO"
        acc = f"ACC-{uuid.uuid4().hex[:8]}"
        asset = "USDT"

        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 100.0, 0.0, 1725700000000000000, 1725700000000000000);
            """,
            env,
            acc,
            asset,
        )

        mgr = DurableReservationManager(pool)
        cmd_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type) VALUES ($1, $2, 'SUBMIT_ORDER');",
            cmd_id,
            f"idemp-{cmd_id}",
        )

        # Attempt to acquire 150 when only 100 is free
        with pytest.raises(InsufficientAvailableBalanceError) as exc_info:
            await mgr.acquire_reservation(
                environment=env,
                account_id=acc,
                command_id=cmd_id,
                client_order_id=f"ORD-{uuid.uuid4().hex[:8]}",
                asset=asset,
                amount=Decimal("150.0"),
            )
        assert "Insufficient available USDT" in str(exc_info.value)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_reservation_provenance_and_illegal_release_rules():
    """Verify strict provenance: venue-reflected reservations cannot be released without terminal snapshot."""
    from cryptonite_execution.reservations import ReservationStateError
    pool = await asyncpg.create_pool(DB_URL)
    try:
        env = "DEMO"
        acc = f"ACC-{uuid.uuid4().hex[:8]}"
        asset = "USDT"

        await pool.execute(
            """
            INSERT INTO trading_balances (environment, account_id, asset, free, locked, account_update_time_ns, observed_at_ns)
            VALUES ($1, $2, $3, 1000.0, 0.0, 1725700000000000000, 1725700000000000000);
            """,
            env,
            acc,
            asset,
        )

        mgr = DurableReservationManager(pool)
        cmd_id = uuid.uuid4()
        await pool.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type) VALUES ($1, $2, 'SUBMIT_ORDER');",
            cmd_id,
            f"idemp-{cmd_id}",
        )

        res_id = await mgr.acquire_reservation(
            environment=env,
            account_id=acc,
            command_id=cmd_id,
            client_order_id=f"ORD-{uuid.uuid4().hex[:8]}",
            asset=asset,
            amount=Decimal("100.0"),
        )

        # 1. Invalid release_reason raises ValueError
        with pytest.raises(ValueError):
            await mgr.release_reservation(res_id, release_reason="INVALID_REASON")

        # 2. VENUE_FILLED_SETTLED without settlement_snapshot_id raises ValueError
        with pytest.raises(ValueError):
            await mgr.release_reservation(res_id, release_reason="VENUE_FILLED_SETTLED", settlement_snapshot_id=None)

        # 3. Unreflected reservation can be released with CANCELLED_UNSENT
        await mgr.release_reservation(res_id, release_reason="CANCELLED_UNSENT")
        row = await pool.fetchrow("SELECT state, venue_reflected, release_reason, settlement_snapshot_id FROM execution_reservations WHERE reservation_id = $1;", res_id)
        assert row["state"] == "RELEASED"
        assert row["venue_reflected"] is False
        assert row["release_reason"] == "CANCELLED_UNSENT"
        assert row["settlement_snapshot_id"] is None

        # 4. Reflected reservation CANNOT be released with unsent reason
        res_id2 = await mgr.acquire_reservation(
            environment=env,
            account_id=acc,
            command_id=cmd_id,
            client_order_id=f"ORD-{uuid.uuid4().hex[:8]}",
            asset=asset,
            amount=Decimal("100.0"),
        )
        await mgr.mark_inflight(res_id2)
        await mgr.mark_settlement_pending(res_id2)
        # Attempt to release with OPERATOR_ABORTED_UNSENT must fail
        with pytest.raises(ReservationStateError):
            await mgr.release_reservation(res_id2, release_reason="OPERATOR_ABORTED_UNSENT")
    finally:
        await pool.close()
