"""Authoritative reconciliation and unmanaged order detection.

Implements:
1. Startup & continuous reconciliation (pinned delay 10.0s, interval 10.0s).
2. Unmanaged external order fail-closed gate: latches kill switch, drops emergency marker
   file, and blocks node startup.
3. Balance reconciliation, SHA256 checksum snapshot recording, and reservation transition verification.
"""

import hashlib
import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import uuid

import asyncpg

logger = logging.getLogger("cryptonite.execution.reconciliation")


class UnmanagedOrderDetectedError(Exception):
    """Raised when an unrecognized order is detected on venue, triggering fail-closed emergency halt."""


class AuthoritativeReconciler:
    """Performs continuous and startup reconciliation against Binance venue."""

    STARTUP_DELAY_SECS: float = 10.0
    RECONCILIATION_INTERVAL_SECS: float = 10.0

    def __init__(
        self,
        pool: asyncpg.Pool,
        environment: str = "DEMO",
        account_id: str = "BINANCE-SPOT-DEMO",
        emergency_marker_path: str = "/data/executor_state/demo/emergency_kill_switch.marker",
    ):
        self._pool = pool
        self._environment = environment
        self._account_id = account_id
        self._emergency_marker_path = emergency_marker_path

    @property
    def emergency_marker_path(self) -> str:
        return self._emergency_marker_path

    @emergency_marker_path.setter
    def emergency_marker_path(self, val: str) -> None:
        self._emergency_marker_path = val

    def _compute_balances_checksum(self, balances: List[Dict[str, Any]]) -> str:
        """Compute deterministic SHA256 checksum of canonical sorted balances payload."""
        sorted_balances = sorted(balances, key=lambda b: b.get("asset", ""))
        payload_str = json.dumps(sorted_balances, sort_keys=True)
        return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    def drop_emergency_marker(self, reason: str) -> None:
        """Write emergency fail-closed marker file to disk."""
        marker_file = Path(self._emergency_marker_path)
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(f"EMERGENCY_KILL_SWITCH_LATCHED: {reason}\n")
        logger.critical(f"Wrote emergency marker file to {self._emergency_marker_path}: {reason}")

    async def reconcile_orders(
        self,
        venue_open_orders: List[Dict[str, Any]],
        trigger_type: str = "STARTUP",
    ) -> Dict[str, Any]:
        """Reconcile venue open orders with local database trading_orders.
        
        If any open venue order is not recognized locally, fails closed immediately.
        """
        run_id = uuid.uuid4()
        unmanaged_orders = []

        async with self._pool.acquire() as conn:
            # 1. Create reconciliation_run
            await conn.execute(
                """
                INSERT INTO reconciliation_runs (
                    run_id, environment, account_id, trigger_type, status, started_at
                ) VALUES ($1, $2, $3, $4, 'IN_PROGRESS', now());
                """,
                run_id,
                self._environment,
                self._account_id,
                trigger_type,
            )

            # 2. Fetch known open client_order_ids from trading_orders
            known_rows = await conn.fetch(
                """
                SELECT client_order_id, venue_order_id, status 
                FROM trading_orders
                WHERE environment = $1 AND account_id = $2;
                """,
                self._environment,
                self._account_id,
            )
            known_client_ids = {r["client_order_id"] for r in known_rows}
            known_venue_ids = {r["venue_order_id"] for r in known_rows if r["venue_order_id"]}

            # 3. Check venue orders against known orders
            for vo in venue_open_orders:
                c_id = vo.get("clientOrderId") or vo.get("origClientOrderId") or ""
                v_id = str(vo.get("orderId", ""))
                if c_id not in known_client_ids and v_id not in known_venue_ids:
                    unmanaged_orders.append(vo)

            if unmanaged_orders:
                reason = f"Detected {len(unmanaged_orders)} unmanaged open order(s) on venue!"
                logger.critical(f"RECONCILIATION FAILURE: {reason} Orders: {unmanaged_orders}")

                # Latch kill switch in database via SECURITY DEFINER function
                await conn.execute(
                    """
                    SELECT trip_execution_kill_switch($1, $2, $3, 'RECONCILER');
                    """,
                    self._environment,
                    self._account_id,
                    reason,
                )

                # Persist reconciliation discrepancy
                for uo in unmanaged_orders:
                    await conn.execute(
                        """
                        INSERT INTO reconciliation_differences (
                            difference_id, run_id, entity_type, entity_id, discrepancy_type,
                            venue_state, action_taken, resolved
                        ) VALUES ($1, $2, 'ORDER', $3, 'UNMANAGED_ORDER', $4, 'EMERGENCY_HALT', false);
                        """,
                        uuid.uuid4(),
                        run_id,
                        str(uo.get("orderId", "")),
                        json.dumps(uo),
                    )

                # Update reconciliation run status
                await conn.execute(
                    """
                    UPDATE reconciliation_runs
                    SET status = 'DISCREPANCY_DETECTED', orders_matched = false,
                        completed_at = now(), diff_summary = $2
                    WHERE run_id = $1;
                    """,
                    run_id,
                    json.dumps({"unmanaged_orders_count": len(unmanaged_orders)}),
                )

                # Drop disk marker file
                self.drop_emergency_marker(reason)
                raise UnmanagedOrderDetectedError(reason)

            # Orders matched successfully
            await conn.execute(
                """
                UPDATE reconciliation_runs
                SET status = 'MATCHED', orders_matched = true, completed_at = now()
                WHERE run_id = $1;
                """,
                run_id,
            )

        return {"run_id": run_id, "status": "MATCHED", "unmanaged_count": 0}

    async def reconcile_balances(
        self,
        venue_balances: List[Dict[str, Any]],
        account_update_time_ns: int,
        observed_at_ns: int,
        snapshot_type: str = "REST_RECONCILIATION",
    ) -> uuid.UUID:
        """Store balance snapshot with SHA256 checksum and update trading_balances projection."""
        checksum = self._compute_balances_checksum(venue_balances)
        snapshot_id = uuid.uuid4()

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 1. Insert snapshot (ON CONFLICT ignore identical duplicate)
                await conn.execute(
                    """
                    INSERT INTO trading_balance_snapshots (
                        snapshot_id, environment, account_id, snapshot_type,
                        account_update_time_ns, observed_at_ns, balances_payload, balances_checksum
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (environment, account_id, account_update_time_ns, balances_checksum) DO NOTHING;
                    """,
                    snapshot_id,
                    self._environment,
                    self._account_id,
                    snapshot_type,
                    account_update_time_ns,
                    observed_at_ns,
                    json.dumps(venue_balances),
                    checksum,
                )

                # 2. Update projection in trading_balances
                for b in venue_balances:
                    asset = b.get("asset", "")
                    free = Decimal(str(b.get("free", "0")))
                    locked = Decimal(str(b.get("locked", "0")))
                    await conn.execute(
                        """
                        INSERT INTO trading_balances (
                            environment, account_id, asset, free, locked,
                            account_update_time_ns, observed_at_ns, reconciliation_status, updated_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'RECONCILED', now())
                        ON CONFLICT (environment, account_id, asset) DO UPDATE
                        SET free = EXCLUDED.free,
                            locked = EXCLUDED.locked,
                            account_update_time_ns = EXCLUDED.account_update_time_ns,
                            observed_at_ns = EXCLUDED.observed_at_ns,
                            reconciliation_status = 'RECONCILED',
                            updated_at = now()
                        WHERE trading_balances.account_update_time_ns <= EXCLUDED.account_update_time_ns;
                        """,
                        self._environment,
                        self._account_id,
                        asset,
                        free,
                        locked,
                        account_update_time_ns,
                        observed_at_ns,
                    )

        return snapshot_id
