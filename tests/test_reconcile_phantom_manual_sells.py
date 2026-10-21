"""scripts/reconcile_phantom_manual_sells.py: the 2026-10-17 WLD and 2026-10-05 BTC phantom manual sells.

Seeds the fills as the bridge recorded them (prices, quantities and fees from the dev DB) on a throwaway migrated
database, then checks the computed correction, the guarded apply and the audit row.
"""

import argparse
import importlib.util
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from migrated_db import migrated_database, seed_paper_account

D = Decimal
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_phantom_manual_sells.py"
spec = importlib.util.spec_from_file_location("reconcile_phantom_manual_sells", SCRIPT)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

ACC = "PAPER-ACCOUNT-001"
WLD, BTC = "WLDUSDT.BINANCE", "BTCUSDT.BINANCE"
M_WLD = "AI_MANDATE_587a0dda940146cf98c9b3bb0b85bf34"
M_BTC = "AI_MANDATE_a96365877ab0435a84c86e3479f207a7"

# (when, order, instance, instrument, side, price, quantity, commission): the dev DB rows, in fill order
FILLS = [
    ("2026-10-04 08:46:50", "AI-E268ED25760B", M_BTC, BTC, "BUY", "85108", "0.009399821403393336", "0.79999"),
    ("2026-10-05 18:49:53", "MANUAL-5941F7F3", "MANUAL_TRADE", BTC, "SELL", "85603.94", "0.004699", "0.40225291406"),
    ("2026-10-05 22:17:54", "AI-9B5ABBCE89EB", M_BTC, BTC, "BUY", "85971.61", "0.0021557052", "0.18533"),
    ("2026-10-07 19:34:42", "MANUAL-937479E6", "MANUAL_TRADE", BTC, "BUY", "83474.18", "0.01", "0.8347418"),
    ("2026-10-07 19:34:45", "MANUAL-5B346DEB", "MANUAL_TRADE", BTC, "BUY", "83474.18", "0.01", "0.8347418"),
    ("2026-10-07 19:34:50", "MANUAL-FFE86A88", "MANUAL_TRADE", BTC, "BUY", "83474.18", "0.01", "0.8347418"),
    ("2026-10-13 08:58:20", "AI-61D3548A6868", M_BTC, BTC, "SELL", "83632.78", "0.011555526603393335", "0.96643"),
    ("2026-10-17 12:08:10", "CLOSE-C3F1DC87", "MANUAL_TRADE", BTC, "SELL", "81558", "0.03", "2.44674"),
    ("2026-10-17 18:07:04", "AI-2E37EC3CEABA", M_WLD, WLD, "BUY", "0.4964", "1945.7164756953", "0.96585"),
    ("2026-10-17 18:09:49", "MANUAL-CC2A3D1A", "MANUAL_TRADE", WLD, "SELL", "0.4964", "972.858237", "0.4829268288468"),
    ("2026-10-17 18:09:51", "MANUAL-CBEFA431", "MANUAL_TRADE", WLD, "SELL", "0.4963", "972.858237", "0.4828295430231"),
    ("2026-10-17 21:37:56", "CLOSE-98C024C3", M_WLD, WLD, "SELL", "0.4834", "0.0000016953", "0.00000000081950802"),
    ("2026-10-18 18:15:54", "CLOSE-FBF9764E", M_WLD, WLD, "SELL", "0.4695", "1945.716474", "0.913513884543"),
]
WLD_CORRECTION = D("964.7906154980301")  # 2 x 972.858237 at 0.4964 / 0.4963, less their fees
BTC_CORRECTION = D("401.85066114594")  # 0.004699 at 85603.94, less its fee


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_reconcile") as url:
        yield url


@pytest.fixture
async def conn(db_url):
    c = await asyncpg.connect(db_url)
    for t in ("audit_events", "paper_trades", "paper_orders", "paper_positions", "execution_commands"):
        await c.execute(f"DELETE FROM {t}")
    await c.execute("DELETE FROM paper_strategy_instances")
    await c.execute("DELETE FROM paper_accounts WHERE account_id = $1", ACC)
    await seed_paper_account(c, ACC)
    strat = await c.fetchval("SELECT strategy_version_id FROM paper_strategy_instances WHERE instance_id = 'MANUAL_TRADE'")
    for iid in (M_WLD, M_BTC):
        await c.execute(
            """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
                   risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
               VALUES ($1, $2, $3, 'h', 'h', 'mandate', 'SYSTEM.MANDATE', '1m', '{}'::jsonb, 0, 'SYSTEM')""",
            iid, ACC, strat)
    yield c
    await c.close()


async def seed(conn, fills=FILLS) -> Decimal:
    """Book the fills the way the bridge did: cash moves by every fill, positions only by what they hold."""
    cash = await conn.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACC)
    for when, oid, iid, inst, side, px, qty, comm in fills:
        at = datetime.fromisoformat(when).replace(tzinfo=UTC)
        await conn.execute(
            """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
                   quantity, filled_qty, avg_fill_price, status, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, 'MARKET', $6, $6, $7, 'FILLED', $8, $8)""",
            oid, iid, ACC, inst, side, D(qty), D(px), at)
        await conn.execute(
            """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id, side, price,
                   quantity, commission, executed_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            f"TRADE-{oid}", ACC, oid, iid, inst, side, D(px), D(qty), D(comm), at)
        cash += D(qty) * D(px) * (1 if side == "SELL" else -1) - D(comm)
    return await conn.fetchval("UPDATE paper_accounts SET cash_balance = $2, total_equity = $2 WHERE account_id = $1 "
                               "RETURNING cash_balance", ACC, cash)


async def test_the_wld_and_btc_phantom_sells_are_found_and_priced(conn):
    cash = await seed(conn)
    plan = await rec.build_plan(conn, ACC)
    assert plan.problems == []
    assert [(o.client_order_id, o.held, o.over_qty) for o in plan.items] == [
        ("MANUAL-5941F7F3", 0, D("0.004699")),
        ("MANUAL-CC2A3D1A", 0, D("972.858237")), ("MANUAL-CBEFA431", 0, D("972.858237"))]
    # MANUAL_TRADE's own 0.03 BTC (bought 10-07) covers the 10-17 CLOSE: that sale is not flagged
    assert sum(o.net_credit for o in plan.items if o.instrument_id == WLD) == WLD_CORRECTION
    assert sum(o.net_credit for o in plan.items if o.instrument_id == BTC) == BTC_CORRECTION
    assert plan.debit == WLD_CORRECTION + BTC_CORRECTION
    assert plan.cash == cash and plan.others == []


async def test_apply_debits_once_and_names_the_orders(conn):
    cash = await seed(conn)
    counts = "SELECT (SELECT count(*) FROM paper_orders), (SELECT count(*) FROM paper_trades)"
    before = tuple(await conn.fetchrow(counts))
    eid, plan = await rec.apply(conn, ACC, WLD_CORRECTION + BTC_CORRECTION, "test", "seeded 10-17 sequence")

    acc = await conn.fetchrow("SELECT cash_balance, total_equity FROM paper_accounts WHERE account_id = $1", ACC)
    assert acc["cash_balance"] == acc["total_equity"] == cash - WLD_CORRECTION - BTC_CORRECTION
    ev = await conn.fetchrow("SELECT event_type, source, details FROM audit_events WHERE id = $1", eid)
    details = json.loads(ev["details"])
    assert ev["event_type"] == "PHANTOM_SELL_RECONCILED"
    assert [i["client_order_id"] for i in details["items"]] == ["MANUAL-5941F7F3", "MANUAL-CC2A3D1A", "MANUAL-CBEFA431"]
    assert D(details["debit"]) == WLD_CORRECTION + BTC_CORRECTION and D(details["cash_before"]) == cash
    assert tuple(await conn.fetchrow(counts)) == before == (len(FILLS), len(FILLS))

    again = await rec.build_plan(conn, ACC)  # a second run sees them reconciled and has nothing to debit
    assert again.items == [] and {o.reconciled_by for o in again.done} == {eid}
    with pytest.raises(rec.RefusedError, match="nothing to correct"):
        await rec.apply(conn, ACC, D(0), "test", "again")


@pytest.mark.parametrize("blocker", ["open_position", "pending_command", "claimed_command", "wrong_confirm"])
async def test_apply_refuses_and_changes_nothing(conn, blocker):
    cash = await seed(conn)
    confirm = WLD_CORRECTION + BTC_CORRECTION
    if blocker == "open_position":  # an open position, and the fills that opened it
        await seed(conn, [("2026-10-19 10:00:00", "AI-OPEN", M_WLD, WLD, "BUY", "0.5", "10", "0.005")])
        await conn.execute(
            """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
                   quantity, current_price, status) VALUES ('p1', $1, $2, $3, 'LONG', 0.5, 10, 0.5, 'OPEN')""",
            ACC, M_WLD, WLD)
        cash = await conn.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACC)
    elif blocker.endswith("_command"):
        await conn.execute("INSERT INTO execution_commands (idempotency_key, command_type, status) VALUES ('k', 'STOP', $1)",
                           blocker.split("_")[0].upper())
    else:
        confirm = WLD_CORRECTION  # the total moved since the dry run
    with pytest.raises(rec.RefusedError):
        await rec.apply(conn, ACC, confirm, "test", "blocked")
    assert await conn.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACC) == cash
    assert await conn.fetchval("SELECT count(*) FROM audit_events") == 0


async def test_refuses_when_the_mandate_never_sold_the_coins_again(conn):
    await seed(conn, FILLS[8:11])  # the manual sells, but not the mandate's later stop: the account is not short
    plan = await rec.build_plan(conn, ACC)
    assert plan.debit == WLD_CORRECTION
    assert any("over-sold 1945.716474000000000000 but the account is short -0.000001695300000000" in p
               for p in plan.problems)


def test_a_partly_covered_sell_debits_only_the_uncovered_part():
    t = lambda side, qty, px="100", comm="0": {  # noqa: E731
        "trade_id": side + qty, "client_order_id": "o", "instance_id": "MANUAL_TRADE", "instrument_id": BTC,
        "side": side, "price": D(px), "quantity": D(qty), "commission": D(comm), "executed_at": None}
    over, net, held = rec.find_oversells([t("BUY", "0.025"), t("SELL", "0.03", comm="0.003")])
    assert [(o.held, o.over_qty, o.commission_share, o.net_credit) for o in over] == \
        [(D("0.025"), D("0.005"), D("0.0005"), D("0.4995"))]
    assert net[BTC] == D("-0.005") and held[("MANUAL_TRADE", BTC)] == 0


async def test_cli_dry_run_then_apply_with_its_confirm_value(conn, db_url, capsys):
    cash = await seed(conn)
    args = argparse.Namespace(database_url=db_url, account_id=ACC, apply=False, confirm=None, reason="", actor="t")
    assert await rec.main(args) == 0
    confirm = re.search(r"--apply --confirm (\S+)", capsys.readouterr().out).group(1)
    assert D(confirm) == WLD_CORRECTION + BTC_CORRECTION
    assert await conn.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACC) == cash

    args.apply = True
    assert await rec.main(args) == 2  # --apply without --confirm
    args.confirm = confirm
    assert await rec.main(args) == 0
    assert "Restart the orchestrator" in capsys.readouterr().out
    assert await conn.fetchval("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", ACC) == cash - D(confirm)
