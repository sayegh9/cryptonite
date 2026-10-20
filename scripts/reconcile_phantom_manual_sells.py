"""Take back the cash that MANUAL_TRADE spot SELLs credited for coins MANUAL_TRADE did not hold (PR #102).

Before PR #102 the dispatcher checked a manual SELL against one account-level quantity per instrument, so a manual
SELL could sell coins an AI mandate owned. The bridge credited the proceeds but found no MANUAL_TRADE position to
reduce; the mandate's position stayed open and was later sold again (its stop or close). The coins were sold twice,
the account is net short on spot, and cash carries the first sale's proceeds. Known cases: WLD 2026-10-17 18:09
(2 x 972.858237, mandate 587a0dda) and BTC 2026-10-05 18:49 (0.004699, mandate a9636587).

How it finds them: replay each instance's spot fills (paper_trades, in time order) the way the bridge keeps
paper_positions. A SELL fills at most what the instance holds; the rest is over-sold. The correction for that
part is its proceeds net of its share of the commission, exactly what the bridge credited for it. It is debited
only where the account as a whole sold that many more coins than it bought on the instrument (the double sale).

Dry run (prints the report and the --confirm value, changes nothing):
    uv run python scripts/reconcile_phantom_manual_sells.py --database-url "$DEV_DB_URL"
Apply (every position flat, no command in flight; --confirm is the debit the dry run printed):
    uv run python scripts/reconcile_phantom_manual_sells.py --database-url "$DEV_DB_URL" --apply --confirm 1366.64...

--apply debits paper_accounts.cash_balance (and total_equity) in one transaction and writes one audit_events row
(PHANTOM_SELL_RECONCILED) naming every order and trade. paper_orders and paper_trades are never changed or deleted;
already-reconciled trades are skipped on a later run. Take a backup first (scripts/backup.sh db), and restart the
orchestrator afterwards: it loads cash from the DB only at startup.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, localcontext

import asyncpg

EVENT_TYPE = "PHANTOM_SELL_RECONCILED"
SOURCE = "scripts/reconcile_phantom_manual_sells.py"
MANUAL = "MANUAL_TRADE"
Q = Decimal("1e-18")  # numeric(38,18)
DUST = Decimal("1e-12")  # rounding left by sizing (a963 BTC kept 1e-18 after its 10-13 sale; its row was swept)
ACTIVE_COMMANDS = ("PENDING", "CLAIMED", "RUNNING", "SENDING")
DONE_ORDERS = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "DENIED")

TRADES_SQL = """
    SELECT t.trade_id, t.client_order_id, t.instance_id, t.instrument_id, t.side, t.price, t.quantity,
           t.commission, t.executed_at
    FROM paper_trades t
    WHERE t.account_id = $1 AND t.instrument_id NOT LIKE '%-PERP.%'
    ORDER BY t.executed_at, t.trade_id
"""
RECONCILED_SQL = """
    SELECT e.id, item->>'trade_id' AS trade_id
    FROM audit_events e, jsonb_array_elements(e.details->'items') item
    WHERE e.event_type = $1 AND e.details->>'account_id' = $2
"""


@dataclass
class OverSell:
    trade_id: str
    client_order_id: str
    instance_id: str
    instrument_id: str
    executed_at: object
    price: Decimal
    quantity: Decimal
    held: Decimal
    over_qty: Decimal
    commission_share: Decimal
    net_credit: Decimal
    reconciled_by: int | None = None


def find_oversells(trades) -> tuple[list[OverSell], dict[str, Decimal], dict[tuple[str, str], Decimal]]:
    """Replay spot fills per (instance, instrument) as the bridge does; return over-sells, each instrument's
    account-level net quantity (bought - sold, all instances) and what each instance holds at the end."""
    held: dict[tuple[str, str], Decimal] = {}
    account_net: dict[str, Decimal] = {}
    out: list[OverSell] = []
    with localcontext() as ctx:
        ctx.prec = 60
        for t in trades:
            key = (t["instance_id"], t["instrument_id"])
            qty, px, comm = Decimal(t["quantity"]), Decimal(t["price"]), Decimal(t["commission"])
            have = held.get(key, Decimal(0))
            sign = 1 if t["side"] == "BUY" else -1
            account_net[t["instrument_id"]] = account_net.get(t["instrument_id"], Decimal(0)) + sign * qty
            if t["side"] == "BUY":
                held[key] = have + qty
                continue
            filled = min(qty, have)
            held[key] = have - filled
            over = qty - filled
            if over > 0:
                share = (comm * over / qty).quantize(Q)
                out.append(OverSell(t["trade_id"], t["client_order_id"], t["instance_id"], t["instrument_id"],
                                    t["executed_at"], px, qty, have, over, share, (over * px).quantize(Q) - share))
    return out, account_net, held


@dataclass
class Plan:
    cash: Decimal
    equity: Decimal
    ledger: Decimal  # starting + realized_pnl - spot BUY fees: what cash should be once flat
    items: list[OverSell]  # MANUAL_TRADE over-sells not yet reconciled
    done: list[OverSell]  # MANUAL_TRADE over-sells an earlier run reconciled
    others: list[OverSell]  # over-sells by any other instance: not this script's to correct
    account_net: dict[str, Decimal]
    problems: list[str]

    @property
    def debit(self) -> Decimal:
        return sum((i.net_credit for i in self.items), Decimal(0))


async def build_plan(conn, account_id: str) -> Plan:
    acc = await conn.fetchrow(
        """SELECT cash_balance, total_equity, starting_balance + realized_pnl - COALESCE((
               SELECT sum(commission) FROM paper_trades WHERE account_id = $1 AND side = 'BUY'
                   AND instrument_id NOT LIKE '%-PERP.%'), 0) AS ledger
           FROM paper_accounts WHERE account_id = $1""", account_id)
    if acc is None:
        raise SystemExit(f"no paper account {account_id}")
    oversells, account_net, held = find_oversells(await conn.fetch(TRADES_SQL, account_id))
    reconciled = {r["trade_id"]: r["id"] for r in await conn.fetch(RECONCILED_SQL, EVENT_TYPE, account_id)}
    manual = [o for o in oversells if o.instance_id == MANUAL]
    for o in manual:
        o.reconciled_by = reconciled.get(o.trade_id)
    plan = Plan(Decimal(acc["cash_balance"]), Decimal(acc["total_equity"]), Decimal(acc["ledger"]),
                [o for o in manual if o.reconciled_by is None], [o for o in manual if o.reconciled_by is not None],
                [o for o in oversells if o.instance_id != MANUAL], account_net, [])

    # The coins must have been sold twice: on each instrument the account sold exactly the over-sold quantity more
    # than it ever bought. Anything else (a mandate still holding them, a partial history) needs a person.
    for inst in sorted({o.instrument_id for o in manual}):
        over = sum((o.over_qty for o in manual if o.instrument_id == inst), Decimal(0))
        short = -account_net.get(inst, Decimal(0))
        if abs(over - short) > DUST:
            plan.problems.append(f"{inst}: MANUAL_TRADE over-sold {over} but the account is short {short}")
    # The replay must end where the bridge did, or it is not modelling the bridge.
    positions = {(r["instance_id"], r["instrument_id"]): Decimal(r["qty"]) for r in await conn.fetch(
        "SELECT instance_id, instrument_id, sum(quantity) AS qty FROM paper_positions WHERE account_id = $1 "
        "AND status = 'OPEN' AND instrument_id NOT LIKE '%-PERP.%' GROUP BY 1, 2", account_id)}
    for key in sorted(set(held) | set(positions)):
        if abs(held.get(key, Decimal(0)) - positions.get(key, Decimal(0))) > DUST:
            plan.problems.append(f"{key[0]} {key[1]}: the fills replay to {held.get(key, 0)} but paper_positions "
                                 f"holds {positions.get(key, 0)}")
    if plan.others:
        plan.problems.append(f"{len(plan.others)} over-sell(s) by other instances; review them first")
    n_open = await conn.fetchval("SELECT count(*) FROM paper_positions WHERE account_id = $1 AND status = 'OPEN'",
                                 account_id)
    if n_open:
        plan.problems.append(f"{n_open} position(s) OPEN; close them first")
    n_cmd = await conn.fetchval("SELECT count(*) FROM execution_commands WHERE status = ANY($1::varchar[])",
                                list(ACTIVE_COMMANDS))
    if n_cmd:
        plan.problems.append(f"{n_cmd} command(s) {'/'.join(ACTIVE_COMMANDS)}; wait for the queue to drain")
    n_ord = await conn.fetchval("SELECT count(*) FROM paper_orders WHERE account_id = $1 "
                                "AND status <> ALL($2::varchar[])", account_id, list(DONE_ORDERS))
    if n_ord:
        plan.problems.append(f"{n_ord} paper order(s) still working; cancel them first")
    return plan


def report(plan: Plan, account_id: str) -> None:
    print(f"Account {account_id}")
    print(f"  cash_balance   {plan.cash}")
    print(f"  total_equity   {plan.equity}")
    print(f"\nMANUAL_TRADE spot SELLs that sold more than MANUAL_TRADE held ({len(plan.items)} to correct):")
    for o in plan.items:
        print(f"  {o.executed_at:%Y-%m-%d %H:%M:%S}  {o.instrument_id}  order {o.client_order_id}  trade {o.trade_id}")
        print(f"      sold {o.quantity} @ {o.price}, held {o.held}, over-sold {o.over_qty}")
        print(f"      credited for the over-sold part: {(o.over_qty * o.price).quantize(Q)} - fee {o.commission_share}"
              f" = {o.net_credit}")
    for inst in sorted({o.instrument_id for o in plan.items}):
        sub = [o for o in plan.items if o.instrument_id == inst]
        print(f"  {inst}: over-sold {sum((o.over_qty for o in sub), Decimal(0))}, account net "
              f"{plan.account_net.get(inst)}, correction {sum((o.net_credit for o in sub), Decimal(0))}")
    for o in plan.done:
        print(f"  already reconciled by audit_events #{o.reconciled_by}: trade {o.trade_id} ({o.client_order_id})")
    for o in plan.others:
        print(f"  NOT CORRECTED (instance {o.instance_id}): trade {o.trade_id} {o.instrument_id} over-sold {o.over_qty}")
    print(f"\nCash correction: -{plan.debit}")
    print(f"Cash after:       {plan.cash - plan.debit}")
    # The bridge books a spot BUY's fee to cash only and an uncovered SELL's proceeds to cash only, so once the
    # account is flat this identity should hold after the correction (perp fills and funding go to both).
    print(f"Check: starting + realized_pnl - spot BUY fees = {plan.ledger} "
          f"(cash after differs by {plan.cash - plan.debit - plan.ledger})")
    if plan.problems:
        print("\n--apply would be refused:")
        for p in plan.problems:
            print(f"  - {p}")


def event_details(plan: Plan, account_id: str, actor: str, reason: str) -> dict:
    return {
        "account_id": account_id,
        "actor": f"operator:{actor}",
        "reason": reason,
        "ref": "PR #102 (4af6cb6): manual SELLs checked against the account-level quantity",
        "cash_before": str(plan.cash),
        "debit": str(plan.debit),
        "cash_after": str(plan.cash - plan.debit),
        "items": [{
            "trade_id": o.trade_id, "client_order_id": o.client_order_id, "instance_id": o.instance_id,
            "instrument_id": o.instrument_id, "executed_at": o.executed_at.isoformat(), "price": str(o.price),
            "quantity": str(o.quantity), "held": str(o.held), "over_sold_qty": str(o.over_qty),
            "commission_share": str(o.commission_share), "net_credit": str(o.net_credit),
        } for o in plan.items],
    }


async def apply(conn, account_id: str, confirm: Decimal, actor: str, reason: str) -> tuple[int, Plan]:
    """One transaction: lock the account, rebuild the plan, refuse on any problem or a total that moved."""
    async with conn.transaction():
        # The bridge locks this row for every fill, and a second run waits here, then sees the first one's event.
        await conn.execute("SELECT 1 FROM paper_accounts WHERE account_id = $1 FOR UPDATE", account_id)
        plan = await build_plan(conn, account_id)
        if plan.problems:
            raise RefusedError("; ".join(plan.problems))
        if not plan.items:
            raise RefusedError("nothing to correct")
        if plan.debit != confirm:
            raise RefusedError(f"the correction is now {plan.debit}, not the --confirm {confirm}; run the dry run again")
        await conn.execute(
            """UPDATE paper_accounts SET cash_balance = cash_balance - $2, total_equity = total_equity - $2,
                   updated_at = now() WHERE account_id = $1""", account_id, plan.debit)
        eid = await conn.fetchval(
            "INSERT INTO audit_events (event_type, source, details) VALUES ($1, $2, $3::jsonb) RETURNING id",
            EVENT_TYPE, SOURCE, json.dumps(event_details(plan, account_id, actor, reason)))
    return eid, plan


class RefusedError(Exception):
    pass


async def main(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(args.database_url)
    try:
        plan = await build_plan(conn, args.account_id)
        report(plan, args.account_id)
        if not args.apply:
            if plan.items:
                print(f"\nDry run: nothing changed. To apply: --apply --confirm {plan.debit}")
            return 0
        if args.confirm is None:
            print("--apply needs --confirm with the correction the dry run printed", file=sys.stderr)
            return 2
        eid, done = await apply(conn, args.account_id, Decimal(args.confirm), args.actor, args.reason)
        print(f"\nApplied: cash {done.cash} -> {done.cash - done.debit}; audit_events #{eid} names "
              f"{len(done.items)} trade(s). paper_orders and paper_trades are unchanged.")
        print("Restart the orchestrator now: it loads cash from the DB only at startup "
              "(rehydrate_dispatcher_state), so until then its risk checks use the old cash.")
        return 0
    except RefusedError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    finally:
        await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", required=True)
    p.add_argument("--account-id", default="PAPER-ACCOUNT-001")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--confirm", help="the cash correction the dry run printed")
    p.add_argument("--reason", default="phantom MANUAL_TRADE sells of mandate-owned coins, fixed in PR #102")
    p.add_argument("--actor", default="admin")
    sys.exit(asyncio.run(main(p.parse_args())))
