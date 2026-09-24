"""Paper orchestrator handles TRIP_KILL_SWITCH / RESET_SWITCH from the /paper API.

The engine's in-memory switch is what the dispatcher checks; the PAPER row in execution_kill_switch
is the durable copy (persisted on trip/reset, re-latched on restart).
"""

import asyncio
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptonite_orchestrator.server import (
    SimpleQuoteCache,
    command_poller,
    rehydrate_dispatcher_state,
)
from test_bridge_perpetuals_persistence import (  # noqa: F401 - ctx/db_url are fixtures
    ACCOUNT,
    ctx,
    db_url,
)
from test_orchestrator_perp_wiring import make_engine
from test_paper_risk_guardrails import PERP, perp_intent

D = Decimal


def paper_engine():
    engine = make_engine()
    engine.dispatcher.kill_switch = engine.kill_switch  # one switch, as in PaperTradingEngine
    engine.bridge = MagicMock()
    return engine


async def run(engine, commands):
    """Drive command_poller over (command_type, payload) pairs; return the mocked connection."""
    rows = [
        {"command_id": uuid.uuid4(), "command_type": t, "target_instance_id": None, "requester_id": "op",
         "payload": json.dumps(p)}
        for t, p in commands
    ]
    conn = AsyncMock()
    remaining = list(rows)

    async def fake_fetchrow(query, *args):
        if remaining:
            return remaining.pop(0)
        raise asyncio.CancelledError()

    conn.fetchrow = fake_fetchrow
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    quotes = SimpleQuoteCache()
    quotes.set_quote(PERP, 50000.0, 50010.0)
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine, quotes)
    return conn


def outcomes(conn):
    out = []
    for call in conn.execute.call_args_list:
        sql = call.args[0]
        if "UPDATE execution_commands" in sql:
            out.append(("REJECTED", call.args[2]) if "REJECTED" in sql else ("COMPLETED", None))
    return out


ENTRY = ("MANUAL_ORDER", {"instrument_id": "BTCUSDT-PERP", "side": "SELL", "quantity": "0.01", "leverage": 10})


async def test_trip_latches_engine_persists_and_blocks_entries():
    engine = paper_engine()
    conn = await run(engine, [("TRIP_KILL_SWITCH", {"reason": "drill", "operator": "op"}), ENTRY])

    assert engine.kill_switch.is_latched
    conn.fetchval.assert_awaited_once_with("SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", "PAPER-ACCOUNT-001", "drill", "op")
    assert outcomes(conn) == [("COMPLETED", None), ("REJECTED", "KILL_SWITCH_ENGAGED")]
    assert engine.dispatcher.perp_positions == {}


async def test_reset_refused_with_open_position_then_allowed_when_flat():
    engine = paper_engine()
    engine.dispatcher.set_perp_position(PERP, D("-0.01"), D("50000"), leverage=10)
    conn = await run(engine, [("TRIP_KILL_SWITCH", {"reason": "drill"}), ("RESET_SWITCH", {"operator": "op"})])
    assert outcomes(conn)[1] == ("REJECTED", "RESET_REJECTED: open orders=0, open positions=1")
    assert engine.kill_switch.is_latched

    engine.dispatcher.set_perp_position(PERP, D("0"), D("50000"))  # flattened
    conn = await run(engine, [("RESET_SWITCH", {"operator": "op"}), ENTRY])
    assert not engine.kill_switch.is_latched
    assert any("UPDATE execution_kill_switch SET is_latched = false" in c.args[0] and c.args[1:3] == ("PAPER", "PAPER-ACCOUNT-001")
               for c in conn.execute.call_args_list)
    assert outcomes(conn) == [("COMPLETED", None), ("COMPLETED", None)]  # entry fills again after reset
    assert engine.dispatcher.get_perp_position_qty(PERP) == D("-0.01")


async def test_restart_restores_a_latched_paper_kill_switch(ctx):  # noqa: F811 - fixture
    await ctx["pool"].fetchval("SELECT trip_execution_kill_switch('PAPER', $1, 'drill before restart', 'op')", ACCOUNT)
    engine = paper_engine()
    engine.account_id = ACCOUNT
    await rehydrate_dispatcher_state(ctx["pool"], engine)
    assert engine.kill_switch.is_latched
    approved, reason, _ = engine.dispatcher.evaluate_intent(perp_intent("SELL", "0.01"))
    assert (approved, reason) == (False, "KILL_SWITCH_ENGAGED")
