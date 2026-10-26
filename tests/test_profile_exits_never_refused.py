"""Auto-Trade Profiles P0: an exit is never refused by the mandate gate, whatever the profile's status, level or the
master switch. Real migrated Postgres through ``gate_ai_order``; an ADD_RISK in the same state is refused."""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ai_api_harness import ACCOUNT, close_harness, make_harness
from cryptonite_execution import profile_lifecycle as pl
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.mandate_gate import gate_ai_order
from cryptonite_schemas.mandate import MandateReject
from migrated_db import migrated_database

D = Decimal
SPOT = "BTCUSDT.BINANCE"
PERP = "BTCUSDT-PERP.BINANCE"
NEVER = {MandateReject.APPROVAL_REQUIRED.value, MandateReject.HALTED.value, MandateReject.COIN_LOCKED.value}


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p0_exits") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    pl.reset_switch_cache()
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED', name = id::text")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("DELETE FROM system_settings WHERE key LIKE 'autotrade.%'")
    await harness.pool.execute("TRUNCATE ai_approvals, execution_commands, paper_orders, paper_positions CASCADE")
    try:
        yield harness
    finally:
        pl.reset_switch_cache()
        await close_harness(harness)


async def set_switch(h, on: bool) -> None:
    await h.pool.execute(
        """INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps(1 if on else 0))
    pl.reset_switch_cache()


def intent(m, instrument, side, *, reduce_only=False):
    return OrderIntent(client_order_id=uuid.uuid4().hex, instance_id=m.instance_id, instrument_id=instrument,
                       side=side, order_type="LIMIT", quantity=D("0.01"), price=D("50000"), reduce_only=reduce_only,
                       stop_loss=D("49000") if instrument == SPOT and side == "BUY" else None)


async def gate(h, m, i, engine):
    async with h.pool.acquire() as conn:
        fresh = await load_mandate(conn, m.id)
        quote = SimpleNamespace(ask_price=D("50000")) if i.instrument_id == SPOT and i.side == "BUY" else None
        return await gate_ai_order(conn, engine, {"command_id": uuid.uuid4()}, {}, i, fresh, quote)


@pytest.mark.parametrize("level", [1, 3])
@pytest.mark.parametrize("switch_on", [False, True])
@pytest.mark.parametrize("status", ["PAUSED", "STOPPING", "HALTED"])
async def test_exits_are_accepted_and_entries_refused(h, status, switch_on, level):
    m = await h.mandate(level, instruments=(SPOT, PERP), status=status)
    await set_switch(h, switch_on)
    # a spot holding (net filled BUY) and a perp long owned by the mandate's instance
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, quantity,
               filled_qty, avg_fill_price, status) VALUES ($1, $2, $3, $4, 'BUY', 'MARKET', 0.01, 0.01, 50000, 'FILLED')""",
        uuid.uuid4().hex, m.instance_id, ACCOUNT, SPOT)
    perp_pos = SimpleNamespace(instance_id=m.instance_id, signed_qty=D("0.01"), entry_price=D("50000"))
    engine = SimpleNamespace(dispatcher=SimpleNamespace(perp_positions={PERP: perp_pos}, mark_prices={}, open_orders={}),
                             account_id=ACCOUNT)

    for exit_order in (intent(m, SPOT, "SELL"),  # REDUCE_RISK / CLOSE_POSITION of a spot holding
                       intent(m, PERP, "SELL", reduce_only=True)):  # reduce-only close of a perp long
        reject = await gate(h, m, exit_order, engine)
        assert reject not in NEVER, (exit_order.instrument_id, reject)
        assert reject is None, (exit_order.instrument_id, reject)

    for entry in (intent(m, SPOT, "BUY"), intent(m, PERP, "BUY")):  # ADD_RISK
        assert await gate(h, m, entry, engine) == MandateReject.HALTED.value, entry.instrument_id
