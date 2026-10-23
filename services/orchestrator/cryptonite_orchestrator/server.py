import asyncio
import collections
import contextlib
import dataclasses
import datetime
import json
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.bridge import EventBridgeWorker
from cryptonite_execution.engine import PaperTradingEngine
from cryptonite_execution.mandate_store import load_mandate, others_spot_holding
from cryptonite_execution.matching import PaperFill, match_order, match_working_orders
from cryptonite_execution.profile_log import record_decision
from cryptonite_execution.risk import OrderIntent
from cryptonite_execution.stop_ratchet import ratcheted_stop
from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.exit_thresholds import SETTING_KEYS as EXIT_SETTING_KEYS
from cryptonite_schemas.exit_thresholds import ExitThresholds, settled
from cryptonite_schemas.mandate import (
    MANDATE_INSTANCE_PREFIX,
    MIN_MANDATE_ENTRY_PCT,
    MandateReject,
    is_ai_instance,
    mandate_id_from_instance,
)
from cryptonite_strategies.registry import STRATEGY_REGISTRY
from nautilus_trader.model import Bar, BarType, Price, Quantity
from pydantic import ValidationError

from cryptonite_orchestrator.mandate_gate import (
    command_mandate_id,
    gate_ai_order,
    grade_ceiling,
    instance_spot_holdings,
    is_ai_command,
    mandate_entry_room,
    mandate_risk_budget,
    plan_perp_entry,
)
from cryptonite_orchestrator.mandate_monitor import (
    alert,
    mandate_monitor_once,
    sweep_approvals_once,
)
from cryptonite_orchestrator.marks import start_mark_to_market
from cryptonite_orchestrator.ops_monitor import ops_monitor
from cryptonite_orchestrator.source_hash import instance_source_hash, risk_policy_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cryptonite.orchestrator")

DATABASE_URL = os.getenv("ASYNC_DATABASE_URL", "postgresql+asyncpg://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
ENVIRONMENT = os.getenv("ENVIRONMENT", "PAPER").upper()
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "PAPER-ACCOUNT-001")


def to_canonical_instrument(sym: str) -> str:
    clean = sym.strip().upper()
    return clean if clean.endswith(".BINANCE") else f"{clean}.BINANCE"


def to_nautilus_bar_type_str(instrument_id: str, timeframe: str) -> str:
    inst = to_canonical_instrument(instrument_id)
    tf = timeframe.strip().lower()
    if tf.count("-") >= 4:
        return tf.upper()
    clean_tf = tf.replace(inst.lower(), "").strip("-")
    if clean_tf.endswith("m"):
        mins = clean_tf[:-1] or "1"
        return f"{inst}-{mins}-MINUTE-LAST-INTERNAL"
    elif clean_tf.endswith("h"):
        hrs = clean_tf[:-1] or "1"
        return f"{inst}-{hrs}-HOUR-LAST-INTERNAL"
    elif clean_tf.endswith("d"):
        days = clean_tf[:-1] or "1"
        return f"{inst}-{days}-DAY-LAST-INTERNAL"
    elif clean_tf.isdigit():
        return f"{inst}-{clean_tf}-MINUTE-LAST-INTERNAL"
    return f"{inst}-1-MINUTE-LAST-INTERNAL"


class SimpleQuote:
    def __init__(self, instrument_id: str, bid_price: float, ask_price: float, bid_size: float = 1.0, ask_size: float = 1.0, ts_init: int | None = None):
        self.instrument_id = instrument_id
        self.bid_price = Decimal(str(bid_price))
        self.ask_price = Decimal(str(ask_price))
        self.bid_size = Decimal(str(bid_size))
        self.ask_size = Decimal(str(ask_size))
        self.ts_init = ts_init or time.time_ns()
        self.sizes_known = False  # True when the feed sent real top-of-book sizes (not the 1.0 default)


class SimpleQuoteCache:
    def __init__(self):
        self._quotes: dict[str, SimpleQuote] = {}

    def set_quote(self, instrument_id: str, bid: float, ask: float, bid_size: float = 1.0, ask_size: float = 1.0, ts_init: int | None = None,
                  sizes_known: bool = False):
        canonical = to_canonical_instrument(instrument_id)
        q = SimpleQuote(canonical, bid, ask, bid_size, ask_size, ts_init)
        q.sizes_known = sizes_known
        self._quotes[canonical] = q
        self._quotes[canonical.replace(".BINANCE", "")] = q

    def quote(self, instrument_id: Any) -> SimpleQuote | None:
        inst_str = str(instrument_id.value) if hasattr(instrument_id, "value") else str(instrument_id)
        if inst_str in self._quotes:
            return self._quotes[inst_str]
        canonical = to_canonical_instrument(inst_str)
        return self._quotes.get(canonical)


DEFAULT_FEE_RATE = Decimal("0.001")
DEFAULT_PERP_FEE_RATE = Decimal("0.0005")  # Binance USD-M taker (leverage plan 1.6)
MAX_MATCH_QUOTE_AGE_NS = 5_000_000_000  # a backlog of older quotes never fills working orders

# Ratchet of AI perps' resting stops (auto-trade plan A4). The trail is the exit monitor's: the same system_settings
# keys (exit.trail_activate_pct, exit.trail_pct) and the same env defaults, so the operator has one knob.
STOP_RATCHET_DEFAULTS = ExitThresholds(
    stop_loss_pct=float(os.getenv("EXIT_STOP_LOSS_PCT", "5")),
    trail_activate_pct=float(os.getenv("EXIT_TRAIL_ACTIVATE_PCT", "3")),
    trail_pct=float(os.getenv("EXIT_TRAIL_PCT", "2")),
    debate_drawdown_pct=float(os.getenv("EXIT_DEBATE_DRAWDOWN_PCT", "2")),
    funding_spike=float(os.getenv("EXIT_FUNDING_SPIKE", "0.0005")),
)
STOP_RATCHET_MIN_INTERVAL_S = 10.0  # one move per position at most this often (each move is a new paper_orders row)
STOP_RATCHET_SETTINGS_TTL_S = 30.0  # the trail settings are re-read this often
STOP_RATCHET_TICK_RETRY_S = 300.0  # an instrument with no known tick is looked up again after this
STOP_RATCHET_TIMEOUT_S = 1.0  # one ratchet pass may take at most this long on the mark loop (it runs after the liquidations)
# Best mark since entry per position (hash: instance_id, side, entry, best), so a restart keeps trailing from it
STOP_RESTORE_STALE_ALERT_S = 120.0  # a restored stop held back by stale data this long raises a CRITICAL alert
STOP_RESTORE_RETRY_REASONS = ("DATA_STALE_OR_DISCONTINUOUS", "NO_LIVE_QUOTE_IN_CACHE", "NO_MARK_PRICE")  # transient

STOP_RATCHET_STATE_KEY = "perp:stop_ratchet:{account}:{instrument}"
STOP_RATCHET_STATE_TTL_S = 14 * 24 * 3600
# Written by the mandate session code (feat/auto-trade-phase-a): AI_MANDATE_<hex> of mandates whose daily profit
# target is met; expires at 00:00 UTC. Read only here; a missing set is empty.
TARGET_MET_KEY = "mandate:session:target_met"
# An entry sized to the mandate's room keeps 1% back, so a quote tick between sizing and the mandate
# check cannot push it over the limit
SIZING_MARGIN = Decimal("0.99")

UPSERT_FILLED_ORDER_SQL = """
    INSERT INTO paper_orders (
        client_order_id, instance_id, account_id, instrument_id, side,
        order_type, quantity, price, filled_qty, avg_fill_price, status,
        last_event_version, created_at, updated_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $7, $9, 'FILLED', 1, now(), now())
    ON CONFLICT (client_order_id) DO UPDATE SET
        status = 'FILLED',
        filled_qty = EXCLUDED.filled_qty,
        avg_fill_price = EXCLUDED.avg_fill_price,
        updated_at = now();
"""


@contextlib.asynccontextmanager
async def _savepoint(conn):
    """A nested transaction (savepoint) when ``conn`` supports it; a no-op otherwise (test doubles)."""
    tx = conn.transaction() if hasattr(conn, "transaction") else None
    if asyncio.iscoroutine(tx):
        tx = await tx
    if hasattr(tx, "__aenter__"):
        async with tx:
            yield
    else:
        yield


DISPATCHED_ID_MEMORY = 10_000


def remember_dispatched(engine, client_order_id: str, command_id) -> None:
    """Record which command placed an order id, independently of the database and the bridge."""
    seen = engine.__dict__.setdefault("_dispatched_order_ids", collections.OrderedDict())
    seen[client_order_id] = str(command_id)
    while len(seen) > DISPATCHED_ID_MEMORY:
        seen.popitem(last=False)


def forget_dispatched(engine, client_order_id: str) -> None:
    engine.__dict__.get("_dispatched_order_ids", {}).pop(client_order_id, None)


def dispatched_by(engine, client_order_id: str) -> str | None:
    return engine.__dict__.get("_dispatched_order_ids", {}).get(client_order_id)


async def apply_paper_fill(
    conn, engine, fill: PaperFill, instance_id: str, order_type: str, limit_price: Decimal | None
) -> None:
    """Book a paper fill.

    Order: dispatcher ledger (pops the working order), then the ORDER_FILLED bridge event (the bridge
    also creates the paper_orders row if missing), then a best-effort paper_orders upsert for
    immediate UI consistency. A failed upsert therefore never leaves memory ahead of the database.
    """
    fee_rate = fee_rate_for(engine.dispatcher, fill.instrument_id)  # maker fills pay it too
    comm = fill.quantity * fill.price * fee_rate
    # A perp entry that carries its stop (``stop_loss``) gets a resting stop once it fills (leverage plan 1.4)
    order_info = engine.dispatcher.open_orders.get(fill.client_order_id) or {}
    attach_stop = order_info.get("attach_stop")
    entry_order = not order_info.get("reduce_only") and (order_info.get("is_perp") or fill.side == "BUY")
    old_pos = getattr(engine.dispatcher, "perp_positions", {}).get(fill.instrument_id)
    entry_price = old_pos.entry_price if old_pos is not None else None
    position_side = "SHORT" if old_pos is not None and old_pos.signed_qty < 0 else "LONG"
    leverage = getattr(order_info.get("intent"), "leverage", None) or getattr(old_pos, "leverage", 1)
    stops_before = set(engine.dispatcher.resting_stops(fill.instrument_id)) if hasattr(
        engine.dispatcher, "resting_stops") else set()
    realized = engine.dispatcher.handle_fill(
        client_order_id=fill.client_order_id,
        fill_qty=fill.quantity,
        fill_price=fill.price,
        commission=comm,
        side=fill.side,
        instrument_id=fill.instrument_id,
    )
    acc_id = getattr(engine, "account_id", ACCOUNT_ID)
    engine.bridge.enqueue_event({
        "event_type": "ORDER_FILLED",
        "event_version": 1,
        "client_order_id": fill.client_order_id,
        "account_id": acc_id,
        "instance_id": instance_id,
        "instrument_id": fill.instrument_id,
        "side": fill.side,
        "order_type": order_type,
        "limit_price": str(limit_price) if limit_price is not None else None,
        "price": str(fill.price),
        "quantity": str(fill.quantity),
        "commission": str(comm),
        "filled_qty": str(fill.quantity),
        "avg_fill_price": str(fill.price),
        "status": "FILLED",
        "occurred_at_ns": time.time_ns(),
        **engine.dispatcher.fill_event_fields(fill.instrument_id, realized, fill.price),
    })
    try:
        # Savepoint: inside the poller's claim transaction a failed statement would otherwise abort
        # the whole transaction, roll back the claim, and let a retry fill the order a second time
        async with _savepoint(conn):
            await conn.execute(
                UPSERT_FILLED_ORDER_SQL,
                fill.client_order_id, instance_id, acc_id, fill.instrument_id,
                fill.side, order_type, fill.quantity, limit_price, fill.price,
            )
            await record_profile_trade(
                conn, engine, instance_id, fill.instrument_id, fill.side, fill.quantity, fill.price,
                entry=entry_order, position_side=position_side, entry_price=entry_price,
                leverage=leverage, stop=attach_stop or getattr(order_info.get("intent"), "stop_price", None),
                realized=realized, commission=comm, client_order_id=fill.client_order_id,
                command_id=dispatched_by(engine, fill.client_order_id),
                reason="STOP" if order_type == "STOP_MARKET" else None,
            )
    except Exception as e:  # the bridge event above still records the fill
        logger.error("paper_orders upsert failed for %s (bridge will record it): %s", fill.client_order_id, e)
    logger.info("Order %s filled %s %s @ %s (%s)", fill.client_order_id, fill.side, fill.quantity, fill.price, fill.liquidity)
    stops_before.discard(fill.client_order_id)  # a stop that just fired is filled, not dropped
    if stops_before and (released_stops := stops_before - set(engine.dispatcher.resting_stops(fill.instrument_id))):
        # the fill closed or flipped the position, so its resting stops were dropped: mark their rows cancelled
        logger.info("Resting stops %s dropped with the position on %s", sorted(released_stops), fill.instrument_id)
        try:
            async with _savepoint(conn):
                await reconcile_working_orders(conn, engine)
        except Exception as e:
            logger.error("Could not cancel the rows of dropped stops %s: %s", sorted(released_stops), e)
    if attach_stop is not None:
        await attach_resting_stop(conn, engine, fill.instrument_id, Decimal(str(attach_stop)), instance_id)


async def record_profile_trade(conn, engine, owner: str, instrument_id: str, side: str,
                               qty: Decimal, price: Decimal, *, entry: bool, position_side: str = "LONG",
                               entry_price: Decimal | None = None, leverage: int = 1,
                               stop: Decimal | None = None, realized: Decimal | None = None,
                               commission: Decimal = Decimal("0"), command_id=None, reason: str | None = None,
                               client_order_id: str | None = None) -> None:
    """ADR 0011/P1.8 trade contract, in the caller's fill transaction; never notify or block a fill.

    The instrument is the decision row's column. Entry stop and execution mode are captured at fill; an exit
    reads that entry record so a tightened stop never changes the original R denominator. Unknown risk stays null.
    """
    mid = mandate_id_from_instance(owner)
    if mid is None:
        return
    try:
        async with _savepoint(conn):
            perp = "-PERP." in instrument_id
            if entry:
                command = None
                if command_id is not None:
                    command = await conn.fetchrow("SELECT command_id, payload FROM execution_commands WHERE command_id = $1",
                                                  uuid.UUID(str(command_id)))
                elif client_order_id is not None:
                    # A working order can fill after restart, when the in-memory command map is empty.
                    prefix = client_order_id.removeprefix("AI-").removeprefix("MANUAL-").lower()
                    command = await conn.fetchrow(
                        """SELECT command_id, payload FROM execution_commands WHERE mandate_id = $1
                           AND (payload->>'client_order_id' = $2 OR replace(command_id::text, '-', '') LIKE $3)
                           ORDER BY created_at DESC LIMIT 1""", mid, client_order_id, prefix + "%")
                    if command:
                        command_id = command["command_id"]
                if command is not None and stop is None:
                    source = _payload_dict(command["payload"])
                    stop = source.get("stop_loss") or source.get("stop_price") or source.get("proposed_stop_loss")
                source = _payload_dict(command["payload"]) if command is not None else {}
                m = await load_mandate(conn, mid)
                if m is None:
                    return
                from cryptonite_execution.profile_lifecycle import autotrade_switch_on
                mode = source.get("profile_execution_mode")
                if mode not in {"ASK", "VETO", "AUTO"}:
                    mode = {1: "ASK", 2: "VETO", 3: "AUTO"}[int(m.autonomy_level)]
                    if not await autotrade_switch_on(conn):
                        mode = "ASK"
                await record_decision(conn, mid, "FILL", "FILLED", instrument_id=instrument_id,
                                      command_id=command_id, side="LONG" if side == "BUY" else "SHORT",
                                      market="PERP" if perp else "SPOT", price=price, qty=qty,
                                      leverage=leverage, stop=stop, mode=mode)
                return
            previous = await conn.fetchrow(
                """SELECT detail FROM ai_profile_decisions WHERE mandate_id = $1 AND instrument_id = $2
                   AND stage = 'FILL' ORDER BY ts DESC, id DESC LIMIT 1""", mid, instrument_id)
            detail = _payload_dict(previous["detail"]) if previous else {}
            if entry_price is None:
                # The bridge can lag the fill. Rebuild the remaining spot cost from committed fill rows,
                # resetting at flat and removing a partial sale at its weighted entry cost. Historical buys
                # from a previous trade must never change this exit's PnL.
                history = await conn.fetch(
                    """SELECT side, filled_qty, COALESCE(avg_fill_price, price) AS fill_price FROM paper_orders
                       WHERE instance_id = $1 AND instrument_id = $2 AND filled_qty > 0
                         AND ($3::text IS NULL OR client_order_id <> $3)
                       ORDER BY created_at, updated_at, client_order_id""", owner, instrument_id, client_order_id)
                held = cost = Decimal("0")
                for trade in history:
                    if trade.get("side") not in ("BUY", "SELL"):
                        continue
                    trade_qty = Decimal(str(trade["filled_qty"]))
                    if trade["side"] == "BUY":
                        cost += trade_qty * Decimal(str(trade["fill_price"]))
                        held += trade_qty
                    elif held > 0:
                        closed = min(trade_qty, held)
                        cost -= cost / held * closed
                        held -= closed
                entry_price = cost / held if held > 0 else detail.get("price")
                if entry_price is None:
                    holdings = await instance_spot_holdings(conn, owner)
                    entry_price = holdings.get(instrument_id, (None, None))[1]
            entry_price = Decimal(str(entry_price)) if entry_price else None
            gross = realized
            if not perp and entry_price is not None:
                gross = (price - entry_price) * qty
            pnl = Decimal(str(gross)) - commission if gross is not None else None
            original_stop = Decimal(str(detail["stop"])) if detail.get("stop") is not None else None
            risk = abs(entry_price - original_stop) * qty if entry_price is not None and original_stop is not None else None
            outcome = {"HARD_STOP": "STOP", "STRUCTURAL_STOP": "STOP", "TRAILING_STOP": "TRAIL",
                       "TAKE_PROFIT_1": "TP1", "TAKE_PROFIT": "TP"}.get(reason, reason) or "MANUAL"
            if outcome == "STOP" and stop is not None and original_stop is not None and Decimal(str(stop)) != original_stop:
                outcome = "TRAIL"
            if command_id is not None and reason is None:
                command = await conn.fetchrow("SELECT payload FROM execution_commands WHERE command_id = $1", uuid.UUID(str(command_id)))
                if command:
                    raw = _payload_dict(command["payload"]).get("reason")
                    outcome = {"TAKE_PROFIT_1": "TP1", "TRAILING_STOP": "TRAIL", "STRUCTURAL_STOP": "STOP",
                               "HARD_STOP": "STOP", "TAKE_PROFIT": "TP"}.get(raw, raw) or outcome
            await record_decision(conn, mid, "EXIT", outcome, instrument_id=instrument_id, command_id=command_id,
                                  side=position_side if perp else "LONG", market="PERP" if perp else "SPOT",
                                  price=price, qty=qty, realized_pnl=pnl,
                                  r_multiple=pnl / risk if pnl is not None and risk else None)
    except Exception as exc:
        logger.warning("Profile trade decision not recorded for %s: %s", owner, exc)


def fee_rate_for(dispatcher, instrument_id: str) -> Decimal:
    """Taker fee for a paper fill: the USD-M rate on perps (leverage plan 1.6), the spot rate otherwise."""
    if instrument_id.split(".", 1)[0].upper().endswith("-PERP"):
        return getattr(dispatcher, "perp_taker_fee", None) or DEFAULT_PERP_FEE_RATE
    return getattr(dispatcher, "taker_fee", None) or DEFAULT_FEE_RATE


async def attach_resting_stop(conn, engine, instrument_id: str, stop_price: Decimal, instance_id: str,
                              refusal: dict | None = None) -> bool:
    """Protect the filled perp position with a resting reduce-only stop (it replaces the owner's earlier one).

    Never loosens a stop that already rests: when the owner already has a stop on the instrument (the position grew,
    or the ratchet trailed it), the stop placed is the tighter of the two, ``max`` for a long and ``min`` for a short.
    A tighter new stop wins; if the resting one is tighter it stays. It is kept as it is when it already covers the
    whole position (no churn), else it is placed again at its own price for the full size. A first entry has no
    resting stop and places ``stop_price`` as given.

    A stop the dispatcher refuses (the mark already went through it, say) leaves the position without a resting
    stop: that is logged CRITICAL; the exit monitor's structural stop and the liquidation-proximity close stay as
    the backup. ``refusal``, when given, receives the dispatcher's reason under ``"reason"``."""
    d = engine.dispatcher
    pos = d.perp_positions.get(instrument_id)
    if pos is not None and pos.signed_qty != 0:
        long = pos.signed_qty > 0
        resting = {cid: o for cid, o in d.resting_stops(instrument_id, instance_id).items()
                   if o["intent"].stop_price is not None and o["side"] == ("SELL" if long else "BUY")}
        if resting:
            prices = [o["intent"].stop_price for o in resting.values()]
            tightest = max(prices) if long else min(prices)
            if (tightest > stop_price) if long else (tightest < stop_price):
                if len(resting) == 1 and next(iter(resting.values()))["quantity"] >= abs(pos.signed_qty):
                    logger.info("Resting stop for %s (%s): the resting %s is tighter than the new %s and covers the "
                                "position; kept", instrument_id, instance_id, tightest, stop_price)
                    return True
                logger.info("Resting stop for %s (%s): the new %s is looser than the resting %s; placed at %s for the "
                            "full position", instrument_id, instance_id, stop_price, tightest, tightest)
                stop_price = tightest
    client_oid = f"STOP-{uuid.uuid4().hex[:12].upper()}"
    ok, reason = d.place_resting_stop(client_oid, instrument_id, stop_price, instance_id)
    if not ok:
        logger.critical("Resting stop for %s (%s) at %s refused: %s; the position has no resting stop",
                        instrument_id, instance_id, stop_price, reason)
        if refusal is not None:
            refusal["reason"] = reason
        return False
    info = d.open_orders[client_oid]  # no await since the placement
    await record_resting_stop(conn, engine, client_oid, instrument_id, stop_price)
    logger.info("Resting stop %s: %s %s %s @ %s", client_oid, info["side"], info["quantity"], instrument_id, stop_price)
    return True


async def record_resting_stop(conn, engine, client_oid: str, instrument_id: str, stop_price: Decimal) -> bool:
    """The paper_orders row of a resting stop the dispatcher just placed (ACCEPTED: the row a restart restores
    from), then the rows of the stops it replaced are cancelled. False when the row was not written: the stop is
    gone already (a close or fill removed it while the caller awaited) or the INSERT failed; it rests in memory
    either way."""
    info = engine.dispatcher.open_orders.get(client_oid)
    if info is None:
        logger.warning("Resting stop %s on %s is gone already; no paper_orders row written", client_oid, instrument_id)
        return False
    try:
        async with _savepoint(conn):
            await conn.execute(
                """
                INSERT INTO paper_orders (
                    client_order_id, instance_id, account_id, instrument_id, side,
                    order_type, quantity, price, filled_qty, status,
                    last_event_version, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, 'STOP_MARKET', $6, $7, 0, 'ACCEPTED', 0, now(), now())
                ON CONFLICT (client_order_id) DO NOTHING
                """,
                client_oid, info["instance_id"], getattr(engine, "account_id", ACCOUNT_ID), instrument_id,
                info["side"], info["quantity"], stop_price,
            )
    except Exception as e:  # the stop rests in memory either way
        logger.error("paper_orders row for resting stop %s failed: %s", client_oid, e)
        return False
    try:
        # the replaced stops are no longer working
        await reconcile_working_orders(conn, engine)
    except Exception as e:
        logger.error("Rows of the stops replaced by %s not cancelled: %s", client_oid, e)
    return True


async def load_stop_restores(pool, engine) -> int:
    """After a restart, the resting stops of open perp positions to place again (they live in memory only).

    Per open perp position: its newest ACCEPTED ``STOP_MARKET`` row (kept, same client id), else the ``stop_loss`` its
    entry order carried (a new row). They are placed on the first mark pass (``restore_pending_stops``): the
    dispatcher needs a mark. Until then ``reconcile_working_orders`` leaves their rows ACCEPTED."""
    d = engine.dispatcher
    pending: dict[str, tuple[str, Decimal, str, bool]] = {}
    account = getattr(engine, "account_id", ACCOUNT_ID)
    async with pool.acquire() as conn:
        for inst, pos in d.perp_positions.items():
            if pos.signed_qty == 0:
                continue
            row = await conn.fetchrow(
                """SELECT client_order_id, price FROM paper_orders
                   WHERE account_id = $1 AND instrument_id = $2 AND instance_id = $3 AND order_type = 'STOP_MARKET'
                     AND status = 'ACCEPTED' ORDER BY created_at DESC LIMIT 1""", account, inst, pos.instance_id)
            if row is not None and row["price"]:
                pending[row["client_order_id"]] = (inst, Decimal(str(row["price"])), pos.instance_id, False)
                continue
            stop = await conn.fetchval(
                """SELECT payload->>'stop_loss' FROM execution_commands
                   WHERE target_instance_id = $1 AND command_type = 'SUBMIT_ORDER' AND status = 'COMPLETED'
                     AND payload->>'instrument_id' = $2 AND payload->>'stop_loss' IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""", pos.instance_id, inst)
            if stop:
                pending[f"STOP-{uuid.uuid4().hex[:12].upper()}"] = (inst, Decimal(str(stop)), pos.instance_id, True)
            else:
                logger.warning("Open perp %s (%s) has no stop to restore; the exit monitor's stops remain", inst,
                               pos.instance_id)
    engine.pending_stop_restores = pending
    if pending:
        logger.info("Resting stops to restore on the first mark pass: %s", sorted(pending))
    return len(pending)


async def restore_pending_stops(pool, engine) -> int:
    """Place the stops ``load_stop_restores`` found once their mark is known. Returns the number of stops placed.

    Right after a restart the dispatcher's freshness check can still fail, so a stop is placed only while the
    ingress is fresh; otherwise it stays pending and is retried on the next pass (CRITICAL log and alert, once, when
    that lasts over ``STOP_RESTORE_STALE_ALERT_S``; the exit monitor's own stops remain the backup). Only a refusal
    that says the mark already went through the stop (``STOP_WOULD_TRIGGER_IMMEDIATELY``) closes the position
    (CLOSE_POSITION, risk reducing), with a CRITICAL log, never left without a stop. Any other refusal is logged
    CRITICAL and the stop dropped without a close (it cannot succeed); a transient one (no quote yet) is retried."""
    d = engine.dispatcher
    pending = getattr(engine, "pending_stop_restores", None) or {}
    deferred = getattr(engine, "stop_restore_deferred", None)
    if deferred is None:
        deferred = engine.stop_restore_deferred = {}  # cid -> (first deferred at, alerted)
    placed = 0
    for cid, entry in list(pending.items()):
        inst, stop, owner, new_row = entry
        if d.mark_prices.get(inst) is None:
            continue  # no mark yet: next pass
        pending.pop(cid)
        pos = d.perp_positions.get(inst)
        if pos is None or pos.signed_qty == 0:
            deferred.pop(cid, None)
            continue
        async with pool.acquire() as conn:
            refusal: dict = {}
            if not d.ingress_health.is_fresh(inst, d.node_cache):  # no await until the placement below
                ok, reason = False, "DATA_STALE_OR_DISCONTINUOUS"
            elif new_row:
                ok = await attach_resting_stop(conn, engine, inst, stop, owner, refusal)
                reason = "APPROVED" if ok else refusal.get("reason", "refused")
            else:
                ok, reason = d.place_resting_stop(cid, inst, stop, owner)
            if not ok and reason.startswith(STOP_RESTORE_RETRY_REASONS):
                pending[cid] = entry
                since, alerted = deferred.get(cid, (time.monotonic(), False))
                if not alerted and time.monotonic() - since > STOP_RESTORE_STALE_ALERT_S:
                    alerted = True
                    message = (f"Resting stop for {inst} ({owner}) at {stop} not restored for over "
                               f"{STOP_RESTORE_STALE_ALERT_S:.0f}s ({reason})")
                    logger.critical("%s: retrying, the position is NOT closed; the exit monitor's own stops "
                                    "(REST fallback) remain the backup", message)
                    await alert(conn, None, "STOP_RESTORE_STALE", message,
                                {"instrument_id": inst, "instance_id": owner, "stop_price": str(stop)},
                                dedupe_key=f"restore-stop-stale:{cid}")
                deferred[cid] = (since, alerted)
                continue
            deferred.pop(cid, None)
            if ok:
                placed += 1
                logger.info("Restored resting stop for %s (%s) at %s", inst, owner, stop)
                continue
            if not reason.startswith("STOP_WOULD_TRIGGER_IMMEDIATELY"):
                logger.critical("Resting stop for %s (%s) at %s could not be restored (%s): dropped, the position is "
                                "NOT closed; the exit monitor's own stops remain", inst, owner, stop, reason)
                continue
            logger.critical("Resting stop for %s (%s) at %s could not be restored (%s): closing the position",
                            inst, owner, stop, reason)
            await conn.execute(
                """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id,
                       requester_id, payload, status, environment, account_id)
                   VALUES ($1, $2, 'CLOSE_POSITION', $3, 'risk:orchestrator', $4::jsonb, 'PENDING', 'PAPER', $5)
                   ON CONFLICT (idempotency_key) DO NOTHING""",
                uuid.uuid4(), f"restore-stop:{cid}", owner,
                json.dumps({"instrument_id": inst, "reason": "STOP_NOT_RESTORED", "stop_price": str(stop)}),
                getattr(engine, "account_id", ACCOUNT_ID),
            )
            await reconcile_working_orders(conn, engine)
    return placed


async def fire_resting_stops(pool, engine, mark_prices: dict[str, Decimal]) -> int:
    """Fill the resting stops the mark reached (leverage plan 1.4). Runs on every mark pass before the
    liquidation check, so it works when the exit monitor is blind. Returns the number of stops filled."""
    d = engine.dispatcher
    fills, cancelled = d.triggered_stops(mark_prices)
    if not fills and not cancelled:
        return 0
    async with pool.acquire() as conn:
        for fill in fills:
            info = d.open_orders.get(fill.client_order_id)
            if info is None:
                continue
            intent = info["intent"]
            await apply_paper_fill(conn, engine, fill, intent.instance_id, "STOP_MARKET", intent.stop_price)
            logger.warning("Resting stop %s fired: %s %s %s @ %s (stop %s)", fill.client_order_id, fill.side,
                           fill.quantity, fill.instrument_id, fill.price, intent.stop_price)
        if cancelled:
            await reconcile_working_orders(conn, engine)
    return len(fills)


@dataclasses.dataclass
class RatchetState:
    """One position's trail: its owner, side and entry (a different one is a new position), the best mark since
    entry, and when its stop last moved (monotonic seconds; memory only)."""
    instance_id: str
    long: bool
    entry: Decimal
    best: Decimal
    last_move: float | None = None


def _text(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


async def ratchet_thresholds(pool, engine, now: float, owner: str | None = None) -> ExitThresholds:
    """The exit monitor's thresholds as stored (re-read every STOP_RATCHET_SETTINGS_TTL_S). A failed read or a
    refused set keeps the last valid one, as the exit monitor does."""
    mid = mandate_id_from_instance(owner)
    if mid is not None:
        global_t = await ratchet_thresholds(pool, engine, now)
        profiles = engine.__dict__.setdefault("profile_ratchet_thresholds", {})
        cached = profiles.get(mid)
        if cached is None or now - cached[1] >= STOP_RATCHET_SETTINGS_TTL_S:
            values = cached[0] if cached is not None else (None, None)
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT exit_trail_activate_pct, exit_trail_pct FROM ai_mandates WHERE id = $1", mid)
                values = (row["exit_trail_activate_pct"], row["exit_trail_pct"]) if row else (None, None)
            except Exception as exc:
                logger.warning("Profile ratchet thresholds not re-read for %s: %s", owner, exc)
            profiles[mid] = (values, now)
        else:
            values = cached[0]
        return dataclasses.replace(global_t,
                                   trail_activate_pct=float(values[0]) if values[0] is not None else global_t.trail_activate_pct,
                                   trail_pct=float(values[1]) if values[1] is not None else global_t.trail_pct)
    cached = getattr(engine, "stop_ratchet_thresholds", None)
    if cached is not None and now - cached[1] < STOP_RATCHET_SETTINGS_TTL_S:
        return cached[0]
    applied = cached[0] if cached is not None else STOP_RATCHET_DEFAULTS
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key = ANY($1)", list(EXIT_SETTING_KEYS))
    except Exception as e:
        logger.warning("Stop ratchet: exit thresholds not re-read (%s); keeping the last valid set", e)
    else:
        applied, problems = settled({r["key"]: r["value"] for r in rows}, STOP_RATCHET_DEFAULTS, applied)
        for problem in problems:
            logger.error("Stop ratchet: exit thresholds refused, keeping the last valid set: %s", problem)
    engine.stop_ratchet_thresholds = (applied, now)
    return applied


async def price_tick(pool, engine, instrument_id: str, now: float) -> Decimal | None:
    """The instrument's price increment from ``instruments``: the perp's own row, else its spot sibling's (the
    ingest records spot instruments only). None when neither is known (looked up again later)."""
    ticks = getattr(engine, "price_ticks", None)
    if ticks is None:
        ticks = engine.price_ticks = {}
    if instrument_id in ticks:
        tick, at = ticks[instrument_id]
        if tick is not None or now - at < STOP_RATCHET_TICK_RETRY_S:
            return tick
    spot = instrument_id.replace("-PERP.", ".", 1)
    tick = None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT instrument_id, price_increment FROM instruments WHERE instrument_id = ANY($1::text[])",
                [instrument_id, spot])
        found = {r["instrument_id"]: r["price_increment"] for r in rows if r["price_increment"]}
        raw = found.get(instrument_id) or found.get(spot)
        tick = Decimal(str(raw)) if raw is not None and Decimal(str(raw)) > 0 else None
    except Exception as e:
        logger.warning("Stop ratchet: price tick of %s not read: %s", instrument_id, e)
    if tick is None:
        logger.warning("Stop ratchet: no price tick for %s (nor %s); its stop is not trailed", instrument_id, spot)
    ticks[instrument_id] = (tick, now)
    return tick


async def ratchet_state(redis, engine, instrument_id: str, owner: str, long: bool, entry: Decimal,
                        mark: Decimal) -> RatchetState:
    """The position's trail with ``mark`` taken in. On first sight (a restart) the best mark comes back from Redis
    if it belongs to the same position (owner, side, entry); a new position starts from the mark. A new best is
    written back to Redis (best effort: memory keeps trailing if Redis fails)."""
    states = getattr(engine, "stop_ratchet", None)
    if states is None:
        states = engine.stop_ratchet = {}
    key = STOP_RATCHET_STATE_KEY.format(account=getattr(engine, "account_id", ACCOUNT_ID), instrument=instrument_id)
    st = states.get(instrument_id)
    if st is None or (st.instance_id, st.long, st.entry) != (owner, long, entry):
        st = RatchetState(owner, long, entry, mark)
        try:
            stored = {_text(k): _text(v) for k, v in (await redis.hgetall(key) or {}).items()}
            if (stored.get("instance_id") == owner and stored.get("side") == ("LONG" if long else "SHORT")
                    and stored.get("entry") and Decimal(stored["entry"]) == entry and stored.get("best")):
                st.best = Decimal(stored["best"])
        except Exception as e:
            logger.warning("Stop ratchet: best mark of %s not read from Redis: %s", instrument_id, e)
        states[instrument_id] = st
        improved = True  # (re)write the record for this position
    else:
        improved = False
    if (mark > st.best) if long else (mark < st.best):
        st.best, improved = mark, True
    if improved:
        try:
            await redis.hset(key, mapping={"instance_id": owner, "side": "LONG" if long else "SHORT",
                                           "entry": str(entry), "best": str(st.best)})
            await redis.expire(key, STOP_RATCHET_STATE_TTL_S)
        except Exception as e:
            logger.warning("Stop ratchet: best mark of %s not saved to Redis: %s", instrument_id, e)
    return st


async def target_met(redis, owner: str) -> bool:
    """The owner's mandate met its daily profit target (``TARGET_MET_KEY``). A missing set or a failed read is
    not met."""
    try:
        return bool(await redis.sismember(TARGET_MET_KEY, owner))
    except Exception as e:
        logger.warning("Stop ratchet: %s not read (%s); treated as not met", TARGET_MET_KEY, e)
        return False


async def drop_closed_ratchet_states(redis, engine) -> int:
    """Forget the trail (memory and Redis hash) of every position that is closed or has no resting stop of its owner
    any more, so a position reopened at the same entry starts fresh instead of inheriting best mark and last move.
    A hash whose position closed while the orchestrator was down is not found here (no in-memory record); it is
    replaced when a position with another owner, side or entry shows up. Returns the number dropped."""
    states = getattr(engine, "stop_ratchet", None)
    if not states:
        return 0
    d = engine.dispatcher
    dropped = 0
    for inst, st in list(states.items()):
        pos = d.perp_positions.get(inst)
        if (pos is not None and pos.signed_qty != 0 and pos.instance_id == st.instance_id
                and d.resting_stops(inst, st.instance_id)):
            continue
        states.pop(inst, None)
        dropped += 1
        key = STOP_RATCHET_STATE_KEY.format(account=getattr(engine, "account_id", ACCOUNT_ID), instrument=inst)
        try:
            await redis.delete(key)
        except Exception as e:
            logger.warning("Stop ratchet: trail record of %s not deleted from Redis: %s", inst, e)
    return dropped


async def ratchet_resting_stops(pool, engine, redis, mark_prices: dict[str, Decimal],
                                now: float | None = None) -> int:
    """Trail the resting stops of AI-owned perp positions behind the best mark since entry (auto-trade plan A4).

    Armed once the best mark is ``exit.trail_activate_pct`` in the position's favour, or at once when the owner's
    mandate met its daily target. A long's stop goes to best x (1 - exit.trail_pct/100), a short's to
    best x (1 + ...), on the tick, only tighter, at least a tick, at most once per STOP_RATCHET_MIN_INTERVAL_S,
    never at or through the mark. A move is ``place_resting_stop`` with a new client id: the new stop is placed and
    the old one released in one synchronous call (the old one is kept if the new one is refused), then the new row
    is written ACCEPTED and the old one CANCELED, so a restart restores the new price. Spot and manual positions are
    not touched. Run after ``fire_resting_stops`` on the same mark pass. Returns the number of stops moved."""
    d = engine.dispatcher
    now = time.monotonic() if now is None else now
    await drop_closed_ratchet_states(redis, engine)
    stops = [(cid, info["instrument_id"], info.get("instance_id")) for cid, info in d.open_orders.items()
             if d.is_stop_order(info) and is_ai_instance(info.get("instance_id"))
             and info["instrument_id"] in mark_prices]
    if not stops:
        return 0
    moved = 0
    for cid, inst, owner in stops:
        pos = d.perp_positions.get(inst)
        if pos is None or pos.signed_qty == 0 or pos.instance_id != owner:
            continue
        t = await ratchet_thresholds(pool, engine, now, owner)
        long, mark = pos.signed_qty > 0, mark_prices[inst]
        st = await ratchet_state(redis, engine, inst, owner, long, pos.entry_price, mark)
        if st.last_move is not None and now - st.last_move < STOP_RATCHET_MIN_INTERVAL_S:
            continue
        tick = await price_tick(pool, engine, inst, now)
        met = await target_met(redis, owner)
        if tick is None:
            continue
        # Read the stop and the position again after the awaits above, and move it without another await between
        info, pos = d.open_orders.get(cid), d.perp_positions.get(inst)
        if info is None or pos is None or pos.signed_qty == 0 or (pos.signed_qty > 0) != long:
            continue
        old = info["intent"].stop_price
        new = ratcheted_stop(long=long, entry=pos.entry_price, best=st.best, mark=mark, current_stop=old,
                             trail_activate_pct=Decimal(str(t.trail_activate_pct)),
                             trail_pct=Decimal(str(t.trail_pct)), tick=tick, target_met=met)
        if new is None:
            continue
        new_cid = f"STOP-{uuid.uuid4().hex[:12].upper()}"
        ok, reason = d.place_resting_stop(new_cid, inst, new, owner)
        st.last_move = now  # a refused move backs off for the minimum interval too
        if not ok:
            logger.warning("Stop ratchet on %s (%s) %s -> %s refused (%s); the stop stays at %s, retry in %ss", inst,
                           owner, old, new, reason, old, STOP_RATCHET_MIN_INTERVAL_S)
            continue
        moved += 1
        logger.info("Stop ratchet on %s (%s): %s -> %s (best mark %s, mark %s%s); %s replaces %s", inst, owner, old,
                    new, st.best, mark, ", daily target met" if met else "", new_cid, cid)

        async def record(new_cid=new_cid, inst=inst, new=new) -> bool:
            async with pool.acquire() as conn:
                return await record_resting_stop(conn, engine, new_cid, inst, new)

        # shielded: the pass timing out must not cancel the row write of a stop that already moved in memory
        if not await asyncio.shield(record()):
            logger.critical("Stop ratchet on %s (%s): the move %s -> %s has no paper_orders row; a restart before the "
                            "next move restores the older stop %s", inst, owner, old, new, old)
    return moved


async def reconcile_working_orders(conn, engine) -> None:
    """Cancel paper_orders rows still ACCEPTED that have no working order in memory any more.

    PAUSE/STOP/CLOSE_POSITION, latches, liquidations and restarts release working orders in the
    dispatcher only. Rows whose order filled are fixed by the bridge's ORDER_FILLED (version 1 > 0).
    """
    await conn.execute(
        """
        UPDATE paper_orders SET status = 'CANCELED', updated_at = now()
        WHERE account_id = $1 AND status = 'ACCEPTED' AND NOT (client_order_id = ANY($2::text[]))
        """,
        getattr(engine, "account_id", ACCOUNT_ID),
        list(engine.dispatcher.open_orders) + list(getattr(engine, "pending_stop_restores", None) or {}),
    )


def _entries_blocked(dispatcher) -> bool:
    return bool(dispatcher.kill_switch.is_latched or dispatcher.daily_loss_latched)


def cancel_entries_if_latched(dispatcher) -> list[str]:
    """While the kill switch or the daily-loss latch is set, working entry orders must not fill."""
    if not _entries_blocked(dispatcher):
        return []
    to_cancel = [
        cid for cid, o in dispatcher.open_orders.items()
        if o.get("intent") is not None and dispatcher.is_entry_order(o)
    ]
    for cid in to_cancel:
        dispatcher.release_reservation(cid)
    if to_cancel:
        logger.warning("Latched: cancelled %d working entry orders", len(to_cancel))
    return to_cancel


def _size(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


async def match_resting_orders(pool, engine, instrument_id: str, bid: float, ask: float,
                               bid_size: Any = None, ask_size: Any = None) -> None:
    """Fill the working orders on ``instrument_id`` that this quote reaches (R7.3: real top-of-book
    sizes, when the feed sends them, let large taker fills pay price impact)."""
    d = engine.dispatcher
    cancelled = cancel_entries_if_latched(d)
    fills = match_working_orders(d.open_orders, instrument_id, Decimal(str(bid)), Decimal(str(ask)),
                                 _size(bid_size), _size(ask_size))
    if not fills and not cancelled:
        return
    async with pool.acquire() as conn:
        for fill in fills:
            info = d.open_orders.get(fill.client_order_id)
            if info is None:  # filled or cancelled while we waited for the connection
                continue
            # State may have moved since matching (awaits above): re-check before booking
            if _entries_blocked(d) and d.is_entry_order(info):
                d.release_reservation(fill.client_order_id)
                cancelled.append(fill.client_order_id)
                continue
            qty = d.fillable_quantity(info)
            if qty <= 0:
                logger.warning("Working exit %s outlived its position; cancelled", fill.client_order_id)
                d.release_reservation(fill.client_order_id)
                cancelled.append(fill.client_order_id)
                continue
            if qty < fill.quantity:
                fill = dataclasses.replace(fill, quantity=qty)
            intent = info["intent"]
            await apply_paper_fill(conn, engine, fill, intent.instance_id, intent.order_type.upper(), intent.price)
        if cancelled:
            await reconcile_working_orders(conn, engine)


def _patch_strategy_for_paper_engine(strategy, engine, instance_id: str):
    """Monkey-patch Nautilus Strategy to route submit_order to PaperTradingEngine dispatcher."""
    def _submit_order(order):
        # Nautilus OrderType: MARKET=1, LIMIT=2. Anything else (stops, ...) has no paper matcher.
        type_value = getattr(getattr(order, "order_type", None), "value", None)
        if type_value not in (1, 2):
            logger.warning("Strategy %s order %s rejected: unsupported order type %s", instance_id, order.client_order_id, type_value)
            return
        intent = OrderIntent(
            client_order_id=order.client_order_id.value,
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(order.instrument_id)),
            side="BUY" if order.side.value == 1 else "SELL",  # OrderSide.BUY=1
            order_type="MARKET" if type_value == 1 else "LIMIT",
            quantity=Decimal(str(order.quantity.as_double())),
            price=Decimal(str(order.price.as_double())) if getattr(order, "price", None) else None,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=order)

    strategy.submit_order = _submit_order

    def _buy_market(qty=None):
        qty_dec = Decimal(str(qty.as_double())) if qty else Decimal(str(strategy.trade_size))
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(strategy.instrument_id)),
            side="BUY",
            order_type="MARKET",
            quantity=qty_dec,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
        
    def _sell_market(qty=None):
        qty_dec = Decimal(str(qty.as_double())) if qty else Decimal(str(strategy.trade_size))
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=to_canonical_instrument(str(strategy.instrument_id)),
            side="SELL",
            order_type="MARKET",
            quantity=qty_dec,
        )
        engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)

    strategy.buy_market = _buy_market
    strategy.sell_market = _sell_market

    # Paper fills never reach the Nautilus portfolio, so position queries read the dispatcher ledger
    # (account-level: strategies sharing an instrument see the same position).
    inst = to_canonical_instrument(str(strategy.instrument_id))
    d = engine.dispatcher
    strategy.is_long = lambda: d.get_position_qty(inst) > 0 or d.get_perp_position_qty(inst) > 0
    strategy.is_short = lambda: d.get_perp_position_qty(inst) < 0
    strategy.is_flat = lambda: d.get_position_qty(inst) == 0 and d.get_perp_position_qty(inst) == 0

    def _close_all(*_args, **_kwargs):
        if getattr(strategy, "is_in_warmup", None) and strategy.is_in_warmup() is True:
            return
        perp_qty = d.get_perp_position_qty(inst)
        if perp_qty != 0:  # perps close with an opposing reduce-only order
            side, qty, reduce_only = ("BUY" if perp_qty < 0 else "SELL"), abs(perp_qty), True
        else:
            side, qty, reduce_only = "SELL", d.get_available_position_qty(inst), False
        if qty <= 0:
            return
        intent = OrderIntent(
            client_order_id=f"O-{uuid.uuid4().hex[:8]}",
            instance_id=instance_id,
            instrument_id=inst,
            side=side,
            order_type="MARKET",
            quantity=qty,
            reduce_only=reduce_only,
        )
        d.dispatch(intent, lambda o: None, nautilus_order=None)

    strategy.close_all = _close_all
    strategy.close_all_positions = _close_all
    return strategy


async def consume_market_data(redis, engine, quote_cache, pool=None):
    """Consumes ui_quote_tick and ingress_health streams to maintain live cache and telemetry.

    With ``pool``, every quote also matches the working paper orders on its instrument.
    """
    quote_stream = "cryptonite:stream:ui_quote_tick"
    health_stream = "cryptonite:stream:ingress_health"

    # Pre-seed latest quotes from stream
    try:
        latest_quotes = await redis.xrevrange(quote_stream, "+", "-", count=20)
        for msg_id, fields in reversed(latest_quotes or []):
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data:
                data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                data = json.loads(data_str)
                quote_cache.set_quote(
                    data["instrument_id"],
                    float(data["bid_price"]),
                    float(data["ask_price"]),
                    float(data.get("bid_size", 1.0)),
                    float(data.get("ask_size", 1.0)),
                    int(data.get("arrival_ns", time.time_ns())),
                    sizes_known="bid_size" in data and "ask_size" in data,
                )
        logger.info("Pre-seeded quote cache with %d entries", len(latest_quotes) if latest_quotes else 0)
    except Exception as e:
        logger.warning("Could not pre-seed quotes: %s", e)

    # Pre-seed latest health records
    try:
        latest_health = await redis.xrevrange(health_stream, "+", "-", count=20)
        for msg_id, fields in reversed(latest_health or []):
            raw_data = fields.get(b"data") or fields.get("data")
            if raw_data:
                data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                data = json.loads(data_str)
                can_inst = to_canonical_instrument(data["instrument_id"])
                engine.ingress_health.update_health(
                    instrument_id=can_inst,
                    session_id=str(data.get("ingest_session_id", "default")),
                    seq=int(data.get("publish_sequence", 0)),
                    event_ts_ns=int(data.get("event_ts_ns", time.time_ns())),
                    arrival_ts_ns=int(data.get("arrival_ts_ns", time.time_ns())),
                )
        logger.info("Pre-seeded ingress health with %d entries", len(latest_health) if latest_health else 0)
    except Exception as e:
        logger.warning("Could not pre-seed ingress health: %s", e)

    async def _consume_quotes():
        last_id = "$"
        while True:
            try:
                res = await redis.xread({quote_stream: last_id}, count=100, block=200)
                if not res:
                    continue
                for _, msgs in res:
                    for msg_id, fields in msgs:
                        last_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                        raw_data = fields.get(b"data") or fields.get("data")
                        if not raw_data:
                            continue
                        data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                        data = json.loads(data_str)
                        quote_cache.set_quote(
                            data["instrument_id"],
                            float(data["bid_price"]),
                            float(data["ask_price"]),
                            float(data.get("bid_size", 1.0)),
                            float(data.get("ask_size", 1.0)),
                            int(data.get("arrival_ns", time.time_ns())),
                            sizes_known="bid_size" in data and "ask_size" in data,
                        )
                        arrival_ns = int(data.get("arrival_ns", time.time_ns()))
                        if pool is not None and engine.dispatcher.open_orders and time.time_ns() - arrival_ns <= MAX_MATCH_QUOTE_AGE_NS:
                            try:
                                await match_resting_orders(
                                    pool, engine, to_canonical_instrument(data["instrument_id"]),
                                    float(data["bid_price"]), float(data["ask_price"]),
                                    data.get("bid_size"), data.get("ask_size"),
                                )
                            except Exception as e:
                                logger.error("Paper order matching failed for %s: %s", data["instrument_id"], e)
            except Exception as e:
                logger.debug("consume_quotes error: %s", e)
                await asyncio.sleep(0.5)

    async def _consume_health():
        last_id = "$"
        while True:
            try:
                res = await redis.xread({health_stream: last_id}, count=50, block=200)
                if not res:
                    continue
                for _, msgs in res:
                    for msg_id, fields in msgs:
                        last_id = msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                        raw_data = fields.get(b"data") or fields.get("data")
                        if not raw_data:
                            continue
                        data_str = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else str(raw_data)
                        data = json.loads(data_str)
                        can_inst = to_canonical_instrument(data["instrument_id"])
                        engine.ingress_health.update_health(
                            instrument_id=can_inst,
                            session_id=str(data.get("ingest_session_id", "default")),
                            seq=int(data.get("publish_sequence", 0)),
                            event_ts_ns=int(data.get("event_ts_ns", time.time_ns())),
                            arrival_ts_ns=int(data.get("arrival_ts_ns", time.time_ns())),
                        )
            except Exception as e:
                logger.debug("consume_health error: %s", e)
                await asyncio.sleep(0.5)

    await asyncio.gather(_consume_quotes(), _consume_health())


PERP_MARK_CHANNEL = "channel:perp_mark_ticks"  # published by cryptonite_scanner_service.derivatives_feed
PERP_MARK_KEY_PATTERN = "perp:mark:*"


def apply_perp_mark_tick(engine, raw) -> dict[str, Any] | None:
    """Feed one perp.mark tick (JSON from the derivatives feed) into the dispatcher; return it parsed."""
    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if data.get("type") != "perp.mark":
        return None
    tick = {
        "instrument_id": data["instrument_id"],
        "mark_price": Decimal(str(data["mark_price"])),
        "funding_rate": Decimal(str(data["funding_rate"])),
        "next_funding_time_ns": int(data["next_funding_time_ns"]),
    }
    engine.dispatcher.update_mark_price(tick["instrument_id"], tick["mark_price"], int(data["ts_event_ns"]))
    return tick


async def settle_funding_on_rollover(engine, funding_state: dict[str, dict[str, Any]], tick: dict[str, Any] | None):
    """Settle funding when a symbol's next funding time rolls forward.

    The settlement is booked at the previous funding time with the rate and mark from the last
    tick before the rollover. Only one settlement is booked per rollover, so settlements missed
    while disconnected are not reconstructed.
    """
    if tick is None:
        return
    prev = funding_state.get(tick["instrument_id"])
    if prev is not None and tick["next_funding_time_ns"] < prev["next_funding_time_ns"]:
        return  # out-of-order tick; keep the newer state
    funding_state[tick["instrument_id"]] = tick
    if prev is None or tick["next_funding_time_ns"] == prev["next_funding_time_ns"]:
        return
    payments, liquidations = await engine.process_funding_settlement(
        prev["instrument_id"], prev["mark_price"], prev["funding_rate"], prev["next_funding_time_ns"]
    )
    for p in payments:
        logger.info(
            "Funding settled %s @ %s: rate %s, cash flow %s (cumulative %s)",
            p.instrument_id, p.funding_time_ns, p.funding_rate, p.cash_flow, p.cumulative_funding,
        )
    for liq in liquidations:
        logger.critical("Funding-triggered liquidation %s: %s %s", liq.client_order_id, liq.instrument_id, liq.side)


async def consume_perp_marks(
    redis,
    engine,
    channel: str = PERP_MARK_CHANNEL,
    key_pattern: str = PERP_MARK_KEY_PATTERN,
    liquidation_interval_s: float = 1.0,
    pool=None,
):
    """Keep dispatcher mark prices current and run liquidation checks on fresh marks.

    Seeds from the cached ``perp:mark:*`` keys, then follows the pub/sub channel. A tick whose
    next funding time rolled forward settles funding (``settle_funding_on_rollover``). Every
    ``liquidation_interval_s`` it calls ``engine.process_liquidations`` with the marks that are
    still fresh, so a stalled feed never liquidates on stale prices.
    """
    funding_state: dict[str, dict[str, Any]] = {}
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            async for key in redis.scan_iter(match=key_pattern):
                cached = await redis.get(key)
                if cached:
                    await settle_funding_on_rollover(engine, funding_state, apply_perp_mark_tick(engine, cached))
            last_check = 0.0
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=liquidation_interval_s)
                if msg and msg.get("type") == "message":
                    await settle_funding_on_rollover(engine, funding_state, apply_perp_mark_tick(engine, msg["data"]))
                now = time.monotonic()
                if now - last_check >= liquidation_interval_s:
                    last_check = now
                    fresh = engine.dispatcher.fresh_mark_prices()
                    # Stops lost with a restart are placed again first, once their mark is known
                    if fresh and pool is not None and getattr(engine, "pending_stop_restores", None):
                        await restore_pending_stops(pool, engine)
                    # Resting stops first: a stop the mark reached fires before the liquidation check
                    if fresh and pool is not None and any(engine.dispatcher.is_stop_order(o)
                                                          for o in engine.dispatcher.open_orders.values()):
                        await fire_resting_stops(pool, engine, fresh)
                    if fresh and engine.dispatcher.perp_positions:
                        for liq in await engine.process_liquidations(fresh):
                            logger.critical(
                                "Synthetic liquidation %s: %s %s %s @ %s (mark %s)",
                                liq.client_order_id, liq.instrument_id, liq.side, liq.quantity,
                                liq.fill_price, liq.mark_price,
                            )
                    # Last: the AI perps' stops trail the best mark (plan A4). It touches Postgres and Redis without
                    # their own timeouts, so it has a budget and a failure or a stall never delays the liquidations
                    if fresh and pool is not None and (
                            getattr(engine, "stop_ratchet", None)
                            or any(engine.dispatcher.is_stop_order(o) for o in engine.dispatcher.open_orders.values())):
                        try:
                            await asyncio.wait_for(ratchet_resting_stops(pool, engine, redis, fresh),
                                                   STOP_RATCHET_TIMEOUT_S)
                        except TimeoutError:
                            logger.error("Stop ratchet pass timed out after %ss; skipped", STOP_RATCHET_TIMEOUT_S)
                        except Exception as e:
                            logger.error("Stop ratchet pass failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Perp mark consumer error, resubscribing: %s", e)
            await asyncio.sleep(1.0)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


async def mark_deploy_failed(pool, redis, instance_id: str, instrument_id: str, error: str) -> None:
    """A strategy instance the engine could not start: FAILED, never left RUNNING on no strategy, with an alert
    saying why (an instance on a venue-less id such as XRPUSDT sat RUNNING with no orders, 2026-10-21)."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE paper_strategy_instances SET status = 'FAILED', updated_at = now() "
                "WHERE instance_id = $1 AND status = 'RUNNING'", instance_id)
            await alert(conn, redis, "STRATEGY_DEPLOY_FAILED",
                        f"Strategy instance {instance_id} on {instrument_id} could not start and was marked FAILED: {error}",
                        {"instance_id": instance_id, "instrument_id": instrument_id, "error": error[:300]},
                        dedupe_key=f"deploy-failed:{instance_id}")
    except Exception as e:
        logger.error("Could not mark instance %s FAILED: %s", instance_id, e)


BRACKET_RELOAD_INTERVAL_S = 300.0


async def reload_leverage_brackets(redis, interval_s: float = BRACKET_RELOAD_INTERVAL_S) -> None:
    """Install the leverage bracket tables the derivatives feed caches daily (leverage plan 1.2), so the
    dispatcher values margin and liquidation with them and refuses entries on missing or stale tables."""
    from cryptonite_execution.brackets import load_brackets

    while True:
        try:
            loaded = await load_brackets(redis)
            logger.debug("Loaded %d leverage bracket tables", loaded)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Could not load leverage brackets: %s", e)
        await asyncio.sleep(interval_s)


SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
DAILY_DRAWDOWN_REASON = "DAILY_DRAWDOWN_LIMIT_BREACHED"


async def risk_monitor_once(engine, pool, redis, now: datetime.datetime | None = None) -> None:
    """One risk-monitor pass: UTC day rollover, DB kill-switch sync, daily drawdown breaker."""
    d = engine.dispatcher
    now = now or datetime.datetime.now(datetime.UTC)
    equity = d.portfolio_equity()

    # 1. UTC midnight: re-base the drawdown high-water mark and clear daily PnL / daily-loss latch
    if d.roll_trading_day(now.date(), equity):
        logger.info("UTC trading day rolled to %s; drawdown base equity %s", now.date(), equity)
        await pool.execute(
            "UPDATE paper_accounts SET daily_realized_pnl = 0, daily_loss_latched = false, utc_trading_day = $2, updated_at = now() WHERE account_id = $1",
            engine.account_id, now.date(),
        )

    # 2. A PAPER latch written straight to the DB (e.g. /control/kill-switch/trip) must stop the engine too
    row = await pool.fetchrow(
        "SELECT is_latched, latched_by, reason FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
        engine.account_id,
    )
    if row and row["is_latched"] and not engine.kill_switch.is_latched:
        engine.kill_switch.trip(latched_by=row["latched_by"] or "database", reason=row["reason"] or "Latched in database")
        logger.critical("Paper kill switch latched from database (%s)", row["reason"])

    # 3. Daily drawdown circuit breaker: latch entries, trip + persist the kill switch, alert
    was_latched = d.daily_loss_latched
    if d.check_daily_drawdown(equity) and not was_latched:
        hwm = d.high_water_mark
        drawdown = (hwm - equity) / hwm if hwm else Decimal("0")
        if not engine.kill_switch.is_latched:
            engine.kill_switch.trip(latched_by="risk-monitor", reason=DAILY_DRAWDOWN_REASON)
        await pool.fetchval(
            "SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", engine.account_id, DAILY_DRAWDOWN_REASON, "risk-monitor"
        )
        await pool.execute("UPDATE paper_accounts SET daily_loss_latched = true, updated_at = now() WHERE account_id = $1", engine.account_id)
        detail = {"equity": str(equity), "high_water_mark": str(hwm), "drawdown_pct": str(drawdown), "limit_pct": str(d.max_daily_loss_pct)}
        await pool.execute(
            """INSERT INTO paper_risk_events (event_id, account_id, rule_name, severity, message, blocked_action, occurred_at)
               VALUES (gen_random_uuid(), $1, $2, 'CRITICAL', $3, $4::jsonb, now())""",
            engine.account_id, DAILY_DRAWDOWN_REASON,
            f"Daily drawdown {drawdown:.2%} >= {d.max_daily_loss_pct:.0%}: kill switch tripped", json.dumps(detail),
        )
        if redis is not None:
            await redis.publish(SYSTEM_ALERTS_CHANNEL, json.dumps({
                "type": "system_alert", "v": 1, "kind": DAILY_DRAWDOWN_REASON, "account_id": engine.account_id, **detail,
            }))
        logger.critical("DAILY DRAWDOWN BREACH %.2f%%: kill switch tripped", float(drawdown * 100))


async def risk_monitor(engine, pool, redis, interval_s: float = 1.0, reconcile_every: int = 5,
                       mandate_every_s: float = 5.0):
    tick = 0
    last_mandate_pass = float("-inf")
    while True:
        try:
            await risk_monitor_once(engine, pool, redis)
            tick += 1
            if tick % reconcile_every == 0:
                cancel_entries_if_latched(engine.dispatcher)
                async with pool.acquire() as conn:
                    await reconcile_working_orders(conn, engine)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Risk monitor error: %s", e)
        if time.monotonic() - last_mandate_pass >= mandate_every_s:
            # Mandate hard stops, the account backstop (A5.2) and approval timeouts (A5.3), on their own
            # timer and isolated, so a failing pass above can neither stop nor speed them up
            last_mandate_pass = time.monotonic()
            try:
                await mandate_monitor_once(engine, pool, redis)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Mandate monitor error: %s", e)
            try:
                await sweep_approvals_once(pool)  # D8 approval timeouts (A5.3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Approval sweeper error: %s", e)
        await asyncio.sleep(interval_s)


# --- Poison commands (WS-URGENT U-A) ----------------------------------------------------------------------------------
# A command whose attempt raises must never stay at the queue head. The attempt's claim transaction has rolled back,
# so the command is PENDING again; the poller settles it in a new short transaction instead (settle_failed_command).
MAX_COMMAND_ATTEMPTS = 5  # attempts for a transient error before the command is settled
TRANSIENT_RETRY_BACKOFF_S = 0.5  # times the attempt number, so a flapping database is not hammered
TRANSIENT_ERROR_DEPTH = 5  # how far down the __cause__ / __context__ chain a transient error is looked for
# What counts as transient: the connection or the server went away or is saturated, a socket or asyncio timeout, or
# the database rolled the transaction back for a deadlock or a serialization failure. Anything else (a bad payload, a
# constraint violation, a bug) is not retried. The chain matters: when the server ends a connection inside a
# transaction, asyncpg raises InterfaceError ("the underlying connection is closed") from the transaction's exit, and
# the original error is only in __context__.
TRANSIENT_COMMAND_ERRORS = (
    ConnectionError,  # socket reset, refused, broken pipe
    TimeoutError,  # asyncio timeouts (asyncio.TimeoutError is this class on Python 3.11+)
    asyncpg.exceptions.PostgresConnectionError,  # SQLSTATE class 08: connection lost or never made, 08003 included
    asyncpg.exceptions.TooManyConnectionsError,  # 53300: the server or pool is saturated
    asyncpg.exceptions.CannotConnectNowError,  # 57P03: the server is starting up or shutting down
    asyncpg.exceptions.AdminShutdownError,  # 57P01: the server ended the session
    asyncpg.exceptions.DeadlockDetectedError,  # 40P01: a deadlock, the database picks this transaction to roll back
    asyncpg.exceptions.SerializationError,  # 40001: a serialization failure. Not 40002 (integrity) or 40003 (unknown)
)
# asyncpg's client side also raises InterfaceError when the connection is closed. The other InterfaceErrors are misuse
# or configuration (another operation in progress, an unsupported feature) and a DataError is a bad argument: none of
# those is transient, so only InterfaceError with this text counts.
CLOSED_CONNECTION_TEXT = "connection is closed"
SETTLE_ATTEMPTS_BEFORE_FALLBACK = 3  # failed settlements of one command before the bare status change
SETTLE_BACKOFF_S = 0.5  # the pause after the first failed settlement; it doubles with each further failure
SETTLE_BACKOFF_CAP_S = 5.0
MAX_ERROR_MESSAGE_CHARS = 500  # error_message and alert text are capped; NUL bytes go (PostgreSQL refuses them)
FALLBACK_ERROR = "COMMAND_FAILED: settlement failed"
RUNBOOK = "docs/runbooks/command-queue.md"
MANDATE_INSTANCE_REFUSAL = (
    "MANDATE_INSTANCE: AI mandates are paused and resumed in Mission Control, not as strategy instances"
)
MANDATE_REFUSED_COMMANDS = ("DEPLOY", "PAUSE", "RESUME", "STOP")
# Claimed ahead of every older command. A stop queued behind other work waits for all of it: the WLD trailing stop of
# 2026-10-17 06:55 waited 5 h 10 min behind two RESUME commands that failed on every attempt, and a +2.3% exit became
# -0.58%. Failing commands are settled now instead of retried, but a backlog, a slow command or a future poison command
# must still never hold a stop or the kill switch.
RISK_REDUCING_COMMANDS = ("TRIP_KILL_SWITCH", "CLOSE_POSITION")

# Settled only from PENDING. FAILED is the end. UNKNOWN is RUNNING with the demo pipeline's ambiguous-outcome outcome:
# the claim never takes a RUNNING row, so it is never claimed or resubmitted, and the stuck-command alert shows it.
_FAILED_SQL = """
    UPDATE execution_commands
    SET status = 'FAILED', command_outcome = 'REJECTED', error_message = $2, processed_at = now()
    WHERE command_id = $1 AND status = 'PENDING' AND socket_write_started = false
    RETURNING command_id
"""
_UNKNOWN_SQL = """
    UPDATE execution_commands
    SET status = 'RUNNING', command_outcome = 'UNKNOWN_PENDING_RECONCILIATION', error_message = $2
    WHERE command_id = $1 AND status = 'PENDING'
    RETURNING command_id
"""
_ATTEMPT_SQL = "UPDATE execution_commands SET attempt_count = $2 WHERE command_id = $1 AND status = 'PENDING'"
# The last resort after settle_failed_command has failed repeatedly. A held outcome (effects may exist) is UNKNOWN,
# never FAILED; otherwise FAILED. Bare on purpose: no alert row, the stuck-command monitor shows a held one.
# The socket guard applies to FAILED only, as in _FAILED_SQL: a held outcome may have written its socket.
_FALLBACK_SQL = """
    UPDATE execution_commands
    SET status = CASE WHEN $3 THEN 'RUNNING' ELSE 'FAILED' END,
        command_outcome = CASE WHEN $3 THEN 'UNKNOWN_PENDING_RECONCILIATION' ELSE 'REJECTED' END,
        error_message = $2,
        processed_at = CASE WHEN $3 THEN processed_at ELSE now() END
    WHERE command_id = $1 AND status = 'PENDING' AND ($3 OR socket_write_started = false)
"""
_OUTCOME_ALERT_SQL = """
    INSERT INTO notifications_outbox (tier, kind, title, body, payload, dedupe_key)
    VALUES ('CRITICAL', 'COMMAND_FAILED', $1, $2, $3::jsonb, $4)
    ON CONFLICT (dedupe_key) DO NOTHING
"""
_REFUSAL_ALERT_SQL = """
    INSERT INTO notifications_outbox (tier, kind, title, body, payload, dedupe_key)
    VALUES ('INFO', 'COMMAND_REFUSED', $1, $2, $3::jsonb, $4)
    ON CONFLICT (dedupe_key) DO NOTHING
"""


def _is_transient_one(error: BaseException) -> bool:
    if isinstance(error, TRANSIENT_COMMAND_ERRORS):
        return True
    return isinstance(error, asyncpg.exceptions.InterfaceError) and CLOSED_CONNECTION_TEXT in str(error).lower()


def is_transient_command_error(exc: BaseException) -> bool:
    """True when ``exc``, or an exception it was raised from or during (at most TRANSIENT_ERROR_DEPTH links), is
    transient."""
    current: BaseException | None = exc
    for _ in range(TRANSIENT_ERROR_DEPTH):
        if current is None:
            return False
        if _is_transient_one(current):
            return True
        following = current.__cause__ if current.__cause__ is not None else current.__context__
        if following is current:
            return False
        current = following
    return False


def sanitised(text: object, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    """``text`` as it is stored in error_message and alert payloads: no NUL bytes (PostgreSQL refuses them), capped."""
    return str(text).replace("\x00", "")[:limit]


def command_failure_code(exc: BaseException) -> str:
    return sanitised(f"COMMAND_FAILED: {type(exc).__name__}: {str(exc)[:300]}")


def settle_backoff_s(failures: int) -> float:
    """The pause after the ``failures``-th failed settlement of one command: exponential, capped."""
    return min(SETTLE_BACKOFF_CAP_S, SETTLE_BACKOFF_S * 2 ** max(0, failures - 1))


async def _transaction(conn):
    """The connection's transaction as an async context manager (a null one where the connection has none)."""
    tx = conn.transaction() if hasattr(conn, "transaction") else None
    if asyncio.iscoroutine(tx):
        tx = await tx
    return tx if hasattr(tx, "__aenter__") else contextlib.nullcontext()


def _payload_dict(raw) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _instrument_of(command_type: str, target: str | None, payload: dict | None) -> str:
    return (payload or {}).get("instrument_id") or target or "-"


def _outcome_text(decision: str, command_type: str, target: str | None, instrument: str, code: str,
                  command_id) -> tuple[str, str]:
    """The title and body of the CRITICAL alert for a settled command."""
    if decision == "FAILED":
        return "Paper command failed", f"{command_type} for {target or '-'} was not run: {code}."
    body = (f"{command_type} for {target or '-'} may have applied effects before it failed ({code}). It is held as "
            "UNKNOWN_PENDING_RECONCILIATION and is never retried.")
    if command_type == "CLOSE_POSITION":  # the monitors treat an unresolved close as in flight (mandate_monitor, exit)
        body += f" So stops for {instrument} are blocked until this command is resolved."
    body += f" Command {command_id}: see {RUNBOOK}."
    return "Paper command outcome unknown", body


async def settle_failed_command(pool, command_id, command_type, target, exc: BaseException, *, attempt: int,
                                effects_applied: bool, payload: dict | None = None, compensated: bool = False) -> str:
    """Settle a command whose attempt raised, and return the decision: RETRY, FAILED, UNKNOWN or NOOP.

    ``attempt`` is the attempt just made (the claim's count). A transient error keeps the command PENDING for another
    attempt until MAX_COMMAND_ATTEMPTS, and its count is kept in attempt_count (the claim's own increment was rolled
    back). A transient error is retried only while no effect may exist: after an attempt that applied an effect
    (``effects_applied``: an order dispatched, a fill booked, a mailbox command sent, a reservation released) it is
    held as UNKNOWN, unless that effect was undone again (``compensated``). Any other error settles at once: FAILED,
    unless the attempt may already have applied an effect or its socket write started. Those are
    UNKNOWN_PENDING_RECONCILIATION instead: held, never retried blindly.
    NOOP: the command is no longer PENDING (it committed or settled on its own), so nothing is changed.
    The settlement runs in one short transaction with a 2 s lock timeout, so a row held by a half-open backend cannot
    stall the poller; a failure raises to the caller, which counts it (settle_after_error).
    """
    code = command_failure_code(exc)
    if is_transient_command_error(exc) and attempt < MAX_COMMAND_ATTEMPTS and (compensated or not effects_applied):
        logger.warning("Command %s: transient %s, attempt %d of %d; retrying", command_id, code, attempt,
                       MAX_COMMAND_ATTEMPTS)
        async with pool.acquire() as conn:
            await conn.execute(_ATTEMPT_SQL, command_id, attempt)
        return "RETRY"
    instrument = _instrument_of(command_type, target, payload)
    async with pool.acquire() as conn:
        async with await _transaction(conn):
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            decision = "FAILED"
            settled = None if effects_applied else await conn.fetchval(_FAILED_SQL, command_id, code)
            if settled is None:  # effects may exist, or the socket was written: the outcome is unknown
                decision = "UNKNOWN"
                settled = await conn.fetchval(_UNKNOWN_SQL, command_id, code)
            if settled is None:
                return "NOOP"
            title, body = _outcome_text(decision, command_type, target, instrument, code, command_id)
            logger.error("Command %s settled as %s: %s", command_id, decision, code)
            detail = {"command_id": str(command_id), "command_type": command_type, "target": target,
                      "instrument_id": instrument, "decision": decision, "error": code}
            try:  # the outbox alert is best effort: a savepoint, so a failed insert cannot undo the settlement above
                async with await _transaction(conn):
                    await conn.execute(_OUTCOME_ALERT_SQL, title, body, json.dumps(detail),
                                       f"command-failed:{command_id}")
            except Exception as e:
                logger.error("Could not publish the outcome of command %s: %s", command_id, e)
    return decision


def _rows_changed(command_tag) -> int:
    """The row count of an asyncpg command tag such as "UPDATE 1"; 0 when there is none to read."""
    parts = str(command_tag).split()
    return int(parts[-1]) if len(parts) == 2 and parts[-1].isdigit() else 0


async def fail_command_fallback(pool, command_id, *, held: bool) -> str:
    """Close a command after settle_failed_command has failed SETTLE_ATTEMPTS_BEFORE_FALLBACK times: one bare status
    change in a short transaction. ``held``: the outcome may exist, so UNKNOWN_PENDING_RECONCILIATION, never FAILED.
    Returns UNKNOWN or FAILED, or NOOP when no row changed (it was no longer PENDING): then nothing was closed."""
    async with pool.acquire() as conn:
        async with await _transaction(conn):
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            command_tag = await conn.execute(_FALLBACK_SQL, command_id, FALLBACK_ERROR, held)
    if _rows_changed(command_tag) == 0:
        return "NOOP"
    return "UNKNOWN" if held else "FAILED"


@dataclasses.dataclass
class _Unsettled:
    """A command whose last attempt raised and whose settlement failed: the poller's in-process state, by command id."""
    failures: int  # failed settlements so far
    exc: BaseException  # the error the attempt raised: the settlement decides on it, the command is not run again
    effects_applied: bool
    compensated: bool


class _SettleOnly(Exception):
    """Raised inside a claim to roll it back: the command's earlier attempt is settled, and the command is not run."""


async def settle_after_error(pool, row, exc: BaseException, *, effects_applied: bool, compensated: bool = False,
                             unsettled: dict[str, _Unsettled]) -> float:
    """Settle the command an attempt raised on; return the seconds the poller waits before its next pass.

    A settlement that itself fails leaves the command PENDING, and the next pass would claim it again. So the error and
    each failed settlement are kept per command id (``unsettled``, in-process): the next pass claims the command, but
    it settles the kept error instead of running the command again. The pass waits settle_backoff_s(n) after the n-th
    failed settlement, and after SETTLE_ATTEMPTS_BEFORE_FALLBACK failures the command is closed by the fallback.
    """
    key = str(row["command_id"])
    attempt = int(row.get("attempt_count") or 0)
    try:
        decision = await settle_failed_command(
            pool, row["command_id"], row["command_type"], row["target_instance_id"], exc,
            attempt=attempt, effects_applied=effects_applied, compensated=compensated,
            payload=_payload_dict(row["payload"]),
        )
    except Exception as settle_error:  # the database is unreachable or the row is locked: counted below
        logger.error("Could not settle command %s: %s", key, settle_error)
        decision = None
    if decision is not None:
        unsettled.pop(key, None)
        return TRANSIENT_RETRY_BACKOFF_S * attempt if decision == "RETRY" else 0.0
    failures = (unsettled[key].failures if key in unsettled else 0) + 1
    unsettled[key] = _Unsettled(failures, exc, effects_applied, compensated)
    if failures >= SETTLE_ATTEMPTS_BEFORE_FALLBACK:
        held = effects_applied or bool(row.get("socket_write_started"))
        try:
            outcome = await fail_command_fallback(pool, row["command_id"], held=held)
        except Exception as fallback_error:
            logger.error("Fallback settlement of command %s failed: %s", key, fallback_error)
        else:
            unsettled.pop(key, None)
            if outcome == "NOOP":
                logger.error("Command %s was no longer PENDING at the fallback: nothing was changed", key)
            else:
                logger.error("Command %s closed as %s by the fallback after %d failed settlements", key, outcome,
                             failures)
            return 0.0
    return settle_backoff_s(failures)


async def alert_short_close(conn, row, target: str, instrument_id: str, wanted: Decimal, sold: Decimal,
                            account_qty: Decimal) -> None:
    """A CRITICAL alert for a mandate close that sold less than the mandate owns (``account_qty``: what the
    dispatcher held). ``alert`` writes it in a savepoint, so a failed alert never undoes the close."""
    reason = _payload_dict(row["payload"]).get("reason") or "-"
    detail = {"command_id": str(row["command_id"]), "target": target, "instrument_id": instrument_id, "reason": reason,
              **{k: format(v.normalize(), "f") for k, v in (("wanted", wanted), ("sold", sold), ("account_qty", account_qty))}}
    message = (f"Stop close short: {reason} for {target} sold {detail['sold']} of its {detail['wanted']} {instrument_id}; "
               f"the account held only {detail['account_qty']}. The rest is still booked open: reconcile and close it. "
               f"Runbook: {RUNBOOK}")
    logger.critical(message)
    await alert(conn, None, "CLOSE_SHORT", message, detail, dedupe_key=f"close-short:{row['command_id']}")


async def command_poller(pool, engine, quote_cache=None):
    """Poll execution_commands for PAPER environment commands."""
    if quote_cache is None:
        quote_cache = SimpleQuoteCache()
    unsettled: dict[str, _Unsettled] = {}  # a command whose last attempt raised and whose settlement failed
    while True:
        row = None  # the command this pass claimed; its claim is rolled back if the attempt raises
        effects_applied = False  # set before an attempt's first in-memory or outbox effect (settle_failed_command)
        compensated = False  # set once the attempt's reservation is released again: its effect is undone
        try:
            async with pool.acquire() as conn:
                tx = conn.transaction() if hasattr(conn, "transaction") else None
                if asyncio.iscoroutine(tx):
                    tx = await tx
                cm = tx if hasattr(tx, "__aenter__") else contextlib.nullcontext()
                async with cm:
                    row = await conn.fetchrow("""
                        WITH claimed AS (
                            SELECT command_id FROM execution_commands
                            WHERE status = 'PENDING'
                              AND environment = 'PAPER'
                              AND command_type IN ('DEPLOY', 'STOP', 'PAUSE', 'RESUME', 'CLOSE_POSITION', 'MANUAL_ORDER', 'SUBMIT_ORDER', 'CANCEL_ORDER', 'TRIP_KILL_SWITCH', 'RESET_SWITCH', 'KILL_SWITCH_RESET_REQUEST')
                              AND (lease_expires_at IS NULL OR lease_expires_at < now())
                            -- risk-reducing commands first (blueprint D8): a stop never waits behind older work
                            ORDER BY command_type = ANY($1::text[]) DESC, created_at ASC LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE execution_commands
                        SET status = 'RUNNING',
                            lease_owner = 'cryptonite-orchestrator',
                            lease_expires_at = now() + INTERVAL '30 seconds',
                            attempt_id = gen_random_uuid(),
                            attempt_count = attempt_count + 1
                        FROM claimed
                        WHERE execution_commands.command_id = claimed.command_id
                        RETURNING execution_commands.*
                    """, list(RISK_REDUCING_COMMANDS))
                    if row:
                        if str(row["command_id"]) in unsettled:
                            # Its last attempt raised and could not be settled: the claim is rolled back and the
                            # stored error is settled, so the command is never run a second time
                            raise _SettleOnly()
                        cmd_type = row["command_type"]
                        target = row["target_instance_id"]
                        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                        acc_id = getattr(engine, "account_id", ACCOUNT_ID)
                        
                        trace_id = row.get("decision_trace_id") or payload.get("decision_trace_id")
                        logger.info("Orchestrator processing %s for %s (decision_trace_id=%s)", cmd_type, target, trace_id)
                        
                        if cmd_type in MANDATE_REFUSED_COMMANDS and (target or "").startswith(MANDATE_INSTANCE_PREFIX):
                            # AI mandates are paused, resumed and stopped in Mission Control only. The API refuses these
                            # with a 409; this is the defence in depth, refused before any effect so the queue moves on.
                            await conn.execute(
                                "UPDATE execution_commands SET status = 'FAILED', command_outcome = 'REJECTED', "
                                "error_message = $2, processed_at = now() WHERE command_id = $1",
                                row["command_id"], MANDATE_INSTANCE_REFUSAL,
                            )
                            # an INFO outbox row, like COMMAND_FAILED, so the refusal is visible to the operator. In a
                            # savepoint: a failed alert cannot undo the refusal above (nor roll the claim back)
                            try:
                                async with await _transaction(conn):
                                    await conn.execute(
                                        _REFUSAL_ALERT_SQL, "Paper command refused",
                                        f"{cmd_type} for {target} was refused: {MANDATE_INSTANCE_REFUSAL}.",
                                        json.dumps({"command_id": str(row["command_id"]), "command_type": cmd_type,
                                                    "target": target, "error": MANDATE_INSTANCE_REFUSAL}),
                                        f"command-refused:{row['command_id']}",
                                    )
                            except Exception as e:
                                logger.error("Could not publish the refusal of command %s: %s", row["command_id"], e)
                            logger.warning("Refused %s for AI mandate %s (command %s)",
                                           cmd_type, target, row["command_id"])
                            continue

                        if cmd_type == "DEPLOY":
                            await conn.execute("""
                                INSERT INTO paper_strategy_instances (
                                    instance_id, account_id, strategy_name, strategy_version_id,
                                    strategy_source_hash, risk_policy_hash, last_processed_bar_ns,
                                    instrument_id, timeframe, resolved_parameters, allocation_usd,
                                    status, created_at, updated_at
                                ) VALUES ($1, $2, $3, $4::uuid, $9, $10, 0, $5, $6, $7::jsonb, $8, 'RUNNING', now(), now())
                                ON CONFLICT (instance_id) DO UPDATE SET status = 'RUNNING', updated_at = now();
                            """,
                                target, payload.get("account_id") or acc_id, payload["strategy_name"],
                                payload["strategy_version_id"], payload["instrument_id"],
                                payload["timeframe"], json.dumps(payload.get("parameters", {})),
                                payload.get("allocation_usd", Decimal("0")),
                                await instance_source_hash(conn, payload["strategy_name"], payload["strategy_version_id"]),
                                risk_policy_hash(engine.dispatcher),
                            )
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("STOP", "PAUSE"):
                            await conn.execute("UPDATE paper_strategy_instances SET status = $2, updated_at = now() WHERE instance_id = $1", target, cmd_type)
                            effects_applied = True
                            engine.mailbox.push({"command_type": cmd_type, "target_instance_id": target})
                            engine.process_mailbox_commands()
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "RESUME":
                            await conn.execute("UPDATE paper_strategy_instances SET status = 'RUNNING', updated_at = now() WHERE instance_id = $1", target)
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "CLOSE_POSITION":
                            inst_raw = payload.get("instrument_id", "")
                            canonical_inst = to_canonical_instrument(inst_raw) if inst_raw else ""
                            # A close with a quantity closes only the target's own holding (a stop on an instrument
                            # another owner shares): only the target's working orders go with it
                            owner_scoped = bool(payload.get("quantity")) and target is not None

                            def release_instrument_orders(keep_stops: bool = False):
                                # Working orders on the instrument would outlive the position being closed; a partial
                                # perp close keeps the resting stop (it shrinks to what is left)
                                for cid, o in list(engine.dispatcher.open_orders.items()):
                                    if o.get("instrument_id") != canonical_inst:
                                        continue
                                    if owner_scoped and o.get("instance_id") != target:
                                        continue
                                    if keep_stops and engine.dispatcher.is_stop_order(o):
                                        continue
                                    engine.dispatcher.release_reservation(cid)

                            pos_qty = engine.dispatcher.get_position_qty(canonical_inst)
                            perp_qty = engine.dispatcher.get_perp_position_qty(canonical_inst)
                            position_side = str(payload.get("position_side") or "").upper()
                            if position_side in ("LONG", "SHORT") and (
                                    perp_qty == 0 or (perp_qty > 0) != (position_side == "LONG")):
                                # A reduction of a LONG/SHORT (a debate's) never closes the other side: the position it
                                # was routed against closed meanwhile and a new one may have opened. A no-op, nothing
                                # released or filled
                                logger.warning("CLOSE_POSITION %s for %s skipped: reduces a %s but the live perp "
                                               "position is %s", row["command_id"], canonical_inst, position_side, perp_qty)
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                                continue
                            own_spot = None  # a mandate's own filled spot holding: what its close must sell
                            if perp_qty == 0 and mandate_id_from_instance(target) is not None:
                                own_spot = (await instance_spot_holdings(conn, target)).get(canonical_inst, (Decimal("0"), None))[0]
                            sold = Decimal("0")
                            if pos_qty > Decimal("0") or perp_qty != 0:
                                # Spot long closes with a SELL; a perp closes with an opposing reduce-only order
                                close_side = "BUY" if perp_qty < 0 else "SELL"
                                close_qty = abs(perp_qty) if perp_qty != 0 else pos_qty
                                if perp_qty == 0 and payload.get("quantity"):
                                    # a mandate stop closes only the mandate's own quantity (A5.2)
                                    close_qty = min(pos_qty, Decimal(str(payload["quantity"])))
                                elif perp_qty != 0 and payload.get("quantity"):
                                    # a partial perp close (the TP1 scale-out): a reduce-only fill of that quantity
                                    close_qty = min(abs(perp_qty), Decimal(str(payload["quantity"])))
                                if mandate_id_from_instance(target) is not None:
                                    # A mandate's close never sells what it does not own, whatever the
                                    # (possibly stale) payload says: cap at its own filled holding, and
                                    # refuse a perp another instance owns
                                    if perp_qty != 0:
                                        owner = getattr(engine.dispatcher.perp_positions.get(canonical_inst), "instance_id", None)
                                        if owner != target:
                                            await conn.execute(
                                                "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                                row["command_id"], "CLOSE_NOT_OWNED",
                                            )
                                            continue
                                    else:
                                        close_qty = min(close_qty, own_spot)
                            if (pos_qty > Decimal("0") or perp_qty != 0) and close_qty <= 0:
                                # nothing of the target's own left to close
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                                continue
                            if pos_qty > Decimal("0") or perp_qty != 0:
                                quote = quote_cache.quote(canonical_inst)
                                if quote:
                                    fill_px = Decimal(str(quote.ask_price if close_side == "BUY" else quote.bid_price))
                                else:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "NO_LIVE_QUOTE",
                                    )
                                    continue
                                effects_applied = True
                                partial_perp = perp_qty != 0 and close_qty < abs(perp_qty)
                                release_instrument_orders(keep_stops=partial_perp)
                                comm = close_qty * fill_px * fee_rate_for(engine.dispatcher, canonical_inst)
                                client_oid = f"CLOSE-{uuid.uuid4().hex[:8].upper()}"
                                instance_id = target or "MANUAL_TRADE"

                                close_position = engine.dispatcher.perp_positions.get(canonical_inst) if perp_qty else None
                                close_entry_price = close_position.entry_price if close_position is not None else None
                                realized = None
                                if perp_qty != 0:
                                    _, _, realized = engine.dispatcher.flatten_perp_position(
                                        canonical_inst, fill_px, comm, client_oid, quantity=close_qty
                                    )
                                else:
                                    engine.dispatcher.handle_fill(
                                        client_order_id=client_oid,
                                        fill_qty=close_qty,
                                        fill_price=fill_px,
                                        commission=comm,
                                        side="SELL",
                                        instrument_id=canonical_inst,
                                    )
                                    sold = close_qty
                                await conn.execute(
                                    """
                                    INSERT INTO paper_orders (
                                        client_order_id, instance_id, account_id, instrument_id, side,
                                        order_type, quantity, price, filled_qty, avg_fill_price, status,
                                        last_event_version, created_at, updated_at
                                    ) VALUES ($1, $2, $3, $4, $7, 'MARKET', $5, $6, $5, $6, 'FILLED', 1, now(), now());
                                    """,
                                    client_oid, instance_id, acc_id, canonical_inst, close_qty, fill_px, close_side
                                )
                                engine.bridge.enqueue_event({
                                    "event_type": "ORDER_FILLED",
                                    "event_version": 1,
                                    "client_order_id": client_oid,
                                    "account_id": acc_id,
                                    "instance_id": instance_id,
                                    "instrument_id": canonical_inst,
                                    "side": close_side,
                                    "price": str(fill_px),
                                    "quantity": str(close_qty),
                                    "commission": str(comm),
                                    "filled_qty": str(close_qty),
                                    "avg_fill_price": str(fill_px),
                                    "status": "FILLED",
                                    "occurred_at_ns": time.time_ns(),
                                    **engine.dispatcher.fill_event_fields(canonical_inst, realized, fill_px),
                                })
                                await record_profile_trade(
                                    conn, engine, instance_id, canonical_inst, close_side, close_qty, fill_px,
                                    entry=False, position_side="SHORT" if perp_qty < 0 else "LONG",
                                    entry_price=close_entry_price, realized=realized, commission=comm,
                                    command_id=row["command_id"], reason=payload.get("reason") or "MANUAL",
                                    client_order_id=client_oid,
                                )
                            else:
                                effects_applied = True
                                release_instrument_orders()
                            if own_spot is not None:
                                wanted = min(own_spot, Decimal(str(payload["quantity"]))) if payload.get("quantity") else own_spot
                                if sold < wanted:
                                    # The account holds less than the mandate's own position: something else sold
                                    # its coins. Never a silent success: the rest stays open and unprotected
                                    await alert_short_close(conn, row, target, canonical_inst, wanted, sold, pos_qty)
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "CANCEL_ORDER":
                            client_oid = payload.get("client_order_id")
                            if client_oid:
                                await conn.execute(
                                    "UPDATE paper_orders SET status = 'CANCELED', updated_at = now() WHERE client_order_id = $1 AND status IN ('PENDING', 'SUBMITTED', 'ACCEPTED')",
                                    client_oid
                                )
                                effects_applied = True
                                if hasattr(engine.dispatcher, "release_reservation"):
                                    engine.dispatcher.release_reservation(client_oid)
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                            else:
                                await conn.execute("UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = 'Missing client_order_id', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type == "TRIP_KILL_SWITCH":
                            # Latch the in-memory switch the dispatcher checks, then persist it (row + audit)
                            reason = payload.get("reason") or "Manual UI Kill Switch"
                            actor = payload.get("operator") or row.get("requester_id") or "web-ui"
                            effects_applied = True
                            engine.mailbox.push({"command_type": "TRIP_KILL_SWITCH", "requester_id": actor, "payload": {"reason": reason}})
                            engine.process_mailbox_commands()
                            await conn.fetchval("SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", acc_id, reason, actor)
                            await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("RESET_SWITCH", "KILL_SWITCH_RESET_REQUEST"):
                            # RESET_SWITCH: /paper/kill-switch/reset. KILL_SWITCH_RESET_REQUEST: /control reset
                            # (step-up authenticated), which carries an expiry and the latch generation it saw.
                            # Either way the engine refuses while any order or position is open.
                            actor = payload.get("operator") or payload.get("actor_id") or row.get("requester_id") or "web-ui"
                            stale = ""
                            if cmd_type == "KILL_SWITCH_RESET_REQUEST":
                                if payload.get("expires_at_ns") and time.time_ns() > int(payload["expires_at_ns"]):
                                    stale = "RESET_REJECTED: request expired"
                                elif payload.get("expected_generation") is not None:
                                    gen = await conn.fetchval(
                                        "SELECT latch_generation FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
                                        acc_id,
                                    )
                                    if gen is not None and int(gen) != int(payload["expected_generation"]):
                                        stale = f"RESET_REJECTED: latch generation changed ({payload['expected_generation']} -> {gen})"
                            if not stale:
                                effects_applied = True
                                engine.mailbox.push({"command_type": "RESET_SWITCH", "requester_id": actor})
                                engine.process_mailbox_commands()
                            if stale or engine.kill_switch.is_latched:
                                reason = stale or (
                                    f"RESET_REJECTED: open orders={len(engine.dispatcher.open_orders)}, "
                                    f"open positions={engine.dispatcher.open_position_count()}"
                                )
                                await conn.execute("UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1", row["command_id"], reason)
                            else:
                                await conn.execute(
                                    "UPDATE execution_kill_switch SET is_latched = false, reset_at = now(), reset_by = $3, updated_at = now() WHERE environment = $1 AND account_id = $2",
                                    "PAPER", acc_id, actor,
                                )
                                await conn.execute("UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1", row["command_id"])
                        elif cmd_type in ("MANUAL_ORDER", "SUBMIT_ORDER"):
                            inst_raw = payload.get("instrument_id") or payload.get("symbol", "BTCUSDT")
                            canonical_inst = to_canonical_instrument(inst_raw)
                            
                            # Determine side: an explicit side, else the recommendation's direction
                            # (``normalize_verdict``, B2). HOLD or an unknown verdict is no order, never a SELL.
                            raw_side = payload.get("side")
                            if not raw_side:
                                raw_side = verdict_direction(payload.get("recommendation"))
                                if raw_side not in ("BUY", "SELL"):
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], f"NO_ORDER_SIDE: recommendation {payload.get('recommendation')!r}",
                                    )
                                    continue
                            side = raw_side.upper()
                            
                            # AI orders are checked against their mandate before the dispatcher (A5.1) and
                            # are booked under the mandate's own paper instance
                            ai_order = is_ai_command(cmd_type, target, row.get("requester_id"))
                            mandate = None
                            if ai_order and (mandate_ref := command_mandate_id(row, payload)):
                                mandate = await load_mandate(conn, mandate_ref)

                            # Determine quantity
                            is_perp_order = canonical_inst.split(".")[0].endswith("-PERP")
                            perp_entry = is_perp_order and not payload.get("reduce_only")
                            planned_leverage = None
                            sizing_plan = None
                            qty_val = Decimal(str(payload["quantity"])) if payload.get("quantity") else None
                            if qty_val and mandate is not None and perp_entry:
                                # A mandate perp entry with its own quantity is not re-sized, but it is still held to
                                # its setup grade's ceiling (ungraded: grade C), as a plan-sized entry is
                                ceiling = grade_ceiling(payload, getattr(engine, "grade_ceilings", None))
                                if int(payload.get("leverage") or 1) > ceiling:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"],
                                        f"LEVERAGE_ABOVE_GRADE_CEILING: {payload.get('leverage')}x > {ceiling}x for grade "
                                        f"{payload.get('setup_grade') or 'ungraded'}",
                                    )
                                    continue
                            if not qty_val and mandate is not None and perp_entry:
                                # Leverage plan 2.4: a mandate's perp entry, long or short, is sized by
                                # plan_leveraged_order to the mandate's room and risk budget (the allocation is the
                                # margin); the mandate gate and the dispatcher check it with the same valuation
                                plan = await plan_perp_entry(conn, engine, mandate, canonical_inst, side, payload,
                                                             quote_cache.quote(canonical_inst))
                                if not plan.ok:
                                    logger.info("AI perp entry %s not placed: %s", row["command_id"], plan.reject)
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], f"{plan.reject}: perp sizing ({plan.binding_ceiling or 'no plan'})",
                                    )
                                    continue
                                qty_val, planned_leverage = plan.quantity, plan.leverage
                                sizing_plan = plan.as_dict()
                                logger.info("AI perp entry %s sized: %s %s %s at %sx (%s), margin %s, loss at stop %s",
                                            row["command_id"], side, qty_val, canonical_inst, plan.leverage,
                                            plan.binding_ceiling, plan.margin_usd, plan.loss_at_stop_usd)
                            if not qty_val:
                                alloc_pct = Decimal(str(payload.get("allocation_pct", 5.0)))
                                quote = quote_cache.quote(canonical_inst)
                                if not quote:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "NO_LIVE_QUOTE_FOR_SIZING",
                                    )
                                    continue
                                mark_px = Decimal(str(quote.ask_price if side == "BUY" else quote.bid_price))
                                # a mandate order's allocation is a share of the mandate's budget ("6% of mandate")
                                base = mandate.capital_budget_usd if mandate is not None else engine.dispatcher.cash_balance
                                notional = base * (alloc_pct / Decimal("100.0"))
                                qty_val = max(Decimal("0.0001"), notional / mark_px)
                                if mandate is not None and not is_perp_order and side == "BUY" and not payload.get("reduce_only"):
                                    # An entry adds to what the mandate already holds: size it down to the
                                    # room left under its position limit and budget (an approval is an upper
                                    # bound), so it passes the mandate check instead of being refused as a breach
                                    # Valued exactly as the mandate check will value it (mandate_entry_room): the
                                    # limit price for a limit order, the live ask otherwise
                                    order_type_for_size = str(payload.get("order_type", "MARKET")).upper()
                                    limit_px = Decimal(str(payload["price"])) if payload.get("price") else None
                                    room_qty, room_px, exposure = await mandate_entry_room(
                                        conn, engine, mandate, canonical_inst, side, limit_px, quote, order_type_for_size)
                                    room_qty = room_qty * SIZING_MARGIN
                                    if room_qty * room_px < mandate.capital_budget_usd * MIN_MANDATE_ENTRY_PCT / Decimal("100"):
                                        reason = (f"{MandateReject.POSITION_LIMIT_REACHED.value}: the mandate holds "
                                                  f"${exposure.instrument_usd:,.0f} of {canonical_inst} (limit "
                                                  f"{mandate.max_position_pct}% of ${mandate.capital_budget_usd:,.0f})")
                                        logger.info("AI entry %s not placed: %s", row["command_id"], reason)
                                        await conn.execute(
                                            "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                            row["command_id"], reason,
                                        )
                                        continue
                                    if room_qty < qty_val:
                                        logger.info("AI entry %s sized down from %s to %s: mandate %s position limit",
                                                    row["command_id"], qty_val, room_qty, mandate.name)
                                        qty_val = room_qty
                            qty_dec = Decimal(str(qty_val))
                            
                            price_dec = Decimal(str(payload["price"])) if payload.get("price") else None
                            order_type = payload.get("order_type", "MARKET").upper()
                            # Derived from the command id when not supplied, so a retried claim (after a
                            # rolled-back transaction) recognises its own order instead of placing a second one
                            client_oid = payload.get("client_order_id") or f"{'MANUAL' if cmd_type == 'MANUAL_ORDER' else 'AI'}-{str(row['command_id']).replace('-', '')[:12].upper()}"
                            instance_id = target or ("MANUAL_TRADE" if cmd_type == "MANUAL_ORDER" else "AI_DEBATE_WORKER")
                            if mandate is not None:
                                instance_id = mandate.instance_id
                            
                            try:
                                intent = OrderIntent(
                                    client_order_id=client_oid,
                                    instance_id=instance_id,
                                    instrument_id=canonical_inst,
                                    side=side,
                                    order_type=order_type,
                                    quantity=qty_dec,
                                    price=price_dec,
                                    # Perp attributes; the dispatcher defaults leverage to 1 and CROSS,
                                    # and rejects them on spot instruments
                                    leverage=planned_leverage or payload.get("leverage"),
                                    # mandate perps are ISOLATED (ADR 0009 D5): the default is explicit, a CROSS
                                    # request is refused by the mandate check
                                    margin_mode=payload.get("margin_mode") or (
                                        "ISOLATED" if mandate is not None and is_perp_order else None),
                                    reduce_only=payload.get("reduce_only") or False,
                                    stop_price=Decimal(str(payload["stop_price"])) if payload.get("stop_price") else None,
                                    # a perp entry carries its stop; an AI one must (leverage plan 2.3)
                                    stop_loss=Decimal(str(payload["stop_loss"])) if perp_entry and payload.get("stop_loss") else None,
                                    require_stop=bool(ai_order and perp_entry),
                                    risk_budget_usd=mandate_risk_budget(mandate) if mandate is not None and perp_entry else None,
                                )
                            except ValidationError as e:
                                await conn.execute(
                                    "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                    row["command_id"], f"INVALID_ORDER_PARAMETERS: {e.errors()[0]['loc']} {e.errors()[0]['msg']}",
                                )
                                continue
                            
                            placed_by = dispatched_by(engine, client_oid)
                            already = (
                                placed_by is not None
                                or client_oid in engine.dispatcher.open_orders
                                or await conn.fetchval("SELECT 1 FROM paper_orders WHERE client_order_id = $1", client_oid) == 1
                            )
                            if already:
                                derived = not payload.get("client_order_id")
                                if placed_by == str(row["command_id"]) or derived:
                                    # A retried claim of this same command: its order is already placed
                                    logger.warning("Command %s retried; order %s already placed", row["command_id"], client_oid)
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS', processed_at = now() WHERE command_id = $1",
                                        row["command_id"],
                                    )
                                else:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], "DUPLICATE_CLIENT_ORDER_ID",
                                    )
                                continue

                            if not ai_order and side == "SELL" and "-PERP." not in canonical_inst:
                                # The dispatcher holds one account-level quantity per instrument, so it would let
                                # a manual SELL sell an AI mandate's coins; the bridge then books the SELL on no
                                # position row and the mandate's next stop finds nothing left to sell (WLD
                                # 2026-10-17: two manual sells of 972.86 left its structural stop 0.0000017)
                                others = await others_spot_holding(conn, acc_id, canonical_inst, instance_id)
                                sellable = max(engine.dispatcher.get_position_qty(canonical_inst) - others, Decimal("0"))
                                if qty_dec > sellable:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"],
                                        f"SELL_EXCEEDS_OWN_HOLDING: {instance_id} can sell {format(sellable.normalize(), 'f')} "
                                        f"{canonical_inst}; {format(others.normalize(), 'f')} belongs to other owners "
                                        "(close an AI position with its own Close position)",
                                    )
                                    continue

                            if ai_order:
                                mandate_reject = await gate_ai_order(
                                    conn, engine, row, payload, intent, mandate, quote_cache.quote(canonical_inst),
                                    system_sized=not payload.get("quantity"),
                                )
                                if mandate_reject:
                                    await conn.execute(
                                        "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                        row["command_id"], mandate_reject,
                                    )
                                    continue
                                if mandate is not None:
                                    # The gate refreshed and locked the profile row. Use that committed snapshot for
                                    # dry-run routing too; a toggle committed after the earlier sizing read must not
                                    # let an entry reach the dispatcher.
                                    mandate = await load_mandate(conn, mandate.id)
                                    if mandate is None:
                                        await conn.execute(
                                            "UPDATE execution_commands SET status = 'REJECTED', command_outcome = 'REJECTED', error_message = $2, processed_at = now() WHERE command_id = $1",
                                            row["command_id"], MandateReject.NOT_FOUND.value,
                                        )
                                        continue
                                    effective_mode = {1: "ASK", 2: "VETO", 3: "AUTO"}[int(mandate.autonomy_level)]
                                    if not await profile_lifecycle.autotrade_switch_on(conn):
                                        effective_mode = "ASK"
                                    # Persist the mode that authorized this order. A delayed fill can arrive after
                                    # the mandate or global switch changes, but its FILL audit must describe this
                                    # order's effective mode.
                                    await conn.execute(
                                        "UPDATE execution_commands SET payload = payload || jsonb_build_object('profile_execution_mode', $2) WHERE command_id = $1",
                                        row["command_id"], effective_mode,
                                    )

                            if ai_order and mandate is not None and mandate.dry_run and (
                                    perp_entry or (not is_perp_order and side == "BUY" and not intent.reduce_only)):
                                # ADR 0011 D-P1-4: complete normal sizing and the mandate gates, then persist the
                                # exact intended order without creating any dispatcher reservation or order.
                                if sizing_plan is None:
                                    room, ref, _ = await mandate_entry_room(
                                        conn, engine, mandate, canonical_inst, side, price_dec,
                                        quote_cache.quote(canonical_inst), order_type)
                                    sizing_plan = {"instrument_id": canonical_inst, "side": side,
                                                   "quantity": str(qty_dec), "entry_price": str(ref),
                                                   "room_quantity": str(room), "leverage": intent.leverage or 1,
                                                   "stop_price": payload.get("stop_loss") or payload.get("stop_price"),
                                                   "notional_usd": str(qty_dec * ref) if ref else None}
                                await conn.execute(
                                    """UPDATE execution_commands SET status = 'COMPLETED', command_outcome = 'SUCCESS',
                                       payload = payload || jsonb_build_object('dry_run_plan', $2::jsonb), processed_at = now()
                                       WHERE command_id = $1""", row["command_id"], json.dumps(sizing_plan, default=str))
                                await record_decision(conn, mandate.id, "ORDER", "DRY_RUN", instrument_id=canonical_inst,
                                                      command_id=row["command_id"], plan=sizing_plan)
                                continue

                            # Dispatch intent to dispatcher (evaluates risk + atomically reserves)
                            approved = False
                            reason = "NO_DISPATCHER"
                            effects_applied = True  # the dispatcher reserves cash and books the order in memory
                            if hasattr(engine.dispatcher, "dispatch") and callable(engine.dispatcher.dispatch):
                                res = engine.dispatcher.dispatch(intent, lambda o: None, nautilus_order=None)
                                if isinstance(res, tuple):
                                    approved, reason = res[0], res[1]
                                else:
                                    approved, reason = True, "APPROVED"
                            else:
                                approved, reason, _ = engine.dispatcher.evaluate_intent(intent)

                            if not approved:
                                logger.warning("Order rejected by dispatcher for %s: %s", client_oid, reason)
                                await conn.execute(
                                    """
                                    UPDATE execution_commands
                                    SET status = 'REJECTED',
                                        command_outcome = 'REJECTED',
                                        error_message = $2,
                                        processed_at = now()
                                    WHERE command_id = $1;
                                    """,
                                    row["command_id"], reason
                                )
                            else:
                                remember_dispatched(engine, client_oid, row["command_id"])
                                try:
                                    quote = quote_cache.quote(canonical_inst)
                                    order_info = engine.dispatcher.open_orders.get(client_oid)
                                    if order_info is not None and order_info.get("is_perp") and intent.stop_loss is not None:
                                        order_info["attach_stop"] = intent.stop_loss
                                    fill = None
                                    if quote and order_info is not None:
                                        fill = match_order(
                                            client_oid, order_info,
                                            Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price)),
                                            *((quote.bid_size, quote.ask_size) if getattr(quote, "sizes_known", False) else (None, None)),
                                        )
                                    if fill:
                                        await apply_paper_fill(conn, engine, fill, instance_id, order_type, price_dec)
                                        logger.info("Order %s filled at %s (decision_trace_id=%s)", client_oid, fill.price, trace_id)
                                    else:
                                        # Works until a quote reaches it: a non-marketable limit rests at its
                                        # limit; an order without a quote yet takes the first quote.
                                        if quote and order_info is not None:
                                            order_info["rested"] = True
                                        await conn.execute(
                                            """
                                            INSERT INTO paper_orders (
                                                client_order_id, instance_id, account_id, instrument_id, side,
                                                order_type, quantity, price, filled_qty, status,
                                                last_event_version, created_at, updated_at
                                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 'ACCEPTED', 0, now(), now())
                                            ON CONFLICT (client_order_id) DO NOTHING;
                                            """,
                                            client_oid, instance_id, acc_id, canonical_inst,
                                            side, order_type, qty_dec, intent.stop_price or price_dec,
                                        )
                                        logger.info("Order %s accepted and working: %s %s %s @ %s (decision_trace_id=%s)",
                                                    client_oid, order_type, side, qty_dec, price_dec, trace_id)

                                    await conn.execute(
                                        """
                                        UPDATE execution_commands
                                        SET status = 'COMPLETED',
                                            command_outcome = 'SUCCESS',
                                            processed_at = now()
                                        WHERE command_id = $1;
                                        """,
                                        row["command_id"]
                                    )
                                except BaseException:
                                    # The claim transaction rolls back and the command is retried: a still-working
                                    # order from this attempt must not survive in memory (it would fill later)
                                    if hasattr(engine.dispatcher, "release_reservation") and client_oid in engine.dispatcher.open_orders:
                                        engine.dispatcher.release_reservation(client_oid)
                                        # Nothing was placed after all: let the retry place it again
                                        forget_dispatched(engine, client_oid)
                                        compensated = True  # the reservation is undone: a transient error may retry
                                    raise

                        if cmd_type in ("STOP", "PAUSE", "CLOSE_POSITION", "TRIP_KILL_SWITCH"):
                            await reconcile_working_orders(conn, engine)

        except _SettleOnly:
            stored = unsettled[str(row["command_id"])]
            logger.warning("Command %s: settling the error of its earlier attempt; it is not run again",
                           row["command_id"])
            pause = await settle_after_error(pool, row, stored.exc, effects_applied=stored.effects_applied,
                                             compensated=stored.compensated, unsettled=unsettled)
            await asyncio.sleep(pause)
        except Exception as e:
            logger.error("Error in command poller: %s", e)
            pause = 0.0
            if row is not None:  # its claim rolled back, so it is PENDING: settle it, never leave it at the head
                pause = await settle_after_error(pool, row, e, effects_applied=effects_applied,
                                                 compensated=compensated, unsettled=unsettled)
            await asyncio.sleep(pause)
        await asyncio.sleep(0.1)


async def prune_profile_decisions(pool) -> int:
    """ADR 0011/P1: retain ninety days of decisions, committing each batch of at most 10,000."""
    total = 0
    while True:
        async with pool.acquire() as conn:
            result = await conn.execute(
                """WITH expired AS (
                       SELECT id FROM ai_profile_decisions WHERE ts < now() - interval '90 days'
                       ORDER BY ts, id LIMIT 10000 FOR UPDATE SKIP LOCKED
                   ) DELETE FROM ai_profile_decisions WHERE id IN (SELECT id FROM expired)""")
        removed = int(result.rsplit(" ", 1)[-1])
        total += removed
        if removed < 10000:
            return total


async def profile_decision_retention(pool) -> None:
    """Prune the profile decision log hourly, with failures isolated from order processing (ADR 0011)."""
    while True:
        try:
            removed = await prune_profile_decisions(pool)
            if removed:
                logger.info("Profile decision retention removed %s rows older than 90 days", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Profile decision retention failed: %s", exc)
        await asyncio.sleep(3600)


async def rehydrate_dispatcher_state(pool, engine):
    """Load cash, spot positions, and perp positions from Postgres into the dispatcher."""
    try:
        acc_row = await pool.fetchrow("SELECT cash_balance FROM paper_accounts WHERE account_id = $1", engine.account_id)
        if acc_row:
            engine.dispatcher.cash_balance = Decimal(str(acc_row["cash_balance"]))
            logger.info("Rehydrated cash balance: %s", engine.dispatcher.cash_balance)
    
        pos_rows = await pool.fetch(
            "SELECT instrument_id, quantity FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' "
            "AND instrument_id NOT LIKE '%-PERP.%'",
            engine.account_id,
        )
        # Several OPEN rows can share one instrument (e.g. a manual position and a mandate-owned one).
        # The dispatcher tracks a single account-level quantity per instrument, so sum the rows:
        # assigning each row in turn keeps only the last one and wrongly shrinks the sellable position.
        spot_totals: dict[str, Decimal] = {}
        for pr in pos_rows:
            inst = pr["instrument_id"]
            spot_totals[inst] = spot_totals.get(inst, Decimal("0")) + Decimal(str(pr["quantity"]))
        engine.dispatcher.positions.update(spot_totals)
        logger.info("Rehydrated %d open positions from database", len(pos_rows))
    except Exception as e:
        logger.warning("Error rehydrating state: %s", e)

    # Perp positions (needs migration 020 columns): signed quantity, locked margin, owning instance
    try:
        perp_rows = await pool.fetch(
            "SELECT instrument_id, instance_id, side, quantity, entry_price, leverage, margin_mode "
            "FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND instrument_id LIKE '%-PERP.%'",
            engine.account_id,
        )
        for pr in perp_rows:
            qty = Decimal(str(pr["quantity"]))
            engine.dispatcher.set_perp_position(
                pr["instrument_id"], -qty if pr["side"] == "SHORT" else qty, Decimal(str(pr["entry_price"])),
                leverage=pr["leverage"], margin_mode=pr["margin_mode"] or "CROSS", instance_id=pr["instance_id"],
            )
        logger.info("Rehydrated %d open perp positions from database", len(perp_rows))
    except Exception as e:
        logger.warning("Error rehydrating perp positions: %s", e)

    # Fail closed across restarts: a latched PAPER kill switch stays latched
    try:
        ks = await pool.fetchrow(
            "SELECT is_latched, latched_by, reason FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
            engine.account_id,
        )
        if ks and ks["is_latched"]:
            engine.kill_switch.trip(latched_by=ks["latched_by"] or "database", reason=ks["reason"] or "Latched before restart")
            logger.critical("Paper kill switch restored LATCHED from database (%s)", ks["reason"])
    except Exception as e:
        logger.warning("Error rehydrating kill switch state: %s", e)


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    redis = await aioredis.from_url(REDIS_URL)

    engine_run_id = f"RUN-{uuid.uuid4().hex[:8]}"
    
    # Initialize the paper trading engine for strategy execution
    engine = PaperTradingEngine(
        account_id=ACCOUNT_ID,
        trader_id="ORCHESTRATOR-001",
        engine_run_id=engine_run_id,
        pg_pool=pool,
        redis_client=redis,
        starting_cash=Decimal("100000.00"),
    )
    
    engine._live_gate_open = True
    engine._running = True

    quote_cache = SimpleQuoteCache()
    engine.dispatcher.set_node_cache(quote_cache)

    # Startup sweep: reset any orphaned RUNNING commands for PAPER environment
    try:
        await pool.execute(
            """
            UPDATE execution_commands
            SET status = 'PENDING',
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE environment = 'PAPER' AND status = 'RUNNING';
            """
        )
    except Exception as e:
        logger.warning("Error running startup sweep: %s", e)

    await rehydrate_dispatcher_state(pool, engine)
    # Resting stops of open perps are placed again on the first mark pass; their rows stay ACCEPTED until then
    try:
        await load_stop_restores(pool, engine)
    except Exception as e:
        logger.error("Could not load the resting stops to restore: %s", e)
    # Working orders live in memory only; the ones from before this restart are gone.
    async with pool.acquire() as conn:
        await reconcile_working_orders(conn, engine)

    # Track active strategies
    active_instances = {}

    # EventBridge worker for async DB persistence
    worker = EventBridgeWorker(engine.bridge, pool, engine_run_id, batch_size=10)
    await worker.start()

    logger.info("Live Strategy Orchestrator initialized.")

    async def sync_strategies():
        """Poll the database for ACTIVE strategy deployments."""
        while True:
            try:
                rows = await pool.fetch("SELECT * FROM paper_strategy_instances WHERE status = 'RUNNING'")
                
                # Load new ones
                for row in rows:
                    iid = row["instance_id"]
                    if iid not in active_instances:
                        logger.info("Deploying new active strategy: %s", iid)
                        reg_entry = STRATEGY_REGISTRY.get(row["strategy_name"])
                        if reg_entry:
                            try:
                                bt_str = to_nautilus_bar_type_str(row["instrument_id"], row["timeframe"])
                                raw_params = json.loads(row["resolved_parameters"]) if isinstance(row["resolved_parameters"], str) else (row["resolved_parameters"] or {})
                                alias_map = {
                                    "oversold": "oversold_threshold",
                                    "exit_level": "exit_threshold",
                                }
                                normalized_params = {}
                                for k, v in raw_params.items():
                                    normalized_params[alias_map.get(k, k)] = v

                                cfg_cls = reg_entry["config_cls"]
                                valid_params = {"instrument_id": row["instrument_id"], "bar_type": bt_str}
                                import inspect
                                sig = inspect.signature(cfg_cls.__init__)
                                for param_name in sig.parameters:
                                    if param_name in normalized_params:
                                        valid_params[param_name] = normalized_params[param_name]

                                cfg = cfg_cls(**valid_params)
                                strategy = reg_entry["strategy_cls"](cfg)
                                _patch_strategy_for_paper_engine(strategy, engine, iid)
                                engine.register_strategy(strategy)
                                active_instances[iid] = strategy
                                logger.info("Successfully deployed %s", iid)
                            except Exception as e:
                                logger.error("Failed to deploy strategy instance %s: %s", iid, e)
                                active_instances[iid] = None
                                await mark_deploy_failed(pool, redis, iid, row["instrument_id"], str(e))
                        else:
                            logger.warning("Strategy '%s' not registered in STRATEGY_REGISTRY for instance %s", row["strategy_name"], iid)
                            active_instances[iid] = None
                            
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Error syncing strategies: %s", e)
                await asyncio.sleep(5)

    async def publish_positions():
        """Poll PostgreSQL paper_positions and publish positions snapshot to Redis."""
        while True:
            try:
                if not engine._running:
                    await asyncio.sleep(1)
                    continue
                    
                pos_list = []
                account_data = None
                async with pool.acquire() as conn:
                    acc_row = await conn.fetchrow(
                        """
                        SELECT total_equity, cash_balance, realized_pnl, daily_realized_pnl, currency
                        FROM paper_accounts
                        WHERE account_id = $1;
                        """,
                        engine.account_id
                    )
                    if acc_row:
                        account_data = {
                            "total_equity": float(acc_row["total_equity"]),
                            "cash_balance": float(acc_row["cash_balance"]),
                            "realized_pnl": float(acc_row["realized_pnl"]),
                            "daily_realized_pnl": float(acc_row["daily_realized_pnl"]),
                            "currency": str(acc_row["currency"])
                        }

                    rows = await conn.fetch(
                        """
                        SELECT position_id, account_id, instance_id, instrument_id,
                               side, entry_price, quantity, current_price, unrealized_pnl,
                               realized_pnl, opened_at
                        FROM paper_positions
                        WHERE account_id = $1 AND status = 'OPEN';
                        """,
                        engine.account_id
                    )
                    for r in rows:
                        quote = quote_cache.quote(r["instrument_id"])
                        curr_px = float(quote.bid_price) if quote else float(r["current_price"])
                        entry_px = float(r["entry_price"])
                        qty = float(r["quantity"])
                        unrealized = (-1 if r["side"] == "SHORT" else 1) * (curr_px - entry_px) * qty

                        pos_dict = {
                            "position_id": str(r["position_id"]),
                            "account_id": str(r["account_id"]),
                            "instance_id": str(r["instance_id"]),
                            "instrument_id": str(r["instrument_id"]),
                            "side": str(r["side"]),
                            "quantity": qty,
                            "entry_price": entry_px,
                            "current_price": curr_px,
                            "unrealized_pnl": unrealized,
                            "realized_pnl": float(r["realized_pnl"]),
                            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
                            "status": "OPEN"
                        }
                        pos_list.append(pos_dict)
                    
                payload_dict = {"type": "positions_snapshot", "positions": pos_list}
                if account_data:
                    payload_dict["account"] = account_data
                payload_json = json.dumps(payload_dict)
                await redis.publish("CHANNEL_POSITIONS", payload_json)
                await redis.publish("channel:portfolio:positions", payload_json)
            except Exception as e:
                logger.error("Error publishing positions: %s", e)
            await asyncio.sleep(1.0)

    # Launch background tasks
    asyncio.create_task(sync_strategies())
    asyncio.create_task(command_poller(pool, engine, quote_cache))
    asyncio.create_task(profile_decision_retention(pool))
    asyncio.create_task(consume_market_data(redis, engine, quote_cache, pool))
    asyncio.create_task(consume_perp_marks(redis, engine, pool=pool))
    asyncio.create_task(reload_leverage_brackets(redis))
    asyncio.create_task(risk_monitor(engine, pool, redis))
    asyncio.create_task(publish_positions())
    mark_projector = await start_mark_to_market(pool, redis, engine, quote_cache)  # noqa: F841 (B3; kept referenced)
    asyncio.create_task(ops_monitor(engine, pool, redis))  # enforced limits (R1.8) + heartbeat alerts (R7.5)

    # Stream consumer for nautilus:stream:bus
    stream_name = "nautilus:stream:bus"
    group_name = "orchestrator_group"
    consumer_name = f"orch_{uuid.uuid4().hex[:6]}"
    
    try:
        await redis.xgroup_create(stream_name, group_name, mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.warning("Group create warning: %s", e)

    logger.info("Listening for live bars on %s...", stream_name)

    _instrument_precisions: dict[str, tuple[int, int]] = {}

    async def _get_instrument_precisions(inst_id: str) -> tuple[int | None, int | None]:
        if inst_id in _instrument_precisions:
            return _instrument_precisions[inst_id]
        if pool:
            try:
                row = await pool.fetchrow(
                    "SELECT price_precision, size_precision FROM instruments WHERE instrument_id = $1 AND is_active = true",
                    inst_id,
                )
                if row and row["price_precision"] is not None and row["size_precision"] is not None:
                    res = (int(row["price_precision"]), int(row["size_precision"]))
                    _instrument_precisions[inst_id] = res
                    return res
            except Exception as ex:
                logger.warning("Error fetching precisions for %s in orchestrator: %s", inst_id, ex)
        return (None, None)

    while True:
        try:
            messages = await redis.xreadgroup(
                group_name, consumer_name, {stream_name: ">"}, count=10, block=2000
            )
            for stream, msgs in messages:
                for msg_id, msg_data in msgs:
                    if b"type" in msg_data and msg_data[b"type"] == b"Bar":
                        payload_json = msg_data[b"payload"].decode("utf-8")
                        try:
                            bar_obj = Bar.from_json_bytes(payload_json.encode('utf-8'))
                            engine.on_live_bar(bar_obj)
                        except Exception:
                            payload_dict = json.loads(payload_json)
                            bt = BarType.from_str(payload_dict["bar_type"])
                            inst_str = str(bt.instrument_id)
                            p_prec, s_prec = await _get_instrument_precisions(inst_str)
                            if p_prec is None or s_prec is None:
                                logger.error(
                                    "Missing instrument precision metadata for %s in bar fallback parser. Failing closed.",
                                    inst_str,
                                )
                            else:
                                b = Bar(
                                    bar_type=bt,
                                    open=Price(Decimal(str(payload_dict["open"])), p_prec),
                                    high=Price(Decimal(str(payload_dict["high"])), p_prec),
                                    low=Price(Decimal(str(payload_dict["low"])), p_prec),
                                    close=Price(Decimal(str(payload_dict["close"])), p_prec),
                                    volume=Quantity(Decimal(str(payload_dict["volume"])), s_prec),
                                    ts_event=int(payload_dict["ts_event"]),
                                    ts_init=int(payload_dict["ts_init"]),
                                )
                                engine.on_live_bar(b)

                    await redis.xack(stream_name, group_name, msg_id)
                    
        except Exception as e:
            logger.error("Error in stream reader loop: %s", e)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
