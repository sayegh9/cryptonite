"""Regression: orchestrator startup rehydration must SUM open rows per instrument.

``paper_positions`` can hold several OPEN rows for the same instrument (for example a manual
position and a mandate-owned one). The dispatcher keeps a single account-level quantity per
instrument (spot fills add/subtract it), so rehydration must add the rows together. It used to
assign each row in turn, keeping only the last one, which shrank the sellable position and
rejected valid exits with ``SPOT_INVARIANT_VIOLATION`` (hit live on 2026-10-07: ledger 0.011556
vs dispatcher 0.006857).
"""

import uuid
from decimal import Decimal

from cryptonite_execution.risk import OrderIntent
from cryptonite_orchestrator.server import rehydrate_dispatcher_state
from test_bridge_perpetuals_persistence import (  # noqa: F401 - fixtures
    ACCOUNT,
    INSTANCE,
    ctx,
    db_url,
)
from test_orchestrator_perp_wiring import make_engine

D = Decimal
BTC = "BTCUSDT.BINANCE"


async def open_row(conn, instrument: str, quantity: str, instance: str = INSTANCE) -> None:
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
               entry_price, quantity, current_price, status)
           VALUES ($1, $2, $3, $4, 'LONG', 50000, $5, 50000, 'OPEN')""",
        uuid.uuid4().hex, ACCOUNT, instance, instrument, D(quantity),
    )


def sell(quantity: Decimal, cid: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=cid, instance_id=INSTANCE, instrument_id=BTC,
        side="SELL", order_type="MARKET", quantity=quantity,
    )


async def test_rehydration_sums_multiple_open_rows_for_one_instrument(ctx):  # noqa: F811 - fixture
    pool = ctx["pool"]
    manual, mandate = D("0.03"), D("0.0116")  # the 2026-10-07 live pair
    async with pool.acquire() as conn:
        await open_row(conn, BTC, str(manual))
        await open_row(conn, BTC, str(mandate))

    engine = make_engine()
    engine.account_id = ACCOUNT
    await rehydrate_dispatcher_state(pool, engine)

    total = manual + mandate
    assert engine.dispatcher.get_position_qty(BTC) == total
    assert engine.dispatcher.get_available_position_qty(BTC) == total

    # A sell larger than either single row but within the summed position must be approved.
    # (The bug saw only one row: both 0.03 and 0.0116 are below 0.04, which is below the 0.0416 total.)
    approved, reason, _ = engine.dispatcher.evaluate_intent(sell(D("0.04"), "SELL-REHYDRATE"))
    assert approved is True, reason
    assert reason == "APPROVED"

    # The invariant is preserved: selling the whole sum is allowed, more than the sum is not.
    assert engine.dispatcher.evaluate_intent(sell(total, "SELL-ALL"))[0] is True
    over_ok, over_reason, _ = engine.dispatcher.evaluate_intent(sell(total + D("0.0001"), "SELL-OVER"))
    assert over_ok is False
    assert "SPOT_INVARIANT_VIOLATION" in over_reason


async def test_rehydration_single_row_is_unchanged(ctx):  # noqa: F811 - fixture
    pool = ctx["pool"]
    async with pool.acquire() as conn:
        await open_row(conn, BTC, "0.05")

    engine = make_engine()
    engine.account_id = ACCOUNT
    await rehydrate_dispatcher_state(pool, engine)

    assert engine.dispatcher.get_position_qty(BTC) == D("0.05")
    assert engine.dispatcher.evaluate_intent(sell(D("0.05"), "SELL-ONE"))[0] is True
