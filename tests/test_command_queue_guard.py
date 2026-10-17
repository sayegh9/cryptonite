"""WS-URGENT U-A (part 1): a command whose attempt raises is settled, never left at the head of the queue.

Poller behaviour is driven over scripted connections (as in test_orchestrator_paper_matching); the settlement SQL and
the outbox row run against the throwaway test database.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from connections import DB_URL
from cryptonite_orchestrator import server
from test_orchestrator_paper_matching import SPOT, spot_engine

NORMAL = "INST-QUEUE-GUARD-1"
NORMAL_2 = "INST-QUEUE-GUARD-2"
FAILED_SQL = "command_outcome = 'REJECTED'"  # the settlement that marks a command FAILED
UNKNOWN_SQL = "command_outcome = 'UNKNOWN_PENDING_RECONCILIATION'"


def _row(command_type, payload, *, target=NORMAL, command_id=None, attempt_count=None):
    row = {"command_id": command_id or uuid.uuid4(), "command_type": command_type, "target_instance_id": target,
           "requester_id": "op", "payload": json.dumps(payload)}
    if attempt_count is not None:
        row["attempt_count"] = attempt_count
    return row


def _scripted(rows):
    """fetchrow that hands out ``rows`` in turn, then ends the poller (CancelledError, as the other suites do)."""
    remaining = list(rows)

    async def fetchrow(query, *args):
        if remaining:
            return remaining.pop(0)
        raise asyncio.CancelledError()

    return fetchrow


def _conn(fetchrow, execute=None, fetchval=None):
    conn = AsyncMock()
    conn.fetchrow = fetchrow
    if execute is not None:
        conn.execute.side_effect = execute
    if fetchval is not None:
        conn.fetchval.side_effect = fetchval
    return conn


async def _drive(engine, conn, quotes=None):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    with pytest.raises(asyncio.CancelledError):
        await server.command_poller(pool, engine, quotes or server.SimpleQuoteCache())
    return conn


def _statements(conn):
    """Every SQL text the poller sent, through execute or fetchval, in order."""
    out = []
    for call in conn.execute.call_args_list + conn.fetchval.call_args_list:
        out.append(call.args)
    return out


def _quotes():
    quotes = server.SimpleQuoteCache()
    quotes.set_quote(SPOT, 50000.0, 50010.0)
    return quotes


async def test_a_command_that_raises_once_is_failed_and_the_queue_continues():
    first = _row("RESUME", {"instance_id": NORMAL}, target=NORMAL)
    second = _row("RESUME", {"instance_id": NORMAL_2}, target=NORMAL_2)
    raised = []

    async def execute(sql, *args):
        if "UPDATE paper_strategy_instances" in sql and args[0] == NORMAL and not raised:
            raised.append(sql)
            raise RuntimeError("duplicate key value violates unique constraint uq_active_strategy_per_instrument")

    conn = await _drive(spot_engine(), _conn(_scripted([first, second]), execute=execute))
    assert raised  # the first command really raised
    statements = _statements(conn)
    failed = [a for a in statements if FAILED_SQL in a[0]]
    assert len(failed) == 1 and failed[0][1] == first["command_id"]
    assert failed[0][2].startswith("COMMAND_FAILED: RuntimeError: duplicate key value violates unique constraint")
    assert any("UPDATE paper_strategy_instances" in a[0] and a[1] == NORMAL_2 for a in statements)
    assert any("SET status = 'COMPLETED'" in c.args[0] for c in conn.execute.call_args_list)


async def test_a_transient_error_is_retried_then_failed_after_five_attempts(monkeypatch):
    monkeypatch.setattr(server, "TRANSIENT_RETRY_BACKOFF_S", 0)
    command_id = uuid.uuid4()
    stored, claims, persisted, settled = {"n": 0}, [], [], []

    async def fetchrow(query, *args):
        if settled:
            raise asyncio.CancelledError()
        claims.append(stored["n"] + 1)  # the claim's own increment, as the database would apply it
        return _row("RESUME", {"instance_id": NORMAL}, command_id=command_id, attempt_count=stored["n"] + 1)

    async def execute(sql, *args):
        if "UPDATE paper_strategy_instances" in sql:
            raise ConnectionResetError("connection lost")
        if "SET attempt_count" in sql:
            stored["n"] = args[1]
            persisted.append(args[1])

    async def fetchval(sql, *args):
        if FAILED_SQL in sql:
            settled.append(args)
        return args[0]

    await _drive(spot_engine(), _conn(fetchrow, execute=execute, fetchval=fetchval))

    assert claims == [1, 2, 3, 4, 5]  # five attempts, each a fresh claim of the same command
    assert persisted == [1, 2, 3, 4]  # the count is kept between the retries
    assert len(settled) == 1
    assert settled[0][1] == "COMMAND_FAILED: ConnectionResetError: connection lost"


async def test_a_generic_error_after_an_applied_effect_is_unknown_and_never_failed():
    # The order is dispatched (reserved in memory) before its resting-order insert fails: its effects may exist
    engine = spot_engine()
    payload = {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "quantity": "0.02", "price": "49800"}

    async def execute(sql, *args):
        if "'ACCEPTED'" in sql:
            raise RuntimeError("fk violation on paper_orders")

    conn = await _drive(engine, _conn(_scripted([_row("MANUAL_ORDER", payload, target=None)]), execute=execute),
                        _quotes())

    statements = _statements(conn)
    assert any(UNKNOWN_SQL in a[0] for a in statements)
    assert not any(FAILED_SQL in a[0] for a in statements)
    assert engine.dispatcher.open_orders == {} and engine.dispatcher.reserved_cash == 0  # released, as before


def test_transient_errors_are_the_connection_and_timeout_family_only():
    # asyncio.TimeoutError is TimeoutError on Python 3.11+, so one timeout case covers both names
    for exc in (ConnectionResetError(), ConnectionRefusedError(), TimeoutError(),
                asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"),
                asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients")):
        assert server.is_transient_command_error(exc), exc
    for exc in (RuntimeError("boom"), KeyError("x"), ValueError("bad payload"),
                asyncpg.exceptions.UniqueViolationError("duplicate key value")):
        assert not server.is_transient_command_error(exc), exc


# --- Settlement against the database --------------------------------------------------------------------------------


async def _insert_command(pool, command_id, status, socket_write_started=False):
    await pool.execute(
        "INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, status, "
        "socket_write_started) VALUES ($1, $2, 'SUBMIT_ORDER', 'INST-QUEUE-GUARD-DB', $3, $4)",
        command_id, f"queue-guard-{command_id}", status, socket_write_started,
    )


async def _state(pool, command_id):
    return await pool.fetchrow("SELECT status, command_outcome, error_message, processed_at FROM execution_commands "
                               "WHERE command_id = $1", command_id)


async def _cleanup(pool, command_ids):
    await pool.execute("DELETE FROM notifications_outbox WHERE dedupe_key = ANY($1::text[])",
                       [f"command-failed:{c}" for c in command_ids])
    await pool.execute("DELETE FROM execution_commands WHERE command_id = ANY($1::uuid[])", command_ids)


async def test_a_plain_pending_command_settles_as_failed_with_a_published_row():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    try:
        await _insert_command(pool, command_id, "PENDING")
        decision = await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                                      RuntimeError("boom"), attempt=1, effects_applied=False)
        row = await _state(pool, command_id)
        assert decision == "FAILED"
        assert (row["status"], row["command_outcome"]) == ("FAILED", "REJECTED")
        assert row["error_message"] == "COMMAND_FAILED: RuntimeError: boom"
        assert row["processed_at"] is not None
        alert = await pool.fetchrow("SELECT tier, kind FROM notifications_outbox WHERE dedupe_key = $1",
                                    f"command-failed:{command_id}")
        assert (alert["tier"], alert["kind"]) == ("CRITICAL", "COMMAND_FAILED")
    finally:
        await _cleanup(pool, [command_id])
        await pool.close()


async def test_a_pending_command_whose_effects_may_exist_is_unknown_and_never_failed():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    try:
        await _insert_command(pool, command_id, "PENDING")
        decision = await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                                      RuntimeError("boom"), attempt=1, effects_applied=True)
        row = await _state(pool, command_id)
        assert decision == "UNKNOWN"
        assert (row["status"], row["command_outcome"]) == ("RUNNING", "UNKNOWN_PENDING_RECONCILIATION")
    finally:
        await _cleanup(pool, [command_id])
        await pool.close()


async def test_a_command_with_socket_write_started_is_never_failed():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    try:
        await _insert_command(pool, command_id, "PENDING", socket_write_started=True)
        decision = await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                                      RuntimeError("boom"), attempt=1, effects_applied=False)
        row = await _state(pool, command_id)
        assert decision == "UNKNOWN"
        assert (row["status"], row["command_outcome"]) == ("RUNNING", "UNKNOWN_PENDING_RECONCILIATION")
    finally:
        await _cleanup(pool, [command_id])
        await pool.close()


@pytest.mark.parametrize("status", ["SENDING", "COMPLETED"])
async def test_a_command_that_is_no_longer_pending_is_left_exactly_as_it_is(status):
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    try:
        await _insert_command(pool, command_id, status, socket_write_started=True)
        before = await _state(pool, command_id)
        decision = await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                                      RuntimeError("boom"), attempt=1, effects_applied=False)
        after = await _state(pool, command_id)
        assert decision == "NOOP"
        assert after["status"] == before["status"] == status
        assert after["command_outcome"] is None
        assert await pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE dedupe_key = $1",
                                   f"command-failed:{command_id}") == 0
    finally:
        await _cleanup(pool, [command_id])
        await pool.close()
