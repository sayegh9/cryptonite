"""agent_safe_trade_timeline (migration 047), read by the Brain as cryptonite_agent_ro.

Runs against a throwaway DB at Alembic head. Each test's seed rows live in a transaction that is rolled back.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from migrated_db import REPO_ROOT, migrated_database

VIEW = "agent_safe_trade_timeline"
COLUMNS = [
    "position_id", "mandate_id", "mandate_name", "instrument_id", "side", "quantity", "entry_price", "opened_at",
    "status", "closed_at", "exit_price", "realized_pnl", "return_pct", "entry_command_id", "entry_source",
    "entry_summary_text", "debate_run_id", "debate_recommendation", "debate_conviction", "close_reason",
    "close_command_id", "partial_exits", "outcome_24h_pct", "outcome_72h_pct",
    "reflection_text",
]

# Every base table the view reads that the agent role was never granted (ai_decision_outcomes already has a SELECT
# grant from migration 028, so it is not in this list). The agent role must have no privilege on any of these.
BASE_TABLES = (
    "paper_positions", "paper_orders", "execution_commands", "ai_approvals", "ai_mandates", "debate_runs",
    "portfolio_decisions", "reflections",
)

XRP = "XRPUSDT.BINANCE"
ACCOUNT = "VIEWTL-PA"
NOW = datetime.now(UTC)


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_viewtl") as url:
        yield url


@pytest.fixture
async def conn(db_url):
    c = await asyncpg.connect(db_url)
    tx = c.transaction()
    await tx.start()
    try:
        yield c
    finally:
        await tx.rollback()
        await c.close()


# --- seeding ---------------------------------------------------------------------------------------------------------

async def mandate(conn, name_hint="tl"):
    mid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO ai_mandates (id, name, capital_budget_usd, instruments, max_position_pct, status,
                                   autonomy_level)
           VALUES ($1, $2, 1000, ARRAY[$3], 10, 'ACTIVE', 2)""",
        mid, f"tl-{name_hint}-{mid.hex[:8]}", XRP,
    )
    return mid


async def account(conn):
    await conn.execute(
        """INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity,
                                       realized_pnl, daily_realized_pnl, daily_loss_latched, updated_at)
           VALUES ($1, 'BINANCE', 'USDT', 1000, 1000, 1000, 0, 0, FALSE, now())""", ACCOUNT,
    )


async def instance(conn, instance_id):
    strategy = uuid.uuid4()
    await conn.execute(
        "INSERT INTO strategies (id, slug, name, description, author) VALUES ($1, $2, $3, 'timeline test', 'tl')",
        strategy, f"tl-{strategy.hex[:12]}", f"TL-{strategy.hex[:12]}",
    )
    await conn.execute(
        "INSERT INTO strategy_versions (id, strategy_id, version_num, code_hash, git_commit, default_parameters, "
        "parameter_schema) VALUES ($1, $1, 1, 'h', 'c', '{}'::jsonb, '{}'::jsonb)", strategy,
    )
    await conn.execute(
        """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
               risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
           VALUES ($1, $2, $3, 'tl', 'tl', 'TL', $4, '1m', '{}'::jsonb, 0, 'ACTIVE')""",
        instance_id, ACCOUNT, strategy, XRP,
    )


def mandate_instance(mid):
    return "AI_MANDATE_" + mid.hex


async def command(conn, command_type, target, payload, requester, created_at, *, status="COMPLETED", mid=None):
    cid = uuid.uuid4()
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id, created_at, processed_at)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, 'PAPER', $8, $9, $10, $10)""",
        cid, f"tl:{cid}", command_type, target, requester, json.dumps(payload), status, ACCOUNT, mid, created_at,
    )
    return cid


async def order(conn, instance_id, client_order_id, instrument, side, created_at, qty, price, status="FILLED"):
    price = Decimal(str(price))  # a float would reach numeric(38,18) as 0.550000000000000044
    await conn.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, filled_qty, avg_fill_price, status, created_at, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'MARKET', $6, $6, $7, $8, $9, $9)""",
        client_order_id, instance_id, ACCOUNT, instrument, side, qty, price, status, created_at,
    )


async def position(conn, instance_id, opened_at, *, qty, entry, realized=0, closed_at=None, side="LONG"):
    pid = f"tl-{uuid.uuid4().hex[:24]}"
    entry = Decimal(str(entry))
    await conn.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, unrealized_pnl, realized_pnl, status, opened_at, closed_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $6, 0, $8, $9, $10, $11)""",
        pid, ACCOUNT, instance_id, XRP, side if closed_at is None else "FLAT", entry, qty if closed_at is None else 0,
        realized, "OPEN" if closed_at is None else "CLOSED", opened_at, closed_at,
    )
    return pid


async def approval(conn, mid, command_id, source, source_ref, summary):
    await conn.execute(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
                                    level_at_creation, timeout_policy, expires_at, status, command_id)
           VALUES ($1, 'ADD_RISK', 'SUBMIT_ORDER', '{}'::jsonb, $2, $3, $4, 2, 'DENY', now() + interval '1 hour',
                   'APPROVED', $5)""",
        mid, summary, source, source_ref, command_id,
    )


async def debate(conn, mid, created_at):
    run = uuid.uuid4()
    await conn.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, mandate_id, decision_trace_id)
           VALUES ($1, $2, 'COMPLETED', 'SCANNER', $3, $4, $5)""",
        run, XRP, created_at, mid, uuid.uuid4(),
    )
    return run


async def decision(conn, run, created_at, recommendation, conviction):
    return await conn.fetchval(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, conviction_level,
                                           allocation_pct, reasoning, decision_price, created_at)
           VALUES ($1, $2, $3, $4, 'HIGH', 5, 'r', 0.5, $5) RETURNING id""",
        run, XRP, recommendation, conviction, created_at,
    )


async def outcome(conn, decision_id, horizon, return_pct):
    await conn.execute(
        """INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars)
           VALUES ($1, $2, $3, 5, -5, 10)""", decision_id, horizon, return_pct,
    )


async def reflection(conn, run, lessons, created_at):
    await conn.execute(
        """INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, created_at)
           VALUES ($1, 1, 0.5, $2, $3)""", run, lessons, created_at,
    )


async def row(conn, position_id):
    return await conn.fetchrow(f"SELECT * FROM {VIEW} WHERE position_id = $1", position_id)


async def closed_trade(conn, requester, command_type, payload, *, opened=NOW - timedelta(days=2)):
    """One brain-card entry, closed by a command of the given requester, type and payload. Returns the position id."""
    await account(conn)
    mid = await mandate(conn, "close")
    target = mandate_instance(mid)
    await instance(conn, target)
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "operator:tester",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=1, closed_at=opened + timedelta(hours=5))
    close = await command(conn, command_type, target, payload, requester, opened + timedelta(hours=4), mid=mid)
    # a SUBMIT_ORDER close fills under its own AI-<command> id, as the orchestrator gives it; a CLOSE_POSITION has none
    closing = f"AI-{close.hex[:12].upper()}" if command_type == "SUBMIT_ORDER" else f"CLOSE-{uuid.uuid4().hex[:8].upper()}"
    await order(conn, target, closing, XRP, "SELL", opened + timedelta(hours=4, seconds=1), 10, 0.6)
    return pid, close


# --- contract: the view exists, the agent role reads it and nothing else --------------------------------------------

async def test_view_exists_with_the_contract_columns_in_order(conn):
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1 "
        "ORDER BY ordinal_position", VIEW,
    )
    assert [r["column_name"] for r in rows] == COLUMNS


async def test_view_is_granted_to_the_agent_role(conn):
    assert await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", VIEW)
    assert await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", VIEW)


async def test_view_runs_with_security_barrier(conn):
    options = await conn.fetchval("SELECT reloptions FROM pg_class WHERE oid = to_regclass($1)", VIEW)
    assert options is not None and "security_barrier=true" in options


@pytest.mark.parametrize("table", BASE_TABLES)
async def test_the_agent_role_has_no_privilege_on_the_base_tables(conn, table):
    assert not await conn.fetchval("SELECT has_table_privilege('cryptonite_agent_ro', $1, 'SELECT')", table)
    assert not await conn.fetchval("SELECT has_any_column_privilege('cryptonite_agent_ro', $1, 'SELECT')", table)


async def test_the_agent_role_reads_the_view_and_nothing_else(conn):
    await conn.execute("SET ROLE cryptonite_agent_ro")
    try:
        await conn.fetch(f"SELECT * FROM {VIEW}")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.fetch("SELECT 1 FROM paper_positions LIMIT 1")
    finally:
        await conn.execute("RESET ROLE")


# --- behaviour: a brain-card trade ---------------------------------------------------------------------------------

async def test_a_brain_card_trade_is_reconstructed_end_to_end(conn):
    await account(conn)
    mid = await mandate(conn, "card")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=3)
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY", "order_type": "MARKET"},
                          "operator:tester", opened - timedelta(seconds=5), mid=mid)
    # A rejected BUY after the filled one must not be taken as the entry
    await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "operator:tester",
                  opened - timedelta(seconds=1), status="REJECTED", mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    summary = "BUY XRPUSDT MARKET at 5% of mandate " + "S" * 400
    await approval(conn, mid, entry, "brain_card", "sid-1:card-1", summary)
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=2, closed_at=opened + timedelta(hours=8))
    close = await command(conn, "CLOSE_POSITION", target, {"instrument_id": XRP, "reason": "HARD_STOP"},
                          "risk:exit_monitor", opened + timedelta(hours=7), mid=mid)
    closing = uuid.uuid4().hex[:8].upper()
    await order(conn, target, f"CLOSE-{closing}", XRP, "SELL", opened + timedelta(hours=7, seconds=1), 10, 0.55)

    r = await row(conn, pid)
    assert r["mandate_id"] == mid and r["mandate_name"].startswith("tl-card-")
    assert r["instrument_id"] == XRP and r["side"] == "LONG"  # the position row reads FLAT once closed
    assert r["quantity"] == Decimal("0") and r["entry_price"] == Decimal("0.5")
    assert r["opened_at"] == opened and r["status"] == "CLOSED" and r["closed_at"] == opened + timedelta(hours=8)
    assert r["exit_price"] == Decimal("0.55")
    assert r["realized_pnl"] == Decimal("2")
    # return on the cost of the entry fill: 2 / (0.5 * 10) = 40 %
    assert r["return_pct"] == Decimal("40")
    assert r["entry_command_id"] == entry
    assert r["entry_source"] == "brain_card"
    assert r["entry_summary_text"] == summary[:300] and len(r["entry_summary_text"]) == 300
    assert r["debate_run_id"] is None and r["debate_recommendation"] is None and r["debate_conviction"] is None
    assert r["close_reason"] == "HARD_STOP" and r["close_command_id"] == close
    assert r["outcome_24h_pct"] is None and r["outcome_72h_pct"] is None and r["reflection_text"] is None


async def test_a_debate_trade_carries_the_latest_verdict_outcomes_and_reflection(conn):
    await account(conn)
    mid = await mandate(conn, "debate")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=5)
    run = await debate(conn, mid, opened - timedelta(minutes=10))
    await decision(conn, run, opened - timedelta(minutes=20), "HOLD", 0.2)
    latest = await decision(conn, run, opened - timedelta(minutes=5), "BUY", 0.81)
    await outcome(conn, latest, "24h", Decimal("1.5"))
    await outcome(conn, latest, "72h", Decimal("-2.25"))
    await reflection(conn, run, "L" * 400, opened + timedelta(days=4))
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "ai:debate_worker",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    await approval(conn, mid, entry, "debate", str(run), "BUY XRPUSDT 5%")
    pid = await position(conn, target, opened, qty=10, entry=0.5)  # still open

    r = await row(conn, pid)
    assert r["entry_source"] == "debate" and r["debate_run_id"] == run
    assert r["debate_recommendation"] == "BUY" and r["debate_conviction"] == Decimal("0.81")
    assert r["outcome_24h_pct"] == Decimal("1.5") and r["outcome_72h_pct"] == Decimal("-2.25")
    assert r["reflection_text"] == "L" * 300
    # an open position has no realized return and no exit yet
    assert r["return_pct"] is None and r["exit_price"] is None and r["close_command_id"] is None
    assert r["close_reason"] is None and r["closed_at"] is None and r["side"] == "LONG"


async def test_a_debate_run_without_a_decision_still_reconstructs_the_entry(conn):
    await account(conn)
    mid = await mandate(conn, "nodecision")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=4)
    run = await debate(conn, mid, opened - timedelta(minutes=10))
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "ai:debate_worker",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    await approval(conn, mid, entry, "debate", str(run), "BUY XRPUSDT 5%")
    pid = await position(conn, target, opened, qty=10, entry=0.5)
    r = await row(conn, pid)
    assert r["debate_run_id"] == run
    assert r["debate_recommendation"] is None and r["debate_conviction"] is None
    assert r["outcome_24h_pct"] is None and r["outcome_72h_pct"] is None


# --- close_reason: from the command that closed the position -------------------------------------------------------

@pytest.mark.parametrize(
    ("requester", "command_type", "payload", "expected"),
    [
        ("risk:exit_monitor", "CLOSE_POSITION", {"instrument_id": XRP, "reason": "HARD_STOP"}, "HARD_STOP"),
        ("risk:exit_monitor", "CLOSE_POSITION", {"instrument_id": XRP, "reason": "TRAILING_STOP"}, "TRAILING_STOP"),
        ("risk:mandate_monitor", "CLOSE_POSITION", {"instrument_id": XRP, "reason": "HARD_STOP"}, "MANDATE_HALT"),
        ("ai:debate_worker", "CLOSE_POSITION", {"instrument_id": XRP, "reason": "EXIT_DEBATE_CLOSE"}, "EXIT_DEBATE"),
        # an L1 exit debate waits for the operator; a timed-out approval is requested as approval:timeout:system
        ("approval:timeout:system", "CLOSE_POSITION",
         {"instrument_id": XRP, "reason": "EXIT_DEBATE_CLOSE", "source": "exit_debate"}, "EXIT_DEBATE"),
        ("approval:web:operator:tester", "CLOSE_POSITION",
         {"instrument_id": XRP, "reason": "EXIT_DEBATE_CLOSE", "source": "exit_debate"}, "EXIT_DEBATE"),
        # an L1 debate SELL is approved on the approval path: the payload's source says it came from a debate
        ("approval:timeout:system", "SUBMIT_ORDER", {"instrument_id": XRP, "side": "SELL", "source": "debate"},
         "EXIT_DEBATE"),
        ("ai:debate_worker", "SUBMIT_ORDER", {"instrument_id": XRP, "side": "SELL", "source": "debate"}, "EXIT_DEBATE"),
        ("web-ui", "CLOSE_POSITION", {"instrument_id": XRP, "price": None}, "MANUAL"),
        ("operator:tester", "SUBMIT_ORDER", {"instrument_id": XRP, "side": "SELL", "source": "brain_order_card"},
         "SELL_CARD"),
        ("approval:web:operator:tester", "SUBMIT_ORDER",
         {"instrument_id": XRP, "side": "SELL", "source": "brain_order_card"}, "SELL_CARD"),
        ("web-ui", "SUBMIT_ORDER", {"instrument_id": XRP, "side": "SELL"}, None),
    ],
)
async def test_close_reason_follows_the_command_that_closed_the_position(conn, requester, command_type, payload,
                                                                         expected):
    pid, close = await closed_trade(conn, requester, command_type, payload)
    r = await row(conn, pid)
    assert r["close_command_id"] == close
    assert r["close_reason"] == expected
    assert r["exit_price"] == Decimal("0.6")  # the first SELL fill after the close command


# --- the close is the latest close command; earlier sells are partial exits -----------------------------------------

async def test_the_close_is_the_latest_close_and_the_earlier_half_sell_is_a_partial_exit(conn):
    await account(conn)
    mid = await mandate(conn, "partial")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=3)
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "operator:tester",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    closed = opened + timedelta(hours=5)
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=1, closed_at=closed)
    # a REDUCE half-close two hours in, then the exit monitor's hard stop
    half = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "SELL", "source": "debate"},
                         "ai:debate_worker", opened + timedelta(hours=2), mid=mid)
    await order(conn, target, f"AI-{half.hex[:12].upper()}", XRP, "SELL", opened + timedelta(hours=2, seconds=1), 5, 0.55)
    stop = await command(conn, "CLOSE_POSITION", target, {"instrument_id": XRP, "reason": "HARD_STOP"},
                         "risk:exit_monitor", opened + timedelta(hours=4), mid=mid)
    await order(conn, target, f"CLOSE-{uuid.uuid4().hex[:8].upper()}", XRP, "SELL", opened + timedelta(hours=4, seconds=1),
                5, 0.58)
    # a SELL after this position closed belongs to a later trade
    await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "SELL"}, "operator:tester",
                  closed + timedelta(minutes=5), mid=mid)

    r = await row(conn, pid)
    assert r["close_command_id"] == stop and r["close_reason"] == "HARD_STOP"
    assert r["partial_exits"] == 1
    assert r["exit_price"] == Decimal("0.58")


async def test_an_open_position_counts_the_sells_it_has_already_made(conn):
    await account(conn)
    mid = await mandate(conn, "openpartial")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=2)
    pid = await position(conn, target, opened, qty=5, entry=0.5)
    await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "SELL", "source": "debate"},
                  "ai:debate_worker", opened + timedelta(hours=1), mid=mid)

    r = await row(conn, pid)
    assert r["close_command_id"] is None and r["close_reason"] is None
    assert r["partial_exits"] == 1


async def test_a_close_from_before_the_position_opened_is_not_its_close(conn):
    await account(conn)
    mid = await mandate(conn, "previous")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=2)
    # the previous trade's hard stop, an hour before this position opened
    await command(conn, "CLOSE_POSITION", target, {"instrument_id": XRP, "reason": "HARD_STOP"}, "risk:exit_monitor",
                  opened - timedelta(hours=1), mid=mid)
    pid = await position(conn, target, opened, qty=1, entry=0.5, realized=0, closed_at=opened + timedelta(hours=3))

    r = await row(conn, pid)
    assert r["close_command_id"] is None and r["close_reason"] is None
    assert r["partial_exits"] == 0


# --- exit_price: the closing fill of that close, never a later position's fill -------------------------------------

async def test_a_close_without_its_own_order_has_no_exit_price_even_if_a_later_sell_fills(conn):
    await account(conn)
    mid = await mandate(conn, "nofill")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=3)
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=0, closed_at=opened + timedelta(hours=5))
    close = await command(conn, "CLOSE_POSITION", target, {"instrument_id": XRP, "reason": "HARD_STOP"},
                          "risk:exit_monitor", opened + timedelta(hours=4), mid=mid)
    # a later position is closed by a SELL that fills after this one closed: it is not this trade's exit
    later = opened + timedelta(hours=6)
    await position(conn, target, later, qty=5, entry=0.6, realized=0, closed_at=later + timedelta(hours=1))
    await order(conn, target, f"CLOSE-{uuid.uuid4().hex[:8].upper()}", XRP, "SELL", later + timedelta(hours=1), 5, 0.9)

    r = await row(conn, pid)
    assert r["close_command_id"] == close and r["close_reason"] == "HARD_STOP"
    assert r["exit_price"] is None


async def test_a_sell_card_close_takes_its_own_fill_not_an_earlier_unrelated_sell(conn):
    await account(conn)
    mid = await mandate(conn, "ownfill")
    target = mandate_instance(mid)
    await instance(conn, target)
    opened = NOW - timedelta(days=3)
    entry = await command(conn, "SUBMIT_ORDER", target, {"instrument_id": XRP, "side": "BUY"}, "operator:tester",
                          opened - timedelta(seconds=5), mid=mid)
    await order(conn, target, f"AI-{entry.hex[:12].upper()}", XRP, "BUY", opened - timedelta(seconds=4), 10, 0.5)
    closed = opened + timedelta(hours=5)
    pid = await position(conn, target, opened, qty=10, entry=0.5, realized=1, closed_at=closed)
    sell = opened + timedelta(hours=4)
    card = await command(conn, "SUBMIT_ORDER", target,
                         {"instrument_id": XRP, "side": "SELL", "source": "brain_order_card"}, "operator:tester",
                         sell, mid=mid)
    await order(conn, target, "OTHER-SELL0001", XRP, "SELL", sell + timedelta(seconds=1), 3, 0.99)  # earlier, not ours
    await order(conn, target, f"AI-{card.hex[:12].upper()}", XRP, "SELL", sell + timedelta(seconds=2), 10, 0.61)

    r = await row(conn, pid)
    assert r["close_command_id"] == card and r["close_reason"] == "SELL_CARD"
    assert r["exit_price"] == Decimal("0.61")


# --- scope: AI-mandate positions of the last 90 days only -----------------------------------------------------------

async def test_only_mandate_positions_opened_in_the_last_90_days_appear(conn):
    await account(conn)
    mid = await mandate(conn, "scope")
    target = mandate_instance(mid)
    await instance(conn, target)
    recent = await position(conn, target, NOW - timedelta(days=89), qty=1, entry=0.5)
    stale = await position(conn, target, NOW - timedelta(days=91), qty=1, entry=0.5, closed_at=NOW - timedelta(days=90))
    legacy_instance = f"AI_DEBATE_VIEWTL{uuid.uuid4().hex[:8]}"  # not a mandate instance
    await instance(conn, legacy_instance)
    legacy = await position(conn, legacy_instance, NOW - timedelta(days=1), qty=1, entry=0.5)
    ids = {r["position_id"] for r in await conn.fetch(f"SELECT position_id FROM {VIEW}")}
    assert recent in ids
    assert stale not in ids
    assert legacy not in ids


# --- migration: downgrade to 046 and back to head ------------------------------------------------------------------

def _alembic(url, *args):
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO_ROOT, env=env, check=True, capture_output=True)


async def _view_exists(url):
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval("SELECT to_regclass($1) IS NOT NULL", VIEW)
    finally:
        await c.close()


def test_downgrade_to_046_drops_the_view_and_upgrade_restores_it():
    # Sync test: migrated_database() and _alembic() own their event loops (asyncio.run)
    with migrated_database("cryptonite_viewtldown") as url:
        assert asyncio.run(_view_exists(url))
        _alembic(url, "downgrade", "046")
        assert not asyncio.run(_view_exists(url))
        _alembic(url, "upgrade", "head")
        assert asyncio.run(_view_exists(url))
