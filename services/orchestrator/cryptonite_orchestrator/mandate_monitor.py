"""Mandate equity / high-water-mark tracker and the O2 hard stops (AI-first blueprint §4.4, A5.2).

Runs inside the orchestrator's risk monitor. Per mandate:

- equity = budget + spot cash flow of its fills (fees included) + its open spot positions at the live
  bid + its perp realized PnL (fees and the funding it paid or received included) and unrealized PnL; tracked against the high-water mark since the mandate was
  created or last resumed (``hwm_rebased_at``) and written to ``ai_mandate_equity``;
- drawdown >= ``max_drawdown_pct`` (at most 20% of its own budget): the mandate is HALTED and demoted
  to L1, and each of its positions is closed with a PAPER ``CLOSE_POSITION`` (risk reducing, through
  the normal command path) capped at the mandate's own quantity; a Critical alert goes out;
- drawdown >= half of it: demoted one level, once per drawdown episode;
- a halted mandate with positions left keeps getting close commands until it is flat.

Daily sessions (ADR 0010), for a mandate with a daily loss limit or profit target: today's row in
``ai_mandate_daily_sessions`` (start equity, P&L, latch times). The daily loss limit cancels pending ADD_RISK
approvals and flattens the mandate (reduce-only) with a Critical alert; the profit target cancels pending ADD_RISK
approvals, alerts (Info) and puts the mandate's instance in the Redis set ``mandate:session:target_met`` until the
next 00:00 UTC; the profit lock flattens with a Critical alert. Each transition is recorded once (the latch column is
set only while NULL) and acted on only by the tick that recorded it. A session latch never changes the mandate's
status or level and is never a breach; the gate refuses new entries while it holds.

Account-wide backstop (O2): account equity 20% below its high-water mark (since the last kill-switch
reset) trips the PAPER kill switch (the latch: no new risk anywhere until the operator resets it
while flat), halts every mandate at L1 and closes every AI-owned position.
"""

import datetime
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_execution.approvals import sweep_expired
from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.autonomy_metrics import propose_if_ready
from cryptonite_execution.mandate_store import apply_transition, load_mandate, load_session
from cryptonite_schemas.mandate import (
    ACCOUNT_BACKSTOP_DRAWDOWN_PCT,
    AutonomyLevel,
    Mandate,
    MandateStatus,
    is_ai_instance,
    mandate_instance_id,
)
from cryptonite_schemas.session import (
    SessionVerdict,
    next_session_start,
    session_start,
    session_verdict,
    utc_session_date,
)

logger = logging.getLogger("cryptonite.orchestrator.mandate_monitor")

SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
BACKSTOP_REASON = "ACCOUNT_BACKSTOP_DRAWDOWN"
BENCHMARK_INSTRUMENT = "BTCUSDT.BINANCE"
EQUITY_SNAPSHOT_EVERY_S = 60
CLOSE_RETRY_AFTER_S = 30
D0 = Decimal("0")
CENT = Decimal("0.01")
# The contract with the perp resting-stop ratchet (A4): instances whose mandate met today's profit target; the set
# expires at the next 00:00 UTC
SESSION_TARGET_MET_KEY = "mandate:session:target_met"
SESSION_TRACKED_STATUSES = (MandateStatus.ACTIVE, MandateStatus.PAUSED, MandateStatus.HALTED)


def evaluate_drawdown(equity: Decimal, prev_hwm: Decimal | None, max_drawdown_pct: Decimal) -> dict[str, Any]:
    """Pure: new high-water mark, drawdown % from it, and whether the hard / half stop is hit."""
    hwm = max(prev_hwm, equity) if prev_hwm is not None else equity
    dd = (hwm - equity) / hwm * Decimal("100") if hwm > 0 else D0
    return {"equity": equity, "hwm": hwm, "drawdown_pct": dd, "hard_stop": dd >= max_drawdown_pct,
            "half_stop": dd >= max_drawdown_pct / Decimal("2")}


def _bid(quotes, instrument_id: str) -> Decimal | None:
    q = quotes.quote(instrument_id) if quotes is not None else None
    return Decimal(str(q.bid_price)) if q is not None and q.bid_price else None


async def mandate_equity(conn, mandate: Mandate, quotes=None) -> Decimal:
    inst = mandate.instance_id
    spot_cash = await conn.fetchval(
        """SELECT COALESCE(SUM(CASE WHEN side = 'SELL' THEN price * quantity - commission
                                    ELSE -(price * quantity + commission) END), 0)
           FROM paper_trades WHERE instance_id = $1 AND instrument_id NOT LIKE '%-PERP.%'""",
        inst,
    )
    rows = await conn.fetch(
        """SELECT instrument_id, side, quantity, entry_price, current_price, realized_pnl, status
           FROM paper_positions WHERE instance_id = $1 AND (status = 'OPEN' OR instrument_id LIKE '%-PERP.%')""",
        inst,
    )
    value = D0
    for r in rows:
        perp = "-PERP." in r["instrument_id"]
        price = _bid(quotes, r["instrument_id"]) or Decimal(str(r["current_price"]))
        qty = Decimal(str(r["quantity"]))
        if perp:
            value += Decimal(str(r["realized_pnl"] or 0))
            if r["status"] == "OPEN":
                sign = Decimal("-1") if r["side"] == "SHORT" else Decimal("1")
                value += sign * qty * (price - Decimal(str(r["entry_price"])))
        else:
            value += qty * price
    return mandate.capital_budget_usd + Decimal(str(spot_cash or 0)) + value


async def _last_equity_row(conn, mandate_id: uuid.UUID | None, since) -> Any:
    if mandate_id is None:
        return await conn.fetchrow(
            """SELECT ts, hwm FROM ai_mandate_equity WHERE mandate_id IS NULL AND ($1::timestamptz IS NULL OR ts >= $1)
               ORDER BY ts DESC, id DESC LIMIT 1""", since)
    return await conn.fetchrow(
        "SELECT ts, hwm FROM ai_mandate_equity WHERE mandate_id = $1 AND ts >= $2 ORDER BY ts DESC, id DESC LIMIT 1",
        mandate_id, since)


async def _snapshot(conn, mandate_id, v: dict, last, now: datetime.datetime, benchmark: Decimal | None, force: bool) -> None:
    stale = last is None or (now - last["ts"]).total_seconds() >= EQUITY_SNAPSHOT_EVERY_S
    if force or stale or last is None or v["hwm"] != Decimal(str(last["hwm"])):
        await conn.execute(
            "INSERT INTO ai_mandate_equity (mandate_id, ts, equity, hwm, drawdown_pct, benchmark_price) VALUES ($1, $2, $3, $4, $5, $6)",
            mandate_id, now, v["equity"], v["hwm"], v["drawdown_pct"].quantize(Decimal("0.0001")), benchmark,
        )


async def alert(conn, redis, kind: str, message: str, detail: dict[str, Any], *, tier: str = "CRITICAL",
                dedupe_key: str | None = None) -> None:
    """An alert (Critical unless ``tier`` says otherwise): channel:system_alerts now, and the notifier
    outbox (Telegram + ntfy) when present."""
    if redis is not None:
        try:
            await redis.publish(SYSTEM_ALERTS_CHANNEL, json.dumps(
                {"type": "system_alert", "v": 1, "kind": kind, "severity": tier, "message": message, **detail},
                default=str))
        except Exception as e:
            logger.error("Could not publish %s alert: %s", kind, e)
    try:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO notifications_outbox (tier, kind, title, body, payload, dedupe_key)
                   VALUES ($6, $1, $2, $3, $4::jsonb, $5) ON CONFLICT (dedupe_key) DO NOTHING""",
                kind, message, json.dumps(detail, default=str), json.dumps(detail, default=str),
                dedupe_key or f"{kind}:{detail.get('mandate_id', 'account')}:{detail.get('episode', '')}", tier,
            )
    except Exception as e:  # no outbox table yet (before migration 026) or DB hiccup: the channel alert went out
        logger.debug("notifications_outbox unavailable: %s", e)


async def enqueue_flatten(conn, instance_id: str, instrument_id: str, quantity: Decimal, reason: str,
                          mandate_id: uuid.UUID | None, account_id: str) -> bool:
    """One risk-reducing CLOSE_POSITION per open position; a rejected close is retried after a pause."""
    prefix = f"mandate_stop:{instance_id}:{instrument_id}"
    last = await conn.fetchrow(
        "SELECT status, created_at FROM execution_commands WHERE idempotency_key LIKE $1 || ':%' ORDER BY created_at DESC LIMIT 1",
        prefix,
    )
    if last is not None:
        if last["status"] not in ("REJECTED", "FAILED", "COMPLETED"):
            return False  # still pending or running
        if time.time() - last["created_at"].timestamp() < CLOSE_RETRY_AFTER_S:
            return False  # a completed close needs a moment to reach paper_positions
    attempt = await conn.fetchval("SELECT count(*) FROM execution_commands WHERE idempotency_key LIKE $1 || ':%'", prefix)
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id)
           VALUES ($1, $2, 'CLOSE_POSITION', $3, 'risk:mandate_monitor', $4::jsonb, 'PENDING', 'PAPER', $5, $6)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), f"{prefix}:{attempt}", instance_id,
        json.dumps({"instrument_id": instrument_id, "quantity": str(quantity), "reason": reason,
                    "mandate_id": str(mandate_id) if mandate_id else None, "reduce_only": True}),
        account_id, mandate_id,
    )
    return True


async def flatten_instance(conn, instance_id: str, reason: str, mandate_id: uuid.UUID | None, account_id: str) -> int:
    rows = await conn.fetch(
        "SELECT instrument_id, quantity FROM paper_positions WHERE instance_id = $1 AND status = 'OPEN' AND quantity > 0",
        instance_id,
    )
    n = 0
    for r in rows:
        n += await enqueue_flatten(conn, instance_id, r["instrument_id"], Decimal(str(r["quantity"])), reason, mandate_id, account_id)
    return n


async def _halt(conn, redis, mandate: Mandate, trigger: AutonomyTrigger, v: dict, account_id: str) -> None:
    t = transition(AutonomyState(mandate.autonomy_level, mandate.status, mandate.allow_perps), trigger,
                   reason=f"{trigger.value}: drawdown {v['drawdown_pct']:.2f}% from HWM {v['hwm']:.2f}")
    detail = {"mandate_id": str(mandate.id), "mandate": mandate.name, "equity": str(v["equity"]), "hwm": str(v["hwm"]),
              "drawdown_pct": f"{v['drawdown_pct']:.2f}", "limit_pct": str(mandate.max_drawdown_pct),
              "episode": str(v["hwm"])}
    async with conn.transaction():
        await apply_transition(conn, mandate.id, t, actor="risk:mandate_monitor", detail=detail)
        await _cancel_pending_add_risk(conn, mandate.id)
        closes = await flatten_instance(conn, mandate.instance_id, trigger.value, mandate.id, account_id)
    detail["closes"] = closes
    await alert(conn, redis, "MANDATE_HARD_STOP" if trigger is AutonomyTrigger.HARD_STOP else BACKSTOP_REASON,
                f"Hard stop hit — {mandate.name}: drawdown {v['drawdown_pct']:.1f}% from HWM. "
                f"Closing {closes} position(s). Mandate HALTED → L1.", detail)
    logger.critical("MANDATE %s %s: drawdown %.2f%% (limit %s%%), %d close(s) queued",
                    mandate.name, trigger.value, float(v["drawdown_pct"]), mandate.max_drawdown_pct, closes)


async def _cancel_pending_add_risk(conn, mandate_id: uuid.UUID) -> None:
    """A halted mandate takes no new risk: its pending ADD_RISK approvals are cancelled (A5.3)."""
    try:
        async with conn.transaction():
            await conn.execute(
                """UPDATE ai_approvals SET status = 'CANCELLED', decided_via = 'system', decided_at = now()
                   WHERE mandate_id = $1 AND status = 'PENDING' AND action_class = 'ADD_RISK'""", mandate_id)
    except Exception as e:  # before migration 026
        logger.debug("ai_approvals unavailable: %s", e)


async def track_mandate(conn, redis, mandate: Mandate, quotes, now: datetime.datetime, account_id: str,
                        benchmark: Decimal | None) -> dict[str, Any]:
    equity = await mandate_equity(conn, mandate, quotes)
    since = await conn.fetchval("SELECT hwm_rebased_at FROM ai_mandates WHERE id = $1", mandate.id)
    last = await _last_equity_row(conn, mandate.id, since)
    v = evaluate_drawdown(equity, Decimal(str(last["hwm"])) if last else None, mandate.max_drawdown_pct)

    if mandate.status in (MandateStatus.ACTIVE, MandateStatus.PAUSED) and v["hard_stop"]:
        await _snapshot(conn, mandate.id, v, last, now, benchmark, force=True)
        await _halt(conn, redis, mandate, AutonomyTrigger.HARD_STOP, v, account_id)
        return v
    await _snapshot(conn, mandate.id, v, last, now, benchmark, force=False)

    if mandate.status is MandateStatus.HALTED:
        # latched: keep closing until flat, never un-halt here
        await flatten_instance(conn, mandate.instance_id, "HARD_STOP", mandate.id, account_id)
    elif v["half_stop"] and mandate.autonomy_level > AutonomyLevel.L1:
        demoted_at = await conn.fetchval("SELECT last_dd_demotion_hwm FROM ai_mandates WHERE id = $1", mandate.id)
        if demoted_at is None or Decimal(str(demoted_at)) != v["hwm"]:  # once per drawdown episode
            t = transition(AutonomyState(mandate.autonomy_level, mandate.status, mandate.allow_perps),
                           AutonomyTrigger.HALF_DRAWDOWN,
                           reason=f"drawdown {v['drawdown_pct']:.2f}% >= half the {mandate.max_drawdown_pct}% hard stop")
            async with conn.transaction():
                await apply_transition(conn, mandate.id, t, actor="risk:mandate_monitor",
                                       detail={"equity": str(v["equity"]), "hwm": str(v["hwm"])})
                await conn.execute("UPDATE ai_mandates SET last_dd_demotion_hwm = $2 WHERE id = $1", mandate.id, v["hwm"])
            logger.warning("Mandate %s demoted to L%d: %s", mandate.name, int(t.after.level), t.reason)
    return v


def has_session_limits(mandate: Mandate) -> bool:
    return mandate.daily_loss_limit_pct is not None or mandate.daily_profit_target_pct is not None


async def _start_equity(conn, mandate: Mandate, day_start: datetime.datetime, equity: Decimal) -> Decimal:
    """The equity the day starts from: the last snapshot before 00:00 UTC, else the equity now (first tick).

    Only a snapshot taken since the mandate's equity was last re-based (``hwm_rebased_at``: a budget edit or a resume)
    counts: an older one is valued at the old budget, while ``start_budget`` is today's, so it would read the edit as
    P&L. When none remains, the equity now is the start (the edit moved both together)."""
    last = await conn.fetchval(
        """SELECT equity FROM ai_mandate_equity
           WHERE mandate_id = $1 AND ts < $2
             AND ts >= COALESCE((SELECT hwm_rebased_at FROM ai_mandates WHERE id = $1), '-infinity'::timestamptz)
           ORDER BY ts DESC, id DESC LIMIT 1""",
        mandate.id, day_start)
    return Decimal(str(last)) if last is not None else equity


def session_pnl(equity: Decimal, budget: Decimal, start_equity: Decimal, start_budget: Decimal) -> Decimal:
    """Equity change since the start of the day; a budget edit during the day is not P&L (equity counts the budget)."""
    return ((equity - budget) - (start_equity - start_budget)).quantize(CENT)


async def _record_latch(conn, mandate_id: uuid.UUID, day: datetime.date, column: str, now: datetime.datetime) -> bool:
    """Set today's ``column`` once: True only for the call that set it."""
    extra = ", target_hit_at = COALESCE(target_hit_at, $3)" if column == "lock_fired_at" else ""
    return await conn.fetchval(
        f"""UPDATE ai_mandate_daily_sessions SET {column} = $3{extra}, updated_at = $3
            WHERE mandate_id = $1 AND session_date = $2 AND {column} IS NULL RETURNING TRUE""",
        mandate_id, day, now) is not None


async def track_session(conn, redis, mandate: Mandate, equity: Decimal, now: datetime.datetime,
                        account_id: str) -> SessionVerdict | None:
    """ADR 0010: update today's session of the mandate and act on a new latch. None when it has no session limit."""
    if mandate.status not in SESSION_TRACKED_STATUSES or not has_session_limits(mandate):
        return None
    day = utc_session_date(now)
    row = await load_session(conn, mandate.id, now)
    if row is None:
        start = (await _start_equity(conn, mandate, session_start(day), equity)).quantize(CENT)
        await conn.execute(
            """INSERT INTO ai_mandate_daily_sessions (mandate_id, session_date, start_equity, start_budget, updated_at)
               VALUES ($1, $2, $3, $4, $5) ON CONFLICT (mandate_id, session_date) DO NOTHING""",
            mandate.id, day, start, mandate.capital_budget_usd, now)
        row = await load_session(conn, mandate.id, now)
    pnl = session_pnl(equity, mandate.capital_budget_usd, Decimal(str(row["start_equity"])),
                      Decimal(str(row["start_budget"])))
    await conn.execute(
        """UPDATE ai_mandate_daily_sessions SET last_pnl = $3, peak_pnl = GREATEST(peak_pnl, $3), updated_at = $4
           WHERE mandate_id = $1 AND session_date = $2""", mandate.id, day, pnl, now)
    verdict = session_verdict(mandate.capital_budget_usd, mandate.daily_loss_limit_pct,
                              mandate.daily_profit_target_pct, mandate.profit_lock_pct, pnl,
                              row["loss_latched_at"], row["target_hit_at"], row["lock_fired_at"])
    detail = {"mandate_id": str(mandate.id), "mandate": mandate.name, "session_date": str(day), "episode": str(day),
              "start_equity": str(row["start_equity"]), "equity": str(equity.quantize(CENT)), "pnl": str(pnl),
              "daily_loss_limit_pct": str(mandate.daily_loss_limit_pct), "budget": str(mandate.capital_budget_usd),
              "daily_profit_target_pct": str(mandate.daily_profit_target_pct),
              "profit_lock_pct": str(mandate.profit_lock_pct)}
    if verdict in (SessionVerdict.LOSS_LATCHED, SessionVerdict.LOCK_FIRED):
        loss = verdict is SessionVerdict.LOSS_LATCHED
        async with conn.transaction():
            new = await _record_latch(conn, mandate.id, day, "loss_latched_at" if loss else "lock_fired_at", now)
            if new:
                await _cancel_pending_add_risk(conn, mandate.id)
            # Latched for the day: keep closing until flat (a pending or just-completed close is not repeated)
            closes = await flatten_instance(conn, mandate.instance_id, "DAILY_LOSS_LIMIT" if loss else "PROFIT_LOCK",
                                            mandate.id, account_id)
        if new:
            detail["closes"] = closes
            if loss:
                msg = (f"Daily loss limit hit — {mandate.name}: session P&L ${pnl:,.2f} (limit "
                       f"{mandate.daily_loss_limit_pct}% of the ${mandate.capital_budget_usd:,.0f} budget). Closing "
                       f"{closes} position(s); new entries resume 00:00 UTC.")
            else:
                msg = (f"Profit lock fired — {mandate.name}: session P&L fell back to ${pnl:,.2f} after the "
                       f"{mandate.daily_profit_target_pct}% target. Closing {closes} position(s); new entries resume "
                       f"00:00 UTC.")
            await alert(conn, redis, "MANDATE_DAILY_LOSS" if loss else "MANDATE_PROFIT_LOCK", msg, detail)
            logger.critical("MANDATE %s session %s: pnl %s, %d close(s) queued", mandate.name, verdict, pnl, closes)
    elif verdict is SessionVerdict.TARGET_MET:
        async with conn.transaction():
            new = await _record_latch(conn, mandate.id, day, "target_hit_at", now)
            if new:
                await _cancel_pending_add_risk(conn, mandate.id)
        await _mark_target_met(redis, mandate, now)  # every tick: a flushed Redis gets the member back
        if new:
            await alert(conn, redis, "MANDATE_DAILY_TARGET",
                        f"Daily profit target met — {mandate.name}: session P&L ${pnl:,.2f} (target "
                        f"{mandate.daily_profit_target_pct}% of the budget). No new entries until 00:00 UTC; "
                        f"positions kept, stops may tighten.", detail, tier="INFO")
            logger.info("Mandate %s met its daily profit target: pnl %s", mandate.name, pnl)
    return verdict


async def _mark_target_met(redis, mandate: Mandate, now: datetime.datetime) -> None:
    if redis is None:
        return
    try:
        await redis.sadd(SESSION_TARGET_MET_KEY, mandate.instance_id)
        await redis.expireat(SESSION_TARGET_MET_KEY, int(next_session_start(now).timestamp()))
    except Exception as e:
        logger.error("Could not mark %s in %s: %s", mandate.instance_id, SESSION_TARGET_MET_KEY, e)


async def account_backstop(conn, engine, redis, now: datetime.datetime, benchmark: Decimal | None) -> dict[str, Any]:
    d = engine.dispatcher
    account_id = getattr(engine, "account_id", "PAPER-ACCOUNT-001")
    equity = Decimal(str(d.portfolio_equity()))
    ks = await conn.fetchrow(
        "SELECT is_latched, reset_at FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1", account_id)
    last = await _last_equity_row(conn, None, ks["reset_at"] if ks else None)
    v = evaluate_drawdown(equity, Decimal(str(last["hwm"])) if last else None, ACCOUNT_BACKSTOP_DRAWDOWN_PCT)
    await _snapshot(conn, None, v, last, now, benchmark, force=v["hard_stop"])
    if not v["hard_stop"]:
        return v

    reason = f"{BACKSTOP_REASON}: account drawdown {v['drawdown_pct']:.2f}% >= {ACCOUNT_BACKSTOP_DRAWDOWN_PCT}%"
    newly = not engine.kill_switch.is_latched or not (ks and ks["is_latched"])
    if not engine.kill_switch.is_latched:
        engine.kill_switch.trip(latched_by="risk:account_backstop", reason=reason)
    if not (ks and ks["is_latched"]):
        await conn.fetchval("SELECT trip_execution_kill_switch($1, $2, $3, $4)", "PAPER", account_id, reason, "risk:account_backstop")
    for row in await conn.fetch("SELECT id FROM ai_mandates WHERE status <> 'ARCHIVED'"):
        try:
            m = await load_mandate(conn, row["id"])
        except Exception as e:  # a row that does not load must not stop the backstop for the others
            logger.error("Account backstop: mandate %s does not load, skipped: %s", row["id"], e)
            continue
        if m is not None and m.status is not MandateStatus.HALTED:
            t = transition(AutonomyState(m.autonomy_level, m.status, m.allow_perps), AutonomyTrigger.BACKSTOP, reason=reason)
            async with conn.transaction():
                await apply_transition(conn, m.id, t, actor="risk:account_backstop", detail={"equity": str(equity), "hwm": str(v["hwm"])})
                await _cancel_pending_add_risk(conn, m.id)
    closes = 0
    for row in await conn.fetch(
            "SELECT DISTINCT instance_id FROM paper_positions WHERE account_id = $1 AND status = 'OPEN' AND quantity > 0", account_id):
        if is_ai_instance(row["instance_id"]):
            closes += await flatten_instance(conn, row["instance_id"], BACKSTOP_REASON, None, account_id)
    if newly:
        await alert(conn, redis, BACKSTOP_REASON,
                    f"Account backstop: drawdown {v['drawdown_pct']:.1f}% from HWM. Kill switch latched, every mandate "
                    f"HALTED → L1, {closes} AI position close(s) queued.",
                    {"equity": str(equity), "hwm": str(v["hwm"]), "drawdown_pct": f"{v['drawdown_pct']:.2f}",
                     "episode": str(v["hwm"])})
        logger.critical("ACCOUNT BACKSTOP %.2f%%: kill switch latched, mandates halted", float(v["drawdown_pct"]))
    return v


async def mandate_monitor_once(engine, pool, redis, quotes=None, now: datetime.datetime | None = None) -> None:
    now = now or datetime.datetime.now(datetime.UTC)
    quotes = quotes if quotes is not None else getattr(engine.dispatcher, "node_cache", None)
    account_id = getattr(engine, "account_id", "PAPER-ACCOUNT-001")
    benchmark = _bid(quotes, BENCHMARK_INSTRUMENT) if quotes is not None and hasattr(quotes, "quote") else None
    async with pool.acquire() as conn:
        await account_backstop(conn, engine, redis, now, benchmark)
        for row in await conn.fetch("SELECT id FROM ai_mandates WHERE status <> 'ARCHIVED' ORDER BY created_at"):
            try:
                m = await load_mandate(conn, row["id"])
            except Exception as e:  # a row that does not load must not abort the others' stops
                logger.error("Mandate %s does not load, skipped this pass: %s", row["id"], e)
                continue
            if m is None:
                continue
            try:
                v = await track_mandate(conn, redis, m, quotes, now, account_id, benchmark)
            except Exception as e:  # one broken mandate must not stop the others' stops
                logger.error("Mandate tracker failed for %s: %s", m.name, e)
                continue
            try:
                await track_session(conn, redis, m, v["equity"], now, account_id)
            except Exception as e:  # nor a session fault the drawdown stops above
                logger.error("Mandate session tracker failed for %s: %s", m.name, e)
        if time.monotonic() - _last_proposal_check[0] >= PROPOSAL_CHECK_EVERY_S:
            _last_proposal_check[0] = time.monotonic()
            await propose_promotions(conn, now)


PROPOSAL_CHECK_EVERY_S = 600
_last_proposal_check = [float("-inf")]


async def propose_promotions(conn, now: datetime.datetime) -> list[int]:
    """§4.5: the system proposes a promotion when a gate passes; only the operator (TOTP) promotes."""
    proposals = []
    for row in await conn.fetch("SELECT id FROM ai_mandates WHERE status = 'ACTIVE' AND autonomy_level < 3"):
        m = await load_mandate(conn, row["id"])
        try:
            async with conn.transaction():
                if (pid := await propose_if_ready(conn, m, now)) is not None:
                    proposals.append(pid)
        except Exception as e:
            logger.error("Gate evaluation failed for %s: %s", m.name, e)
    return proposals


async def sweep_approvals_once(pool, now: datetime.datetime | None = None) -> list:
    """D8 timeouts: expired ADD_RISK approvals mean no; expired REDUCE_RISK ones (and L2 veto
    windows inside active hours) run their command."""
    async with pool.acquire() as conn:
        done = await sweep_expired(conn, now)
    for approval_id, status in done:
        logger.info("Approval %s timed out: %s", approval_id, status)
    return done


__all__ = ["evaluate_drawdown", "mandate_equity", "mandate_instance_id", "mandate_monitor_once", "session_pnl",
           "sweep_approvals_once", "track_session"]
