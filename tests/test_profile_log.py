"""Pure tests for cryptonite_execution.profile_log.record_decision (no DB)."""

import datetime
import json
import logging
import uuid
from decimal import Decimal

from cryptonite_execution.profile_log import record_decision
from cryptonite_schemas.profile import DecisionStage

EXPECTED_SQL = (
    "INSERT INTO ai_profile_decisions (mandate_id, instrument_id, stage, outcome, reason, detail, run_id, command_id) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)"
)
MANDATE = uuid.UUID("11111111-2222-3333-4444-555555555555")
RUN = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
COMMAND = uuid.UUID("12345678-1234-5678-1234-567812345678")


class FakeConn:
    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple] = []
        self.exc = exc

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self.exc is not None:
            raise self.exc
        return "INSERT 0 1"


async def test_enum_stage_and_uuid_ids_bind_in_order():
    conn = FakeConn()
    ok = await record_decision(conn, MANDATE, DecisionStage.GATE, "PASS", "score ok",
                               instrument_id="BTCUSDT", run_id=RUN, command_id=COMMAND)
    assert ok is True
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert sql == EXPECTED_SQL
    assert args == (MANDATE, "BTCUSDT", "GATE", "PASS", "score ok", "{}", RUN, COMMAND)


async def test_string_stage_and_str_ids_are_coerced():
    conn = FakeConn()
    ok = await record_decision(conn, str(MANDATE), "ORDER", "SENT", None,
                               run_id=str(RUN), command_id=str(COMMAND))
    assert ok is True
    _, args = conn.calls[0]
    assert args[0] == MANDATE and isinstance(args[0], uuid.UUID)
    assert args[2] == "ORDER"
    assert args[4] is None
    assert args[6] == RUN and args[7] == COMMAND


async def test_optional_ids_are_none_when_absent():
    conn = FakeConn()
    await record_decision(conn, MANDATE, "SESSION", "OPEN")
    _, args = conn.calls[0]
    assert args[1] is None
    assert args[4] is None
    assert args[6] is None
    assert args[7] is None


async def test_detail_serialises_decimal_datetime_and_uuid():
    conn = FakeConn()
    when = datetime.datetime(2026, 10, 22, 12, 0, 0, tzinfo=datetime.UTC)
    other = uuid.UUID("99999999-8888-7777-6666-555555555555")
    await record_decision(conn, MANDATE, "FILL", "OK", price=Decimal("64000.50"), at=when, ref=other, qty=3)
    detail_json = conn.calls[0][1][5]
    assert json.loads(detail_json) == {
        "price": "64000.50",
        "at": str(when),
        "ref": str(other),
        "qty": 3,
    }


async def test_outcome_and_instrument_truncated():
    conn = FakeConn()
    long_outcome = "X" * 55
    long_instrument = "I" * 80
    await record_decision(conn, MANDATE, "EXIT", long_outcome, instrument_id=long_instrument)
    _, args = conn.calls[0]
    assert args[3] == "X" * 40
    assert args[1] == "I" * 64


async def test_unknown_stage_returns_false_without_execute(caplog):
    conn = FakeConn()
    with caplog.at_level(logging.WARNING, logger="cryptonite_execution.profile_log"):
        ok = await record_decision(conn, MANDATE, "NOPE", "x")
    assert ok is False
    assert conn.calls == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_execute_raising_returns_false_and_warns(caplog):
    conn = FakeConn(exc=RuntimeError("db down"))
    with caplog.at_level(logging.WARNING, logger="cryptonite_execution.profile_log"):
        ok = await record_decision(conn, MANDATE, DecisionStage.BREAKER, "TRIPPED")
    assert ok is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "profile decision not recorded: db down"


async def test_bad_mandate_id_returns_false_without_raising(caplog):
    conn = FakeConn()
    with caplog.at_level(logging.WARNING, logger="cryptonite_execution.profile_log"):
        ok = await record_decision(conn, "not-a-uuid", "GATE", "PASS")
    assert ok is False
    assert conn.calls == []
    assert any("profile decision not recorded" in r.getMessage() for r in caplog.records)
