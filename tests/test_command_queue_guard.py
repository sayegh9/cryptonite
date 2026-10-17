"""WS-URGENT U-A: a bad command can never block the paper queue; AI-mandate instances take no strategy commands.

Poller behaviour is driven over scripted connections (as in test_orchestrator_paper_matching); the settlement SQL,
the outbox row and the stuck-command alerts run against the throwaway test database.
"""

import asyncio
import contextlib
import json
import time
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from connections import DB_URL
from cryptonite_api.main import create_app
from cryptonite_api.routes import paper
from cryptonite_api.routes.auth import require_csrf, require_operator
from cryptonite_orchestrator import ops_monitor, server
from cryptonite_schemas.mandate import mandate_instance_id
from fastapi.testclient import TestClient
from test_orchestrator_paper_matching import SPOT, spot_engine

D = Decimal
AI = mandate_instance_id(uuid.uuid4())
NORMAL = "INST-QUEUE-GUARD-1"
NORMAL_2 = "INST-QUEUE-GUARD-2"
REFUSAL = "MANDATE_INSTANCE: AI mandates are paused and resumed in Mission Control, not as strategy instances"
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


# --- Poller: refusals, generic errors, transient errors ------------------------------------------------------------


async def test_resume_on_an_ai_mandate_is_failed_and_the_queue_continues():
    resume = _row("RESUME", {"instance_id": AI}, target=AI)
    close = _row("CLOSE_POSITION", {"instrument_id": "BTCUSDT"}, target=AI)
    submit = _row("SUBMIT_ORDER", {"instrument_id": "BTCUSDT", "side": "BUY", "order_type": "MARKET",
                                   "quantity": "0.02"})
    engine = spot_engine()
    conn = await _drive(engine, _conn(_scripted([resume, close, submit])), _quotes())

    refused = [c.args for c in conn.execute.call_args_list if "status = 'FAILED'" in c.args[0]]
    assert len(refused) == 1
    assert refused[0][1] == resume["command_id"]
    assert refused[0][2] == REFUSAL
    assert not any("UPDATE paper_strategy_instances" in c.args[0] for c in conn.execute.call_args_list)
    # the queue moved on: the close of the mandate's (empty) book completes, and the submit is decided by the
    # mandate gate (an AI order without an active mandate), which is a processed outcome, not a stuck one
    assert sum("SET status = 'COMPLETED'" in c.args[0] for c in conn.execute.call_args_list) == 1
    submit_outcomes = [c.args for c in conn.execute.call_args_list if "error_message = $2" in c.args[0]
                       and c.args[1] == submit["command_id"]]
    assert len(submit_outcomes) == 1 and "MANDATE" in str(submit_outcomes[0][2])
    assert engine.dispatcher.get_position_qty(SPOT) == D("0")


@pytest.mark.parametrize("command_type", ["DEPLOY", "PAUSE", "RESUME", "STOP"])
async def test_strategy_commands_on_an_ai_mandate_are_refused_before_any_effect(command_type):
    engine = spot_engine()
    engine.mailbox = MagicMock()
    refused = _row(command_type, {"instance_id": AI}, target=AI)
    conn = await _drive(engine, _conn(_scripted([refused])))

    # the refusal, its INFO outbox row, and nothing else: no instance update, no mailbox command
    assert conn.execute.call_count == 2
    assert conn.execute.call_args_list[0].args[2] == REFUSAL
    alert = conn.execute.call_args_list[1].args
    assert "'COMMAND_REFUSED'" in alert[0] and "'INFO'" in alert[0]
    assert alert[4] == f"command-refused:{refused['command_id']}"
    engine.mailbox.push.assert_not_called()


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


def test_the_refusal_code_is_the_same_in_the_api_and_the_orchestrator():
    assert server.MANDATE_INSTANCE_REFUSAL == REFUSAL
    assert paper.MANDATE_INSTANCE_REFUSAL == REFUSAL


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


# --- The API refuses strategy commands on mandate instances -------------------------------------------------------


@pytest.fixture
def api():
    app = create_app()
    mock_pool = MagicMock()
    mock_pool.fetch = AsyncMock(return_value=[])
    mock_pool.fetchrow = AsyncMock(return_value={"command_id": uuid.uuid4(), "command_type": "PAUSE",
                                                 "status": "PENDING", "created_at": "2026-10-17T00:00:00Z"})
    app.state.pool = mock_pool
    app.dependency_overrides[require_operator] = lambda: {"type": "session", "role": "operator"}
    app.dependency_overrides[require_csrf] = lambda: {"type": "session", "role": "operator"}
    return TestClient(app), mock_pool


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_the_api_refuses_pause_resume_and_stop_on_an_ai_mandate_instance(api, action):
    client, mock_pool = api
    res = client.post(f"/api/v1/paper/strategies/{AI}/{action}")
    assert res.status_code == 409
    assert res.json()["detail"] == REFUSAL
    mock_pool.fetchrow.assert_not_called()


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_the_api_still_takes_pause_resume_and_stop_on_a_normal_instance(api, action):
    client, mock_pool = api
    res = client.post(f"/api/v1/paper/strategies/INST-123/{action}")
    assert res.status_code == 200
    assert "INSERT INTO execution_commands" in mock_pool.fetchrow.call_args.args[0]


def test_the_api_close_position_on_an_ai_mandate_is_unchanged(api):
    client, mock_pool = api  # a stop must still be able to close a mandate's position
    res = client.post(f"/api/v1/paper/strategies/{AI}/close-position", json={"instrument_id": "BTCUSDT.BINANCE"})
    assert res.status_code == 200
    assert mock_pool.fetchrow.call_args.args[4] == AI  # target_instance_id ($4, after the query and two ids)


# --- Stuck-command alerts -------------------------------------------------------------------------------------------


class _PublishRecorder:
    def __init__(self):
        self.published = []

    async def publish(self, channel, message):
        self.published.append(json.loads(message))


async def test_stuck_commands_alert_once_per_command_with_a_stop_at_thirty_seconds():
    pool = await asyncpg.create_pool(DB_URL)
    ids = {name: uuid.uuid4() for name in ("stop", "stuck", "fresh_submit", "fresh_stop", "done")}
    ages = {"stop": (("CLOSE_POSITION", "PENDING"), 40), "stuck": (("SUBMIT_ORDER", "RUNNING"), 130),
            "fresh_submit": (("SUBMIT_ORDER", "PENDING"), 60), "fresh_stop": (("CLOSE_POSITION", "PENDING"), 20),
            "done": (("SUBMIT_ORDER", "COMPLETED"), 900)}
    try:
        for name, ((kind, status), age) in ages.items():
            await pool.execute(
                "INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, "
                "status, environment, created_at) VALUES ($1, $2, $3, 'INST-QUEUE-GUARD-ALERT', $4, 'PAPER', "
                "now() - make_interval(secs => $5::float8))",
                ids[name], f"queue-guard-alert-{ids[name]}", kind, status, float(age))
        redis, alerted = _PublishRecorder(), set()

        first = await ops_monitor.check_stuck_commands(pool, redis, alerted)
        second = await ops_monitor.check_stuck_commands(pool, redis, alerted)

        ours = {str(ids[n]) for n in ("stop", "stuck")}
        assert ours <= set(first) and not ours & set(second)
        assert not {str(ids[n]) for n in ("fresh_submit", "fresh_stop", "done")} & set(first)
        for name in ("stop", "stuck"):
            key = f"stuck-command:{ids[name]}"
            assert await pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE dedupe_key = $1", key) == 1
        mine = [m for m in redis.published if m.get("command_id") in ours]
        assert len(mine) == 2
        stop = next(m for m in mine if m["command_type"] == "CLOSE_POSITION")
        assert stop["message"].startswith("A stop is waiting: CLOSE_POSITION")
        assert "the orchestrator queue may be blocked" in stop["message"]
        assert stop["severity"] == "CRITICAL" and stop["age_s"] >= 30

        await pool.execute("UPDATE execution_commands SET status = 'COMPLETED' WHERE command_id = $1", ids["stop"])
        await ops_monitor.check_stuck_commands(pool, redis, alerted)
        assert str(ids["stop"]) not in alerted  # forgotten once it has settled
    finally:
        await pool.execute("DELETE FROM notifications_outbox WHERE dedupe_key = ANY($1::text[])",
                           [f"stuck-command:{i}" for i in ids.values()])
        await pool.execute("DELETE FROM execution_commands WHERE command_id = ANY($1::uuid[])", list(ids.values()))
        await pool.close()


# --- WS-URGENT round-1 review: transient chain, real connection loss, settlement, UNKNOWN text, stuck scope -----------

UNKNOWN = "UNKNOWN_PENDING_RECONCILIATION"


def _chained(outer: BaseException, inner: BaseException) -> BaseException:
    """``outer`` raised while ``inner`` was being handled, so outer.__context__ is inner (asyncpg's transaction exit
    does exactly this when the connection was already ended by the server)."""
    try:
        raise inner
    except BaseException:
        try:
            raise outer
        except BaseException as error:
            return error


def test_connection_loss_inside_a_transaction_is_transient_through_the_exit_error():
    lost = asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation")
    exit_error = _chained(asyncpg.exceptions.InterfaceError("underlying connection is closed"), lost)
    assert server.is_transient_command_error(exit_error)
    assert server.is_transient_command_error(asyncpg.exceptions.InterfaceError("underlying connection is closed"))
    assert server.is_transient_command_error(asyncpg.exceptions.DeadlockDetectedError("deadlock detected"))
    assert server.is_transient_command_error(asyncpg.exceptions.SerializationError("could not serialize access"))


def test_the_cause_and_context_chain_is_walked_to_a_bounded_depth():
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = ConnectionResetError("reset")
    assert server.is_transient_command_error(wrapped)
    assert not server.is_transient_command_error(
        _chained(RuntimeError("bug"), asyncpg.exceptions.UniqueViolationError("duplicate key value")))

    def nested(levels):
        error = ConnectionResetError("reset")
        for _ in range(levels):
            outer = RuntimeError("wrap")
            outer.__cause__ = error
            error = outer
        return error

    # the transient error at the last level looked at: found; one level deeper: not looked for
    assert server.is_transient_command_error(nested(server.TRANSIENT_ERROR_DEPTH - 1))
    assert not server.is_transient_command_error(nested(server.TRANSIENT_ERROR_DEPTH))


class _KillingConnection:
    """A real connection whose first strategy-instance UPDATE first ends its own backend, as a server-side kill does."""

    def __init__(self, conn, owner):
        self._conn = conn
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql, *args):
        if not self._owner.fired and "UPDATE paper_strategy_instances" in sql:
            self._owner.fired = True
            await self._conn.fetchval("SELECT pg_terminate_backend(pg_backend_pid())")
        return await self._conn.execute(sql, *args)


class _KillOwnBackendOnce:
    def __init__(self, pool):
        self.pool = pool
        self.fired = False

    @contextlib.asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield _KillingConnection(conn, self)


async def test_a_real_connection_the_server_ends_inside_a_transaction_is_retried_not_failed():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    engine = spot_engine()
    engine.mailbox = MagicMock()
    engine.process_mailbox_commands = MagicMock()
    killer = _KillOwnBackendOnce(pool)
    try:
        await pool.execute(  # the oldest PAPER row, so the poller claims it first
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, status, "
            "environment, created_at) VALUES ($1, $2, 'STOP', $3, 'PENDING', 'PAPER', '2000-01-01 00:00:00+00')",
            command_id, f"queue-guard-kill-{command_id}", NORMAL,
        )
        poller = asyncio.create_task(server.command_poller(killer, engine, server.SimpleQuoteCache()))
        try:
            for _ in range(400):  # up to 20 s
                state = await pool.fetchrow("SELECT status, command_outcome, attempt_count FROM execution_commands "
                                            "WHERE command_id = $1", command_id)
                if state["status"] in ("COMPLETED", "FAILED"):
                    break
                await asyncio.sleep(0.05)
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        assert killer.fired  # the backend really was ended inside the transaction
        assert (state["status"], state["command_outcome"]) == ("COMPLETED", "SUCCESS")
        assert state["attempt_count"] == 2  # the first attempt rolled back; the retry completed the command
        assert await pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE dedupe_key = $1",
                                   f"command-failed:{command_id}") == 0
    finally:
        await _sweep(pool, [command_id])
        await pool.close()


async def _insert_queued(pool, command_id, command_type, *, status="PENDING", age_s=0.0, environment="PAPER",
                         outcome=None, instrument=None, socket_write_started=False):
    payload = json.dumps({"instrument_id": instrument} if instrument else {})
    await pool.execute(
        "INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, status, "
        "command_outcome, environment, payload, socket_write_started, created_at) "
        "VALUES ($1, $2, $3, 'INST-QUEUE-GUARD-DB', $4, $5, $6, $7::jsonb, $8, "
        "now() - make_interval(secs => $9::float8))",
        command_id, f"queue-guard-{command_id}", command_type, status, outcome, environment, payload,
        socket_write_started, float(age_s),
    )


async def _sweep(pool, command_ids):
    keys = [f"{prefix}:{c}" for c in command_ids for prefix in ("command-failed", "stuck-command", "command-refused")]
    await pool.execute("DELETE FROM notifications_outbox WHERE dedupe_key = ANY($1::text[])", keys)
    await pool.execute("DELETE FROM execution_commands WHERE command_id = ANY($1::uuid[])", list(command_ids))


def test_settlement_backoff_is_exponential_and_capped():
    assert [server.settle_backoff_s(n) for n in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


async def test_a_settlement_that_keeps_failing_is_closed_by_the_fallback_not_retried_forever(monkeypatch):
    monkeypatch.setattr(server, "SETTLE_BACKOFF_S", 0.0)  # the schedule itself is tested above
    command_id = uuid.uuid4()
    claims, settles, fallbacks = [], [], []

    async def fetchrow(query, *args):
        if fallbacks:
            raise asyncio.CancelledError()
        claims.append(command_id)
        return _row("RESUME", {"instance_id": NORMAL}, command_id=command_id, attempt_count=len(claims))

    async def execute(sql, *args):
        if "UPDATE paper_strategy_instances" in sql:
            raise RuntimeError("duplicate key value violates unique constraint uq_active_strategy_per_instrument")
        if "status = CASE WHEN" in sql:
            fallbacks.append(args)

    async def fetchval(sql, *args):
        settles.append(sql)  # the settlement UPDATE itself fails, as on a row held by a half-open backend
        raise asyncpg.exceptions.LockNotAvailableError("canceling statement due to lock timeout")

    await _drive(spot_engine(), _conn(fetchrow, execute=execute, fetchval=fetchval))
    assert len(settles) == server.SETTLE_ATTEMPTS_BEFORE_FALLBACK == 3
    assert len(claims) == 3  # re-claimed after each failed settlement, never a fourth time
    assert fallbacks == [(command_id, "COMMAND_FAILED: settlement failed", False)]


async def test_the_fallback_closes_a_held_outcome_as_unknown_and_any_other_as_failed():
    pool = await asyncpg.create_pool(DB_URL)
    failed_id, held_id = uuid.uuid4(), uuid.uuid4()
    try:
        await _insert_queued(pool, failed_id, "SUBMIT_ORDER")
        await _insert_queued(pool, held_id, "SUBMIT_ORDER")
        assert await server.fail_command_fallback(pool, failed_id, held=False) == "FAILED"
        assert await server.fail_command_fallback(pool, held_id, held=True) == "UNKNOWN"
        failed, held = await _state(pool, failed_id), await _state(pool, held_id)
        assert (failed["status"], failed["command_outcome"], failed["error_message"]) == (
            "FAILED", "REJECTED", "COMMAND_FAILED: settlement failed")
        assert failed["processed_at"] is not None
        assert (held["status"], held["command_outcome"]) == ("RUNNING", UNKNOWN)
        assert held["processed_at"] is None
    finally:
        await _sweep(pool, [failed_id, held_id])
        await pool.close()


async def test_error_text_is_stripped_of_nul_bytes_and_capped_before_it_is_stored():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    try:
        await _insert_queued(pool, command_id, "SUBMIT_ORDER")
        exc = RuntimeError("bad\x00payload " + "x" * 2000)
        decision = await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB", exc,
                                                      attempt=1, effects_applied=False)
        row = await _state(pool, command_id)
        assert decision == "FAILED"
        assert row["error_message"].startswith("COMMAND_FAILED: RuntimeError: badpayload x")
        assert "\x00" not in row["error_message"]
        assert len(row["error_message"]) <= server.MAX_ERROR_MESSAGE_CHARS
        # the NUL-free alert was written too (a NUL in its jsonb payload would have been refused, silently)
        assert await pool.fetchval("SELECT count(*) FROM notifications_outbox WHERE dedupe_key = $1",
                                   f"command-failed:{command_id}") == 1
    finally:
        await _sweep(pool, [command_id])
        await pool.close()


async def test_a_settlement_waits_two_seconds_for_a_locked_row_then_raises_so_it_is_counted():
    pool = await asyncpg.create_pool(DB_URL)
    holder = await asyncpg.connect(DB_URL)
    command_id = uuid.uuid4()
    tx = holder.transaction()
    try:
        await _insert_queued(pool, command_id, "SUBMIT_ORDER")
        await tx.start()
        await holder.execute("SELECT 1 FROM execution_commands WHERE command_id = $1 FOR UPDATE", command_id)
        started = time.monotonic()
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await server.settle_failed_command(pool, command_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                               RuntimeError("boom"), attempt=1, effects_applied=False)
        assert 1.5 < time.monotonic() - started < 10  # the lock_timeout, not an indefinite wait
    finally:
        await tx.rollback()
        await holder.close()
        await _sweep(pool, [command_id])
        await pool.close()


async def test_an_unknown_close_position_alert_states_the_stop_block_and_points_to_the_runbook():
    pool = await asyncpg.create_pool(DB_URL)
    close_id, submit_id = uuid.uuid4(), uuid.uuid4()
    try:
        await _insert_queued(pool, close_id, "CLOSE_POSITION", instrument="BTCUSDT")
        await _insert_queued(pool, submit_id, "SUBMIT_ORDER")
        decision = await server.settle_failed_command(pool, close_id, "CLOSE_POSITION", "INST-QUEUE-GUARD-DB",
                                                      RuntimeError("fill lost"), attempt=1, effects_applied=True,
                                                      payload={"instrument_id": "BTCUSDT"})
        await server.settle_failed_command(pool, submit_id, "SUBMIT_ORDER", "INST-QUEUE-GUARD-DB",
                                          RuntimeError("fill lost"), attempt=1, effects_applied=True)
        assert decision == "UNKNOWN"
        close_alert = await pool.fetchrow("SELECT title, body FROM notifications_outbox WHERE dedupe_key = $1",
                                          f"command-failed:{close_id}")
        submit_alert = await pool.fetchval("SELECT body FROM notifications_outbox WHERE dedupe_key = $1",
                                           f"command-failed:{submit_id}")
        assert close_alert["title"] == "Paper command outcome unknown"
        assert "stops for BTCUSDT are blocked until this command is resolved" in close_alert["body"]
        assert "docs/runbooks/command-queue.md" in close_alert["body"]
        assert "stops for" not in submit_alert and "docs/runbooks/command-queue.md" in submit_alert
    finally:
        await _sweep(pool, [close_id, submit_id])
        await pool.close()


async def test_stuck_alerts_cover_paper_rows_of_the_last_day_and_an_unknown_close_gets_its_own_text():
    pool = await asyncpg.create_pool(DB_URL)
    ids = {name: uuid.uuid4() for name in ("live", "unknown_close", "old", "demo")}
    try:
        await _insert_queued(pool, ids["live"], "SUBMIT_ORDER", age_s=130)
        await _insert_queued(pool, ids["unknown_close"], "CLOSE_POSITION", status="RUNNING", age_s=3600,
                             outcome=UNKNOWN, instrument="BTCUSDT")
        await _insert_queued(pool, ids["old"], "SUBMIT_ORDER", age_s=25 * 3600)
        await _insert_queued(pool, ids["demo"], "SUBMIT_ORDER", age_s=130, environment="DEMO")
        redis = _PublishRecorder()
        first = await ops_monitor.check_stuck_commands(pool, redis, set())
        ours = {str(ids["live"]), str(ids["unknown_close"])}
        assert ours <= set(first)
        assert not {str(ids["old"]), str(ids["demo"])} & set(first)
        published = {m["command_id"]: m for m in redis.published}
        unknown = published[str(ids["unknown_close"])]
        assert "stops for BTCUSDT are blocked until this command is resolved" in unknown["message"]
        assert "docs/runbooks/command-queue.md" in unknown["message"]
        assert "queue may be blocked" not in unknown["message"]
        assert unknown["outcome"] == UNKNOWN and unknown["severity"] == "CRITICAL"
        assert "queue may be blocked" in published[str(ids["live"])]["message"]
    finally:
        await _sweep(pool, list(ids.values()))
        await pool.close()


async def test_a_first_run_on_history_alerts_on_nothing_older_than_a_day():
    """The first run after deploy must not page for old rows: a fresh database has none, and a database with history
    alerts on the live rows only (every row older than STUCK_COMMAND_WINDOW_H is skipped, however stuck it is)."""
    pool = await asyncpg.create_pool(DB_URL)
    history = [uuid.uuid4() for _ in range(3)]
    try:
        for command_id, (kind, age_days) in zip(history, [("SUBMIT_ORDER", 2), ("CLOSE_POSITION", 10),
                                                          ("RESUME", 40)]):
            await _insert_queued(pool, command_id, kind, status="RUNNING", age_s=age_days * 86400)
        first = await ops_monitor.check_stuck_commands(pool, _PublishRecorder(), set())
        assert not {str(c) for c in history} & set(first)
    finally:
        await _sweep(pool, history)
        await pool.close()


async def test_a_refused_strategy_command_on_an_ai_mandate_is_written_once_and_the_queue_moves_on():
    pool = await asyncpg.create_pool(DB_URL)
    command_id = uuid.uuid4()
    engine = spot_engine()
    engine.mailbox = MagicMock()
    engine.process_mailbox_commands = MagicMock()
    try:
        await pool.execute(  # the oldest PAPER row, so the poller claims it first
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, status, "
            "environment, created_at) VALUES ($1, $2, 'RESUME', $3, 'PENDING', 'PAPER', '2000-01-01 00:00:00+00')",
            command_id, f"queue-guard-refuse-{command_id}", AI,
        )
        poller = asyncio.create_task(server.command_poller(pool, engine, server.SimpleQuoteCache()))
        try:
            for _ in range(200):
                state = await _state(pool, command_id)
                if state["status"] == "FAILED":
                    break
                await asyncio.sleep(0.05)
        finally:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        assert (state["status"], state["command_outcome"], state["error_message"]) == ("FAILED", "REJECTED", REFUSAL)
        alert = await pool.fetch("SELECT tier, kind FROM notifications_outbox WHERE dedupe_key = $1",
                                 f"command-refused:{command_id}")
        assert [(a["tier"], a["kind"]) for a in alert] == [("INFO", "COMMAND_REFUSED")]
        engine.mailbox.push.assert_not_called()
    finally:
        await _sweep(pool, [command_id])
        await pool.close()
