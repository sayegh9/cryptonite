"""execution_commands environment routing from the /paper API.

The paper orchestrator claims only environment='PAPER'; the DEMO executor claims 'DEMO'. Manual web
orders and the /paper kill switch must reach the orchestrator (PAPER / PAPER-ACCOUNT-001).
"""

from unittest.mock import AsyncMock, MagicMock

from cryptonite_api.routes import paper


def fake_request():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"command_id": "cmd-1", "command_type": "X", "status": "PENDING"})
    request = MagicMock()
    request.app.state.pool = pool
    return request, pool


def inserted_env_and_account(pool):
    sql, *args = pool.fetchrow.await_args.args
    assert "environment, account_id" in sql
    return args[6], args[7]  # $7, $8


async def test_manual_order_is_enqueued_for_the_paper_orchestrator():
    request, pool = fake_request()
    req = paper.ManualOrderRequest(instrument_id="BTCUSDT.BINANCE", side="BUY", quantity=0.01)
    await paper.submit_manual_order(req, request=request, auth={}, csrf={})
    assert inserted_env_and_account(pool) == ("PAPER", "PAPER-ACCOUNT-001")


async def test_paper_kill_switch_commands_route_to_the_paper_orchestrator():
    request, pool = fake_request()
    await paper.trip_kill_switch(paper.TripKillSwitchRequest(reason="test", operator="op"), request=request, auth={}, csrf={})
    assert inserted_env_and_account(pool) == ("PAPER", "PAPER-ACCOUNT-001")

    request, pool = fake_request()
    await paper.reset_kill_switch(paper.ResetKillSwitchRequest(operator="op"), request=request, auth={}, csrf={})
    assert inserted_env_and_account(pool) == ("PAPER", "PAPER-ACCOUNT-001")


async def test_kill_switch_status_reads_the_paper_row():
    request, pool = fake_request()
    pool.fetchrow = AsyncMock(return_value={"environment": "PAPER", "is_latched": True})
    assert (await paper.get_kill_switch(request=request, auth={}))["is_latched"] is True
    assert "environment = 'PAPER'" in pool.fetchrow.await_args.args[0]
