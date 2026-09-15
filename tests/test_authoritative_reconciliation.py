"""Tests for AuthoritativeReconciler."""

import os
from pathlib import Path
import uuid
import asyncpg
import pytest

from cryptonite_execution.reconciliation import (
    AuthoritativeReconciler,
    UnmanagedOrderDetectedError,
)

DB_URL = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"


@pytest.mark.asyncio
async def test_reconciliation_unmanaged_order_gate_fails_closed(tmp_path):
    """Verify that an unrecognized venue open order trips the fail-closed emergency gate."""
    pool = await asyncpg.create_pool(DB_URL)
    marker_path = tmp_path / "emergency_kill_switch.marker"

    try:
        env = "DEMO"
        acc = f"ACC-{uuid.uuid4().hex[:8]}"

        reconciler = AuthoritativeReconciler(
            pool=pool,
            environment=env,
            account_id=acc,
            emergency_marker_path=str(marker_path),
        )

        # Unmanaged order on venue that does not exist in local database
        venue_orders = [
            {
                "symbol": "BTCUSDT",
                "orderId": 999111222,
                "clientOrderId": "ROGUE-ORDER-001",
                "price": "50000.00",
                "origQty": "0.50",
                "status": "NEW",
            }
        ]

        with pytest.raises(UnmanagedOrderDetectedError) as exc_info:
            await reconciler.reconcile_orders(venue_orders, trigger_type="STARTUP")

        assert "unmanaged open order" in str(exc_info.value)

        # 1. Verify kill switch was latched in PostgreSQL
        ks_row = await pool.fetchrow(
            "SELECT is_latched, latched_by, reason FROM execution_kill_switch WHERE environment = $1 AND account_id = $2;",
            env,
            acc,
        )
        assert ks_row["is_latched"] is True
        assert ks_row["latched_by"] == "RECONCILER"
        assert "unmanaged" in ks_row["reason"]

        # 2. Verify emergency marker file was dropped on disk
        assert marker_path.exists()
        content = marker_path.read_text()
        assert "EMERGENCY_KILL_SWITCH_LATCHED" in content

        # 3. Verify discrepancy logged in reconciliation_differences
        diff_row = await pool.fetchrow(
            """
            SELECT discrepancy_type, action_taken, entity_id 
            FROM reconciliation_differences 
            WHERE entity_id = '999111222';
            """
        )
        assert diff_row is not None
        assert diff_row["discrepancy_type"] == "UNMANAGED_ORDER"
        assert diff_row["action_taken"] == "EMERGENCY_HALT"

        # Cleanup kill switch state for subsequent tests
        await pool.execute(
            "UPDATE execution_kill_switch SET is_latched = false WHERE environment = $1 AND account_id = $2;",
            env,
            acc,
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_reconciliation_balance_snapshot_and_projection():
    """Verify balance reconciliation persists SHA256 checksum snapshot and updates projection."""
    pool = await asyncpg.create_pool(DB_URL)
    try:
        env = "DEMO"
        acc = f"ACC-{uuid.uuid4().hex[:8]}"

        reconciler = AuthoritativeReconciler(pool=pool, environment=env, account_id=acc)

        venue_balances = [
            {"asset": "BTC", "free": "1.50000000", "locked": "0.10000000"},
            {"asset": "USDT", "free": "5000.00000000", "locked": "250.00000000"},
        ]

        # 1. Reconcile balances
        snap_id = await reconciler.reconcile_balances(
            venue_balances=venue_balances,
            account_update_time_ns=1725700010000000000,
            observed_at_ns=1725700010050000000,
        )
        assert isinstance(snap_id, uuid.UUID)

        # 2. Verify snapshot record
        snap_row = await pool.fetchrow(
            "SELECT balances_checksum, snapshot_type FROM trading_balance_snapshots WHERE snapshot_id = $1;",
            snap_id,
        )
        assert snap_row is not None
        assert len(snap_row["balances_checksum"]) == 64  # SHA256 hex string

        # 3. Verify trading_balances updated projection
        btc_row = await pool.fetchrow(
            "SELECT free, locked FROM trading_balances WHERE environment = $1 AND account_id = $2 AND asset = 'BTC';",
            env,
            acc,
        )
        assert btc_row is not None
        assert float(btc_row["free"]) == 1.5
        assert float(btc_row["locked"]) == 0.1

        usdt_row = await pool.fetchrow(
            "SELECT free, locked FROM trading_balances WHERE environment = $1 AND account_id = $2 AND asset = 'USDT';",
            env,
            acc,
        )
        assert usdt_row is not None
        assert float(usdt_row["free"]) == 5000.0
        assert float(usdt_row["locked"]) == 250.0
    finally:
        await pool.close()
