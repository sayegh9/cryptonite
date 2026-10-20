"""EventBridgeWorker persistence of perpetual fills into paper_positions / paper_trades / paper_accounts.

Fills flow as in the orchestrator: dispatcher.dispatch -> handle_fill -> ORDER_FILLED event with
dispatcher.fill_event_fields -> EventBridgeWorker. Runs against a throwaway DB at Alembic head.
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import asyncpg
import pytest
from cryptonite_execution.bridge import (
    CoalescedMarkToMarketProjector,
    EventBridge,
    EventBridgeWorker,
)
from cryptonite_execution.derivatives import MarginMode, OrderSide
from cryptonite_execution.kill_switch import LatchedKillSwitch
from cryptonite_execution.margin import calculate_liquidation_price
from migrated_db import migrated_database
from test_paper_risk_guardrails import PERP, make_perp_dispatcher, perp_intent

D = Decimal
ACCOUNT = "PAPER-PERP-001"
INSTANCE = "INST-001"  # matches perp_intent
SPOT = "BTCUSDT.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_bridgetest") as url:
        yield url


@pytest.fixture
async def ctx(db_url):
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    strat_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE paper_execution_events, paper_trades, paper_positions, paper_orders, "
            "paper_strategy_instances, paper_accounts CASCADE")
        await conn.execute(
            "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $2, 'test', 'test')",
            strat_id, f"perp-{strat_id.hex[:8]}",
        )
        await conn.execute(
            """INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, parameter_schema)
               VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)""",
            strat_id,
        )
        await conn.execute(
            """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
                                           realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
               VALUES ($1, 'BINANCE', 'USDT', 10000, 10000, 10000, 0, 0, FALSE, now())""",
            ACCOUNT,
        )
        await conn.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
                   risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
               VALUES ($1, $2, $3, 's', 'r', 'Perp', $4, '1m', '{}'::jsonb, 1000, 'RUNNING')""",
            INSTANCE, ACCOUNT, strat_id, PERP,
        )
    bridge = EventBridge(LatchedKillSwitch())
    worker = EventBridgeWorker(bridge, pool, engine_run_id=f"RUN-{uuid.uuid4().hex[:8]}")
    yield {"pool": pool, "bridge": bridge, "worker": worker, "dispatcher": make_perp_dispatcher()}
    await pool.close()


async def fill(ctx, cid, side, qty, price, fee="0", instrument=PERP, **flags):
    """Dispatch + fill in the dispatcher, then persist through the bridge like the orchestrator does."""
    d = ctx["dispatcher"]
    qty, price, fee = D(qty), D(price), D(fee)
    if instrument == PERP:
        ok, status = d.dispatch(perp_intent(side, str(qty), cid, **flags), MagicMock(), MagicMock())
        assert ok, status
    realized = d.handle_fill(cid, qty, price, fee, side=side, instrument_id=instrument)
    await ctx["pool"].execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, price, filled_qty, avg_fill_price, status, last_event_version)
           VALUES ($1, $2, $3, $4, $5, 'MARKET', $6, NULL, $6, $7, 'FILLED', 1)""",
        cid, INSTANCE, ACCOUNT, instrument, side, qty, price,
    )
    ctx["bridge"].enqueue_event({
        "event_type": "ORDER_FILLED", "event_version": 2, "client_order_id": cid, "account_id": ACCOUNT,
        "instance_id": INSTANCE, "instrument_id": instrument, "side": side, "price": str(price),
        "quantity": str(qty), "commission": str(fee), "filled_qty": str(qty), "avg_fill_price": str(price),
        "status": "FILLED", **d.fill_event_fields(instrument, realized, price),
    })
    assert await ctx["worker"].drain_and_commit_batch() == 1


async def positions(ctx, instrument=PERP):
    return await ctx["pool"].fetch(
        "SELECT * FROM paper_positions WHERE instrument_id = $1 ORDER BY opened_at, status DESC", instrument
    )


async def open_position(ctx, instrument=PERP):
    rows = [r for r in await positions(ctx, instrument) if r["status"] == "OPEN"]
    assert len(rows) <= 1
    return rows[0] if rows else None


async def account(ctx):
    return await ctx["pool"].fetchrow("SELECT * FROM paper_accounts WHERE account_id = $1", ACCOUNT)


async def test_short_fill_persists_short_side_positive_quantity_and_margin_fields(ctx):
    await fill(ctx, "S-1", "SELL", "0.04", "50000", fee="2", leverage=10, margin_mode="ISOLATED")

    row = await open_position(ctx)
    assert (row["side"], row["quantity"], row["entry_price"]) == ("SHORT", D("0.04"), D("50000"))
    assert (row["leverage"], row["margin_mode"], row["isolated_margin"]) == (10, "ISOLATED", D("200"))
    expected_liq = calculate_liquidation_price(PERP, OrderSide.SELL, D("0.04"), D("50000"), D("200"), MarginMode.ISOLATED)
    assert row["liquidation_price"] == expected_liq.quantize(D("1e-18"))
    assert row["liquidation_price"] > row["entry_price"]
    assert row["maintenance_margin"] == D("8")  # 2,000 notional * 0.4%

    acct = await account(ctx)
    assert acct["cash_balance"] == D("9998")  # fee only; no notional moves through cash
    trade = await ctx["pool"].fetchrow("SELECT * FROM paper_trades WHERE client_order_id = 'S-1'")
    assert (trade["side"], trade["quantity"], trade["commission"]) == ("SELL", D("0.04"), D("2"))


async def test_closing_fill_transitions_row_to_flat_with_realized_pnl(ctx):
    await fill(ctx, "S-1", "SELL", "0.04", "50000", fee="2", leverage=10)
    await fill(ctx, "C-1", "BUY", "0.04", "49000", fee="1.96", reduce_only=True)

    (row,) = await positions(ctx)
    assert (row["side"], row["quantity"], row["status"]) == ("FLAT", 0, "CLOSED")
    assert row["closed_at"] is not None and row["liquidation_price"] is None
    # 0.04 * (50,000 - 49,000) - 2 opening fee - 1.96 closing fee: the row carries both fees (leverage plan 2.6),
    # so it matches the account's cash change
    assert row["realized_pnl"] == D("36.04")

    acct = await account(ctx)
    assert acct["cash_balance"] == ctx["dispatcher"].cash_balance == D("10036.04")
    assert acct["total_equity"] == acct["cash_balance"]


async def test_long_to_short_flip_closes_long_row_and_opens_short_row(ctx):
    await fill(ctx, "L-1", "BUY", "0.02", "50000", leverage=10)
    await fill(ctx, "F-1", "SELL", "0.05", "51000", fee="2.55", leverage=10)

    closed, opened = await positions(ctx)
    assert (closed["side"], closed["status"], closed["quantity"]) == ("FLAT", "CLOSED", 0)
    assert closed["realized_pnl"] == D("17.45")  # 0.02 * (51,000 - 50,000) - 2.55
    assert (opened["side"], opened["status"]) == ("SHORT", "OPEN")
    assert (opened["quantity"], opened["entry_price"]) == (D("0.03"), D("51000"))
    assert opened["realized_pnl"] == 0


async def test_adding_to_short_updates_average_entry_and_liquidation_price(ctx):
    await fill(ctx, "S-1", "SELL", "0.02", "50000", leverage=10, margin_mode="ISOLATED")
    first_liq = (await open_position(ctx))["liquidation_price"]
    await fill(ctx, "S-2", "SELL", "0.02", "52000", leverage=10, margin_mode="ISOLATED")

    row = await open_position(ctx)
    assert (row["quantity"], row["entry_price"], row["isolated_margin"]) == (D("0.04"), D("51000"), D("204"))
    assert row["liquidation_price"] > first_liq
    assert len(await positions(ctx)) == 1


async def test_cross_position_stores_cross_mode_without_isolated_margin(ctx):
    await fill(ctx, "S-1", "SELL", "0.04", "50000", leverage=10, margin_mode="CROSS")  # the default is ISOLATED now
    row = await open_position(ctx)
    assert (row["margin_mode"], row["isolated_margin"]) == ("CROSS", 0)
    # Cross wallet 10,000 backs the short, so liquidation sits far above entry
    assert row["liquidation_price"] > D("200000")


async def test_account_equity_counts_perp_unrealized_pnl_not_notional(ctx):
    await fill(ctx, "L-1", "BUY", "0.02", "50000", leverage=10)
    await fill(ctx, "L-2", "BUY", "0.02", "52000", leverage=10)  # current_price 52,000, entry 51,000
    acct = await account(ctx)
    assert acct["total_equity"] == acct["cash_balance"] + D("0.04") * (D("52000") - D("51000"))


async def test_spot_fills_keep_long_only_behaviour(ctx):
    await fill(ctx, "SP-1", "BUY", "0.01", "50000", instrument=SPOT)
    row = await open_position(ctx, SPOT)
    assert (row["side"], row["quantity"], row["margin_mode"], row["leverage"]) == ("LONG", D("0.01"), None, 1)
    assert (await account(ctx))["cash_balance"] == D("9500")  # spot moves notional through cash

    await fill(ctx, "SP-2", "SELL", "0.01", "51000", instrument=SPOT)
    (row,) = await positions(ctx, SPOT)
    assert (row["side"], row["status"], row["realized_pnl"]) == ("FLAT", "CLOSED", D("10"))
    assert (await account(ctx))["cash_balance"] == D("10010")


async def test_perp_fill_without_snapshot_leaves_positions_untouched(ctx):
    ctx["bridge"].enqueue_event({
        "event_type": "ORDER_FILLED", "event_version": 2, "client_order_id": "NO-SNAP", "account_id": ACCOUNT,
        "instance_id": INSTANCE, "instrument_id": PERP, "side": "SELL", "price": "50000",
        "quantity": "0.01", "commission": "0.5", "filled_qty": "0.01", "avg_fill_price": "50000", "status": "FILLED",
    })
    await ctx["pool"].execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, filled_qty, status, last_event_version)
           VALUES ('NO-SNAP', $1, $2, $3, 'SELL', 'MARKET', 0.01, 0.01, 'FILLED', 1)""",
        INSTANCE, ACCOUNT, PERP,
    )
    assert await ctx["worker"].drain_and_commit_batch() == 1
    assert await positions(ctx) == []  # no guessed position; the fee still settles
    assert (await account(ctx))["cash_balance"] == D("9999.5")


async def test_mark_to_market_projector_values_shorts_with_correct_sign(ctx):
    await fill(ctx, "S-1", "SELL", "0.04", "50000", leverage=10)
    projector = CoalescedMarkToMarketProjector(ctx["pool"], lambda inst: D("51000") if inst == PERP else None)
    await projector.project_once()
    row = await open_position(ctx)
    assert row["unrealized_pnl"] == D("-40")  # short loses when price rises


async def test_mandate_equity_counts_perp_fees_and_funding_paid(ctx):
    """Leverage plan 2.6: a mandate's equity (and so its drawdown and hard stop) includes the perp fees it paid and
    the funding it paid or received, not only the price move."""
    from types import SimpleNamespace

    from cryptonite_orchestrator.mandate_monitor import mandate_equity

    d = ctx["dispatcher"]
    await fill(ctx, "S-1", "SELL", "0.04", "50000", fee="1", leverage=5, margin_mode="ISOLATED")
    # a negative rate: shorts pay -(-0.04) * 50,000 * -0.0001 = -0.2
    (p,) = d.apply_funding_settlement(PERP, D("50000"), D("-0.0001"), 1_700_000_000_000_000_000)
    key = f"FUND-{p.instrument_id}-{p.funding_time_ns}"
    ctx["bridge"].enqueue_event({
        "event_type": "FUNDING_SETTLED", "event_version": 1, "client_order_id": key, "native_event_id": key,
        "idempotency_key": f"FUNDING:{ACCOUNT}:{PERP}:{p.funding_time_ns}", "account_id": ACCOUNT,
        "instance_id": INSTANCE, "instrument_id": PERP, "cash_flow": str(p.cash_flow),
        "occurred_at_ns": p.funding_time_ns, "payload": p.model_dump(mode="json"),
        "position": d.perp_position_snapshot(PERP, D("50000")),
    })
    assert await ctx["worker"].drain_and_commit_batch() == 1
    m = SimpleNamespace(instance_id=INSTANCE, capital_budget_usd=D("10000"))
    async with ctx["pool"].acquire() as conn:
        equity = await mandate_equity(conn, m)
    assert p.cash_flow == D("-0.2")
    assert equity == D("10000") - D("1") - D("0.2")  # entry fee and funding paid; no price move yet
    assert (await account(ctx))["cash_balance"] == D("10000") - D("1.2")
