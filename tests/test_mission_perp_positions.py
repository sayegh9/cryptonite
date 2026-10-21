"""Leverage plan 4.6: Mission Control shows each perp position's leverage, margin, notional and how far the mark is
from its liquidation price, and the mandate's perp exposure; spot positions carry none of these fields."""

import json
import uuid
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from migrated_db import migrated_database
from test_brain_perp_cards import perp_mandate

PERP = "BTCUSDT-PERP.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_mission_perps") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("TRUNCATE paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def add_position(h, instance_id, inst, side, qty, price, **perp):
    cols = ", ".join(["position_id", "account_id", "instance_id", "instrument_id", "side", "entry_price", "quantity",
                      "current_price", "unrealized_pnl", "status", *perp])
    values = [str(uuid.uuid4()), "PAPER-ACCOUNT-001", instance_id, inst, side, Decimal(price), Decimal(qty),
              Decimal(price), Decimal("0"), "OPEN", *perp.values()]
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    await h.pool.execute(f"INSERT INTO paper_positions ({cols}) VALUES ({marks})", *values)


async def test_a_perp_position_shows_its_liquidation_distance(h):
    m = await perp_mandate(h)
    await add_position(h, m.instance_id, PERP, "LONG", "0.03", "60000", leverage=7, margin_mode="ISOLATED",
                       isolated_margin=Decimal("257.14"), liquidation_price=Decimal("52000"))
    async with h.client() as c:
        res = await c.get("/api/v1/ai/mandates", headers=h.headers)
    assert res.status_code == 200, res.text
    [view] = [v for v in res.json()["mandates"] if v["id"] == str(m.id)]
    [pos] = view["positions"]
    assert (pos["leverage"], pos["margin_mode"], pos["liquidation_price"]) == (7, "ISOLATED", "52000")
    assert pos["notional_usd"] == "1800" and pos["isolated_margin"] == "257.14"
    assert pos["liquidation_distance_pct"] == "13.33"  # (60000 - 52000) / 60000
    assert view["perp_exposure_usd"] == "1800"


async def test_a_spot_position_has_no_perp_fields(h):
    m = await perp_mandate(h)
    await add_position(h, m.instance_id, "BTCUSDT.BINANCE", "LONG", "0.01", "60000")
    async with h.client() as c:
        body = (await c.get("/api/v1/ai/mandates", headers=h.headers)).json()
    [view] = [v for v in body["mandates"] if v["id"] == str(m.id)]
    assert "liquidation_distance_pct" not in view["positions"][0] and view["perp_exposure_usd"] == "0"


async def test_flatten_perps_needs_totp_and_closes_only_perps(h):
    """Leverage plan 4.7: one CLOSE_POSITION per open perp, to the instance that holds it; spot is left alone."""
    m = await perp_mandate(h)
    await add_position(h, m.instance_id, PERP, "LONG", "0.03", "60000", leverage=7, margin_mode="ISOLATED",
                       isolated_margin=Decimal("257.14"), liquidation_price=Decimal("52000"))
    await add_position(h, "MANUAL_TRADE", "ETHUSDT-PERP.BINANCE", "SHORT", "1", "3000", leverage=3,
                       margin_mode="CROSS", isolated_margin=Decimal("0"), liquidation_price=Decimal("3900"))
    await add_position(h, "MANUAL_TRADE", "BTCUSDT.BINANCE", "LONG", "0.01", "60000")
    await h.pool.execute("TRUNCATE execution_commands CASCADE")
    async with h.client() as c:
        res = await c.post("/api/v1/paper/perps/flatten", headers=h.headers, json={})
        assert res.status_code == 401 and res.json()["detail"].startswith("TOTP_REQUIRED")
        assert await h.pool.fetchval("SELECT count(*) FROM execution_commands") == 0
        res = await c.post("/api/v1/paper/perps/flatten", headers=h.headers, json={"totp_code": h.totp.now()})
    assert res.status_code == 200, res.text
    assert res.json()["closed"] == 2
    rows = await h.pool.fetch("SELECT command_type, target_instance_id, payload FROM execution_commands ORDER BY payload->>'instrument_id'")
    got = [(r["command_type"], r["target_instance_id"], json.loads(r["payload"])["instrument_id"]) for r in rows]
    assert got == [("CLOSE_POSITION", m.instance_id, PERP), ("CLOSE_POSITION", "MANUAL_TRADE", "ETHUSDT-PERP.BINANCE")]
