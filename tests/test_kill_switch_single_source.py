"""The PAPER kill switch is read from one place, and a missing row is never reported as armed."""

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptonite_api.kill_switch import PAPER_ACCOUNT_ID, PAPER_ENVIRONMENT, read_kill_switch
from cryptonite_api.routes.control import get_kill_switch_status
from cryptonite_api.routes.paper import get_kill_switch
from fastapi import HTTPException

LATCHED = {
    "id": 20,
    "environment": "PAPER",
    "account_id": "PAPER-ACCOUNT-001",
    "is_latched": True,
    "latch_generation": 3,
    "reason": "manual halt",
    "latched_by": "operator",
    "latched_at": datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
    "updated_at": datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
}


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(args)
        return self.row


class FakePool(FakeConn):
    @asynccontextmanager
    async def acquire(self):
        yield self


def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


def test_the_paper_key_is_the_one_row_the_three_readers_share():
    assert (PAPER_ENVIRONMENT, PAPER_ACCOUNT_ID) == ("PAPER", "PAPER-ACCOUNT-001")


@pytest.mark.asyncio
async def test_read_keys_the_row_by_environment_and_account():
    conn = FakeConn(LATCHED)
    row = await read_kill_switch(conn, "PAPER", "PAPER-ACCOUNT-001")
    assert conn.calls == [("PAPER", "PAPER-ACCOUNT-001")]
    assert row["is_latched"] is True


@pytest.mark.asyncio
async def test_read_returns_none_for_a_missing_row():
    assert await read_kill_switch(FakeConn(None), "PAPER", "PAPER-ACCOUNT-001") is None


def test_control_status_names_its_row_explicitly():
    params = inspect.signature(get_kill_switch_status).parameters
    assert params["environment"].default is inspect.Parameter.empty
    assert params["account_id"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_control_status_reports_the_named_row():
    pool = FakePool(LATCHED)
    out = await get_kill_switch_status(_request(pool), environment="PAPER", account_id="PAPER-ACCOUNT-001", auth={})
    assert pool.calls == [("PAPER", "PAPER-ACCOUNT-001")]
    assert out == {
        "environment": "PAPER",
        "account_id": "PAPER-ACCOUNT-001",
        "is_latched": True,
        "latch_generation": 3,
        "updated_at": "2026-10-10T09:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_control_status_refuses_a_missing_row_instead_of_reporting_armed():
    with pytest.raises(HTTPException) as exc:
        await get_kill_switch_status(_request(FakePool(None)), environment="PAPER", account_id="PAPER-ACCOUNT-001", auth={})
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_paper_status_returns_the_shared_row():
    out = await get_kill_switch(_request(FakePool(LATCHED)), auth={})
    assert out["is_latched"] is True and out["id"] == 20


@pytest.mark.asyncio
async def test_paper_status_refuses_a_missing_row_instead_of_reporting_armed():
    with pytest.raises(HTTPException) as exc:
        await get_kill_switch(_request(FakePool(None)), auth={})
    assert exc.value.status_code == 503
