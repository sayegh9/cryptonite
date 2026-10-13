"""What the AI holds (the Brain's SELL check) and the approval reconciler, against a migrated database (ADR 0004)."""

import uuid
from decimal import Decimal

import asyncpg
import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_agents.brain.holdings import ai_spot_holding
from cryptonite_orchestrator.ops_monitor import reconcile_approvals
from migrated_db import migrated_database

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_holdings") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def fill(pool, instance_id: str, side: str, qty: str, instrument_id: str = BTC) -> None:
    await pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status)
           VALUES ($1, $2, 'PAPER-ACCOUNT-001', $3, $4, 'MARKET', $5::numeric, $5::numeric, 50000, 'FILLED')""",
        f"fill-{uuid.uuid4().hex[:8]}", instance_id, instrument_id, side, qty)


async def approval(pool, mandate_id, command_status: str, error: str | None, approval_status: str = "APPROVED"):
    """An ADD_RISK approval and the paper command it was turned into, in the given states."""
    command_id = await pool.fetchval(
        """INSERT INTO execution_commands (idempotency_key, command_type, payload, status, error_message)
           VALUES ($1, 'SUBMIT_ORDER', '{}'::jsonb, $2, $3) RETURNING command_id""",
        f"idem-{uuid.uuid4().hex}", command_status, error)
    return await pool.fetchval(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at, status, decided_via, decided_by, decided_at, command_id)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, 'BUY BTC', 'test', $2, 2, 'DENY',
                   now() + interval '1 hour', $3, 'web', 'op-1', now(), $4)
           RETURNING id""",
        mandate_id, f"src-{uuid.uuid4().hex}", approval_status, command_id)


# --- ai_spot_holding: what the AI holds, net of its sells ------------------------------------------------------------

async def test_the_holding_is_what_the_mandate_bought_less_what_it_sold(h):
    m = await h.mandate(instruments=(BTC,))
    await fill(h.pool, m.instance_id, "BUY", "0.5")
    await fill(h.pool, m.instance_id, "SELL", "0.2")
    assert await ai_spot_holding(h.pool, BTC) == Decimal("0.3")


async def test_nothing_held_and_manual_positions_are_not_an_ai_holding(h):
    await fill(h.pool, "MANUAL_TRADE", "BUY", "1")
    assert await ai_spot_holding(h.pool, BTC) == Decimal("0")


async def test_the_largest_holding_of_any_mandate_is_the_figure(h):
    a = await h.mandate(instruments=(BTC,))
    b = await h.mandate(instruments=(BTC,))
    await fill(h.pool, a.instance_id, "BUY", "0.2")
    await fill(h.pool, b.instance_id, "BUY", "0.5")
    assert await ai_spot_holding(h.pool, BTC) == Decimal("0.5")


async def test_the_agent_role_reads_the_holding_through_its_view_and_not_the_orders(h):
    # The Brain connects as cryptonite_agent_ro, which may read only agent_safe_* views (migration 014)
    m = await h.mandate(instruments=(BTC,))
    await fill(h.pool, m.instance_id, "BUY", "0.5")
    async with h.pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL ROLE cryptonite_agent_ro")
        assert await ai_spot_holding(conn, BTC) == Decimal("0.5")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.fetch("SELECT 1 FROM paper_orders LIMIT 1")


async def test_a_holding_of_another_instrument_does_not_count(h):
    m = await h.mandate(instruments=(BTC, ETH))
    await fill(h.pool, m.instance_id, "BUY", "2", instrument_id=ETH)
    assert await ai_spot_holding(h.pool, BTC) == Decimal("0")


# --- reconcile_approvals: an approval whose command was refused is not an executed order ----------------------------

async def test_an_approval_whose_command_was_refused_is_cancelled_with_the_reason(h):
    m = await h.mandate(instruments=(BTC,))
    refused = await approval(h.pool, m.id, "REJECTED", "MANDATE_POSITION_NOT_OWNED")

    assert await reconcile_approvals(h.pool) == 1
    row = await h.pool.fetchrow("SELECT status, decided_via, summary FROM ai_approvals WHERE id = $1", refused)
    assert row["status"] == "CANCELLED"
    assert row["decided_via"] == "system"
    assert row["summary"] == "BUY BTC · NOT EXECUTED: REJECTED (MANDATE_POSITION_NOT_OWNED)"


async def test_the_operator_is_told_once_that_an_approved_order_was_not_executed(h):
    m = await h.mandate(instruments=(BTC,))
    refused = await approval(h.pool, m.id, "REJECTED", "MANDATE_SIZING_CONFLICT")

    assert await reconcile_approvals(h.pool) == 1
    assert await reconcile_approvals(h.pool) == 0
    rows = await h.pool.fetch("SELECT tier, kind, body, payload FROM notifications_outbox WHERE dedupe_key = $1",
                              f"not-executed:{refused}")
    assert len(rows) == 1
    assert (rows[0]["tier"], rows[0]["kind"]) == ("INFO", "APPROVED_NOT_EXECUTED")
    assert "NOT EXECUTED: REJECTED (MANDATE_SIZING_CONFLICT)" in rows[0]["body"]


async def test_a_failed_command_is_reconciled_too(h):
    m = await h.mandate(instruments=(BTC,))
    failed = await approval(h.pool, m.id, "FAILED", "exchange timeout")

    assert await reconcile_approvals(h.pool) == 1
    assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", failed) == "CANCELLED"


async def test_executed_pending_and_still_queued_approvals_are_left_alone(h):
    m = await h.mandate(instruments=(BTC,))
    completed = await approval(h.pool, m.id, "COMPLETED", None)
    queued = await approval(h.pool, m.id, "PENDING", None)  # the command is not processed yet
    undecided = await approval(h.pool, m.id, "REJECTED", "x", approval_status="PENDING")  # the operator has not decided

    assert await reconcile_approvals(h.pool) == 0
    for approval_id in (completed, queued, undecided):
        assert await h.pool.fetchval("SELECT status FROM ai_approvals WHERE id = $1", approval_id) != "CANCELLED"


async def test_reconciling_twice_changes_nothing_the_second_time(h):
    m = await h.mandate(instruments=(BTC,))
    await approval(h.pool, m.id, "REJECTED", "MANDATE_POSITION_NOT_OWNED")

    assert await reconcile_approvals(h.pool) == 1
    assert await reconcile_approvals(h.pool) == 0
