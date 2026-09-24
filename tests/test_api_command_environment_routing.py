"""execution_commands environment routing from the /paper API.

The paper orchestrator claims only environment='PAPER'; the DEMO executor claims 'DEMO'. Manual web
orders must reach the orchestrator, while every other route keeps the column defaults it had
before (DEMO / DEMO-ACCOUNT-001), which is where the executor handles kill-switch commands.
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


async def test_kill_switch_commands_keep_demo_routing():
    request, pool = fake_request()
    await paper.trip_kill_switch(paper.TripKillSwitchRequest(reason="test", operator="op"), request=request, auth={}, csrf={})
    assert inserted_env_and_account(pool) == ("DEMO", "DEMO-ACCOUNT-001")

    request, pool = fake_request()
    await paper.reset_kill_switch(paper.ResetKillSwitchRequest(operator="op"), request=request, auth={}, csrf={})
    assert inserted_env_and_account(pool) == ("DEMO", "DEMO-ACCOUNT-001")
