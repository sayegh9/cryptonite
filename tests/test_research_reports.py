"""LE-6: research reports as queryable history: stored through the API (idempotent per id), listed for
the digest, readable by the Brain's read-only role, and the digest built from them."""

import datetime
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptonite_agents.research.queue import build_digest
from cryptonite_api.main import app
from httpx import ASGITransport, AsyncClient
from migrated_db import migrated_database


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_researchtest") as url:
        yield url


def report(status: str, oos_sharpe: float, hours_ago: float = 1) -> dict:
    created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours_ago)
    return {"id": str(uuid.uuid4()), "idea": f"{status} idea", "status": status, "reason": "because",
            "strategy_slug": "ai-gen-x", "strategy_name": f"{status} strategy", "in_sample": {"sharpe_ratio": 1.2},
            "out_of_sample": {"sharpe_ratio": oos_sharpe, "total_trades": 14}, "cost_usd": 0.12,
            "created_at": created.isoformat()}


def body(r: dict) -> dict:
    return {**{k: r[k] for k in r}, "report": r}


async def test_reports_are_stored_once_and_listed_newest_first(db_url, operator_auth):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    app.state.pool = pool
    try:
        promote, reject, old = report("PROMOTE", 1.1), report("REJECT", -0.3, 0.5), report("REVISE", 0.4, 48)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for r in (promote, reject, old):
                assert (await client.post("/api/v1/research/reports", json=body(r))).json()["created"] is True
            again = await client.post("/api/v1/research/reports", json=body(promote))
            assert again.status_code == 201 and again.json()["created"] is False  # a retried worker never duplicates
            bad = await client.post("/api/v1/research/reports", json=body({**promote, "id": str(uuid.uuid4()), "status": "GREAT"}))
            assert bad.status_code == 422
            listed = (await client.get("/api/v1/research/reports", params={"hours": 24})).json()["reports"]
        assert [r["status"] for r in listed] == ["REJECT", "PROMOTE"]  # newest first; the 48 h old one is outside
        digest = build_digest(hours=24, reports=listed)
        assert digest["count"] == 2 and digest["by_status"] == {"PROMOTE": 1, "REJECT": 1}
        assert digest["text"].splitlines()[1].startswith("- [PROMOTE] PROMOTE strategy: OOS Sharpe 1.1")

        parts = urlsplit(db_url)  # the Brain's read-only SQL role can read the history
        ro = urlunsplit(parts._replace(netloc=f"cryptonite_agent_ro:agent_ro_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))
        conn = await asyncpg.connect(ro)
        try:
            assert await conn.fetchval("SELECT count(*) FROM research_reports") == 3
        finally:
            await conn.close()
    finally:
        await pool.close()


async def test_validation_fields_are_stored_and_trials_are_counted(db_url, operator_auth):
    """Phase 5: folds, dsr, holdout and robustness (migration 032) round-trip; earlier trials on the same
    instrument/timeframe are counted for the DSR; a report from before Phase 5 still stores (NULLs)."""
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    app.state.pool = pool
    try:
        validated = report("PROMOTE", 1.4)
        validated.update(folds=[{"window": [1, 2, 3], "sharpe_ratio": 1.1}] * 4, dsr=0.97,
                         holdout={"sharpe_ratio": 0.9, "total_return": 0.02},
                         robustness={"worst_retention": 0.8, "fragile": False})
        full = {**validated, "instrument_id": "SOLUSDT.BINANCE", "timeframe": "1h", "trials": [{}] * 12}
        legacy = report("REJECT", -0.1)
        other_tf = {**legacy, "id": str(uuid.uuid4()), "instrument_id": "SOLUSDT.BINANCE", "timeframe": "4h",
                    "trials": [{}] * 5}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/v1/research/reports", json={**validated, "report": full})).status_code == 201
            assert (await client.post("/api/v1/research/reports", json=body(legacy))).status_code == 201
            assert (await client.post("/api/v1/research/reports", json={**other_tf, "report": other_tf})).status_code == 201
            bad = await client.post("/api/v1/research/reports", json={**body(report("REJECT", 0)), "dsr": 1.5})
            assert bad.status_code == 422
            listed = {r["id"]: r for r in (await client.get("/api/v1/research/reports")).json()["reports"]}
            trials = (await client.get("/api/v1/research/trials",
                                       params={"instrument_id": "SOLUSDT.BINANCE", "timeframe": "1h"})).json()
        v = listed[validated["id"]]
        assert v["dsr"] == 0.97 and len(v["folds"]) == 4 and v["holdout"]["total_return"] == 0.02
        assert v["robustness"]["fragile"] is False
        assert [listed[legacy["id"]][k] for k in ("folds", "dsr", "holdout", "robustness")] == [None] * 4
        assert trials["trials"] == 12  # the 4h report and the reports without an instrument do not count
    finally:
        await pool.close()


def test_contiguous_history_stops_at_the_newest_gap():
    from cryptonite_api.routes.backtests import contiguous_history

    step = 60
    files = [(0, 1_000), (1_060, 2_000), (5_000, 6_000), (6_060, 9_000)]  # a gap between 2_000 and 5_000
    assert contiguous_history(files, step) == (5_000, 9_000)
    assert contiguous_history(files[:2], step) == (0, 2_000)
    assert contiguous_history([], step) is None
