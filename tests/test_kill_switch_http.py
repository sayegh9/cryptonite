"""The kill-switch endpoints over HTTP: a status read must name its row, and a missing row is a 503, never armed."""

from contextlib import asynccontextmanager

from cryptonite_api.routes.auth import require_operator
from cryptonite_api.routes.control import router as control_router
from cryptonite_api.routes.paper import router as paper_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeConn:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        return self.row


class FakePool(FakeConn):
    @asynccontextmanager
    async def acquire(self):
        yield self


def client_for(row):
    app = FastAPI()
    app.include_router(control_router)
    app.include_router(paper_router)
    app.dependency_overrides[require_operator] = lambda: {"sub": "operator"}
    app.state.pool = FakePool(row)
    return TestClient(app)


LATCHED = {"is_latched": True, "latch_generation": 3, "updated_at": None, "reason": "halt", "latched_by": "op",
           "latched_at": None, "id": 20, "environment": "PAPER", "account_id": "PAPER-ACCOUNT-001"}


def test_control_status_without_its_row_names_is_a_422_not_a_default():
    resp = client_for(LATCHED).get("/api/v1/control/kill-switch/status")
    assert resp.status_code == 422


def test_control_status_with_both_params_returns_the_row():
    resp = client_for(LATCHED).get("/api/v1/control/kill-switch/status",
                                   params={"environment": "PAPER", "account_id": "PAPER-ACCOUNT-001"})
    assert resp.status_code == 200 and resp.json()["is_latched"] is True


def test_control_status_for_a_missing_row_is_a_503():
    resp = client_for(None).get("/api/v1/control/kill-switch/status",
                                params={"environment": "PAPER", "account_id": "PAPER-ACCOUNT-001"})
    assert resp.status_code == 503


def test_paper_status_for_a_missing_row_is_a_503():
    resp = client_for(None).get("/api/v1/paper/kill-switch")
    assert resp.status_code == 503


def test_paper_status_returns_the_row():
    resp = client_for(LATCHED).get("/api/v1/paper/kill-switch")
    assert resp.status_code == 200 and resp.json()["is_latched"] is True
