"""Mandate equity / high-water-mark tracker and the O2 hard stops (AI-first blueprint §4.4, A5.2).

Runs inside the orchestrator's risk monitor. Per mandate:

- equity = budget + spot cash flow of its fills (fees included) + its open spot positions at the live
  bid + its perp realized PnL (fees and the funding it paid or received included) and unrealized PnL; tracked against the high-water mark since the mandate was
  created or last resumed (``hwm_rebased_at``) and written to ``ai_mandate_equity``;
- drawdown >= ``max_drawdown_pct`` (at most 20% of its own budget): the mandate is HALTED (its execution mode and
  markets stay, ADR 0011 D3), and each of its positions is closed with a PAPER ``CLOSE_POSITION`` (risk reducing, through
  the normal command path) capped at the mandate's own quantity; a Critical alert goes out;
- drawdown >= half of it: an ACTION-tier alert once per drawdown episode; the level never changes automatically (ADR 0011 D3);
- a STOPPING mandate that is flat becomes STOPPED and releases its coin locks (``profile_lifecycle.finish_stop``);
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
while flat), halts every mandate and closes every AI-owned position.
"""

import datetime
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_execution.approvals import sweep_expired
from cryptonite_execution.autonomy import AutonomyState, AutonomyTrigger, transition
from cryptonite_execution.flatten import (  # noqa: F401  (re-exported)
    CLOSE_RETRY_AFTER_S,
    enqueue_flatten,
    flatten_instance,
)
from cryptonite_execution.mandate_store import (
    apply_transition,
    load_mandate,
    load_session,
)
from cryptonite_execution.profile_lifecycle import finish_stop, pause
from cryptonite_execution.profile_log import record_decision
from cryptonite_schemas.mandate import (
    ACCOUNT_BACKSTOP_DRAWDOWN_PCT,
    Mandate,
    MandateStatus,
    is_ai_instance,
    mandate_instance_id,
)
from cryptonite_schemas.profile import underlying_spot
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
D0 = Decimal("0")
CENT = Decimal("0.01")
# The contract with the perp resting-stop ratchet (A4): instances whose mandate met today's profit target; the set
# expires at the next 00:00 UTC
SESSION_TARGET_MET_KEY = "mandate:session:target_met"
SESSION_TRACKED_STATUSES = (MandateStatus.ACTIVE, MandateStatus.PAUSED, MandateStatus.STOPPING, MandateStatus.HALTED)


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
                dedupe_key: str | None = None, body: str | None = None) -> None:
    """An alert (Critical unless ``tier`` says otherwise): channel:system_alerts now, and the notifier
    outbox (Telegram + ntfy) when present. ``body`` can supply readable text while payload keeps detail."""
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
                kind, message, body if body is not None else json.dumps(detail, default=str), json.dumps(detail, default=str),
                dedupe_key or f"{kind}:{detail.get('mandate_id', 'account')}:{detail.get('episode', '')}", tier,
            )
    except Exception as e:  # no outbox table yet (before migration 026) or DB hiccup: the channel alert went out
        logger.debug("notifications_outbox unavailable: %s", e)


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
                f"Closing {closes} position(s). Mandate HALTED (execution mode unchanged).", detail)
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

    if mandate.status in (MandateStatus.ACTIVE, MandateStatus.PAUSED, MandateStatus.STOPPING) and v["hard_stop"]:
        await _snapshot(conn, mandate.id, v, last, now, benchmark, force=True)
        await _halt(conn, redis, mandate, AutonomyTrigger.HARD_STOP, v, account_id)
        return v
    await _snapshot(conn, mandate.id, v, last, now, benchmark, force=False)

    if mandate.status is MandateStatus.HALTED:
        # latched: keep closing until flat, never un-halt here
        await flatten_instance(conn, mandate.instance_id, "HARD_STOP", mandate.id, account_id)
    elif v["half_stop"] and mandate.status is MandateStatus.ACTIVE:
        # ADR 0011 D3: recorded and alerted once per drawdown episode; the operator's mode is never changed
        seen = await conn.fetchval("SELECT last_dd_demotion_hwm FROM ai_mandates WHERE id = $1", mandate.id)
        if seen is None or Decimal(str(seen)) != v["hwm"]:
            t = transition(AutonomyState(mandate.autonomy_level, mandate.status, mandate.allow_perps),
                           AutonomyTrigger.HALF_DRAWDOWN,
                           reason=f"drawdown {v['drawdown_pct']:.2f}% >= half the {mandate.max_drawdown_pct}% hard stop")
            detail = {"mandate_id": str(mandate.id), "mandate": mandate.name, "equity": str(v["equity"]),
                      "hwm": str(v["hwm"]), "drawdown_pct": f"{v['drawdown_pct']:.2f}",
                      "limit_pct": str(mandate.max_drawdown_pct), "episode": str(v["hwm"])}
            # recorded as the alert (outbox row) and the episode marker; ai_autonomy_events has no kind for a warning
            await conn.execute("UPDATE ai_mandates SET last_dd_demotion_hwm = $2 WHERE id = $1", mandate.id, v["hwm"])
            if mandate.notify_level != 'NONE':
                await alert(conn, redis, "MANDATE_HALF_DRAWDOWN",
                            f"Drawdown warning — {mandate.name}: {v['drawdown_pct']:.1f}% from HWM, at least half the "
                            f"{mandate.max_drawdown_pct}% hard stop. Mode unchanged.", detail, tier="ACTION")
            logger.warning("Mandate %s: %s (mode unchanged)", mandate.name, t.reason)
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
        if new and mandate.notify_level != "NONE":
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
        if m is not None and m.status not in (MandateStatus.HALTED, MandateStatus.STOPPED):
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
        try:
            await notify_profile_breakers(conn, redis)
            await notify_profile_decisions(conn, redis)
        except Exception as e:
            logger.error("Profile notifications failed: %s", e)
        for row in await conn.fetch("SELECT id FROM ai_mandates WHERE status <> 'ARCHIVED' ORDER BY created_at"):
            try:
                m = await load_mandate(conn, row["id"])
            except Exception as e:  # a row that does not load must not abort the others' stops
                logger.error("Mandate %s does not load, skipped this pass: %s", row["id"], e)
                continue
            if m is None:
                continue
            # Breakers have independent data sources and must survive equity/quote failures.
            try:
                ingress = getattr(engine, "ingress_health", None)
                health = ingress.get_health_snapshot() if ingress is not None else {}
                await track_profile_breakers(conn, redis, m, now, health)
            except Exception as e:
                logger.error("Profile breaker failed for %s: %s", m.name, e)
            try:
                v = await track_mandate(conn, redis, m, quotes, now, account_id, benchmark)
            except Exception as e:
                logger.error("Mandate tracker failed for %s: %s", m.name, e)
            else:
                try:
                    await track_session(conn, redis, m, v["equity"], now, account_id)
                except Exception as e:
                    logger.error("Mandate session tracker failed for %s: %s", m.name, e)
            try:
                await profile_daily_summary(conn, redis, m, now)
            except Exception as e:
                logger.error("Profile summary failed for %s: %s", m.name, e)
            try:
                if m.status is MandateStatus.STOPPING:  # flat -> STOPPED, locks released
                    await finish_stop(conn, m.id)
            except Exception as e:
                logger.error("Finishing the stop of %s failed: %s", m.name, e)


async def propose_promotions(conn, now: datetime.datetime) -> list[int]:
    """ADR 0011 D3: the system no longer proposes promotions; the gate is a health score the operator reads. Kept so
    callers and the autonomy view keep working; it proposes nothing."""
    return []


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


async def _profile_alert(conn, redis, kind: str, detail: dict, key: str, *, tier: str = 'INFO') -> bool:
    """ADR 0011: acknowledge delivery only after a durable, uniquely keyed outbox row."""
    if await conn.fetchval('SELECT EXISTS (SELECT 1 FROM notifications_outbox WHERE dedupe_key = $1)', key):
        return True
    # Render text here because outbox delivery uses title/body directly, without alert_text.
    if kind == 'PROFILE_FILL':
        body = (f"{detail.get('side')} {detail.get('market')} {detail.get('instrument_id')} · {detail.get('mode')}\n"
                f"Quantity {detail.get('qty')} @ {detail.get('price')} · {detail.get('leverage')}x · "
                f"stop {detail.get('stop')}")
    elif kind == 'PROFILE_EXIT':
        body = (f"{detail.get('side')} {detail.get('market')} {detail.get('instrument_id')} · {detail.get('reason')}\n"
                f"Quantity {detail.get('qty')} @ {detail.get('price')} · "
                f"P&L ${detail.get('realized_pnl')} · "
                f"{detail.get('r_multiple') if detail.get('r_multiple') is not None else 'unknown '}R")
    elif kind == 'PROFILE_BREAKER':
        body = f"Profile paused: {detail.get('reason')}. Exits continue."
    else:
        body = (f"{detail.get('day')} · P&L ${detail.get('pnl')} · {detail.get('trades')} trades\n"
                f"Wins {detail.get('wins')} / losses {detail.get('losses')} · "
                f"Breaker events {detail.get('breaker_events')}")
        if 'refusals' in detail:
            body += f"\nRefusals: {detail['refusals']}"
    # The shared alert helper deliberately tolerates write failure; verify before advancing a cursor.
    await alert(conn, None, kind, f"{detail.get('mandate', 'Profile')}: {kind.replace('_', ' ').title()}", detail,
                tier=tier, dedupe_key=key, body=body)
    saved = await conn.fetchval('SELECT EXISTS (SELECT 1 FROM notifications_outbox WHERE dedupe_key = $1)', key)
    if saved and redis is not None:
        try:
            await redis.publish(SYSTEM_ALERTS_CHANNEL, json.dumps(
                dict(type='system_alert', v=1, kind=kind, severity=tier, **detail), default=str))
        except Exception as e:
            logger.error("Could not publish %s; durable outbox delivery remains queued: %s", kind, e)
    return bool(saved)


def _detail(value) -> dict:
    """Decode asyncpg's JSON representation without converting monetary values to floats."""
    return json.loads(value) if isinstance(value, str) else dict(value or {})


async def notify_profile_decisions(conn, redis) -> None:
    """ADR 0011: consume fills/exits in ID order, persisting only acknowledged progress.

    Every decision has a distinct outbox key. A crash after insertion and before Redis SET safely
    replays it. Filtered events also advance the cursor; an unavailable outbox never does.
    """
    if redis is None:
        return
    cursor = int(await redis.get('profile_notify:cursor') or 0)
    # Drain existing writers before taking a committed prefix of serial IDs. Release immediately:
    # delivery must never hold up fills. Sequence allocation happens inside the INSERT's table lock.
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout = '1s'")
        await conn.execute('LOCK TABLE ai_profile_decisions IN SHARE MODE')
        rows = await conn.fetch(
            """SELECT d.id, d.mandate_id, d.instrument_id, d.stage, d.outcome, d.detail,
                      m.name, m.notify_level FROM ai_profile_decisions d
               JOIN ai_mandates m ON m.id = d.mandate_id
               WHERE d.id > $1 AND d.stage IN ('FILL','EXIT') ORDER BY d.id LIMIT 500""", cursor)
    for row in rows:
        detail = _detail(row['detail'])
        level = row['notify_level']
        send = (level == 'IMPORTANT' and row['stage'] == 'FILL' or
                level == 'ALL' and (row['stage'] == 'EXIT' or detail.get('mode') in ('AUTO', 'VETO')))
        if send:
            detail.update(mandate_id=str(row['mandate_id']), mandate=row['name'],
                          instrument_id=row['instrument_id'], reason=row['outcome'])
            if not await _profile_alert(conn, redis, f"PROFILE_{row['stage']}", detail,
                                        f"profile-decision:{row['id']}"):
                return
        await redis.set('profile_notify:cursor', row['id'])


async def _profile_stale_coin(conn, redis, mandate: Mandate, now: datetime.datetime, health: dict) -> str | None:
    """ADR 0011: missing health gets ten minutes from start or the last healthy observation.

    The start baseline comes from persisted STARTED/RESUMED events. Redis retains the last
    healthy arrival across process restarts; an outage of Redis falls back to the start baseline.
    """
    started = await conn.fetchval(
        """SELECT COALESCE((SELECT MAX(e.created_at) FROM ai_autonomy_events e
                           WHERE e.mandate_id = m.id AND e.kind IN ('STARTED', 'RESUMED')),
                          m.created_at) AS started_at FROM ai_mandates m WHERE m.id = $1""", mandate.id)
    baseline = started.timestamp() if started is not None else now.timestamp()
    for coin in mandate.instruments:
        data = health.get(coin) or health.get(underlying_spot(coin))
        last = int(data['arrival_ts_ns']) / 1e9 if data else baseline
        key = f'profile_breaker:last_healthy:{mandate.id}:{coin}'
        if redis is not None:
            try:
                if data and now.timestamp() - last <= 600:
                    await redis.set(key, str(last))
                elif not data:
                    cached = await redis.get(key)
                    if cached is not None:
                        last = max(last, float(cached))
            except Exception as e:
                logger.warning('Profile breaker health cache unavailable: %s', e)
        if now.timestamp() - last > 600:
            return coin
    return None


async def track_profile_breakers(conn, redis, mandate: Mandate, now: datetime.datetime, health: dict) -> None:
    """ADR 0011 D7/D13: durably pause once per new trigger; audit failure cannot undo safety.

    The lifecycle PAUSED event is the durable episode marker and retry source. Unlike the
    best-effort decision log, lifecycle audit and state share the lifecycle transaction.
    """
    if mandate.status is not MandateStatus.ACTIVE:
        return
    triggers: list[tuple[str, str, str]] = []
    n = mandate.breaker_max_loss_streak
    losses = await conn.fetch(
        """SELECT position_id, closed_at, realized_pnl FROM paper_positions
           WHERE instance_id = $1 AND status = 'CLOSED'
           ORDER BY closed_at DESC, position_id DESC LIMIT $2""", mandate.instance_id, n)
    if len(losses) == n and all(Decimal(str(r['realized_pnl'])) < 0 for r in losses):
        latest = losses[0]
        triggers.append(('LOSS_STREAK', f"{latest['position_id']}:{latest['closed_at']}",
                         f'BREAKER_LOSS_STREAK: {n} losing trades'))
    n = mandate.breaker_max_reject_streak
    commands = await conn.fetch(
        """SELECT command_id, command_outcome FROM execution_commands WHERE target_instance_id = $1
           AND environment = 'PAPER' AND status IN ('COMPLETED', 'FAILED', 'REJECTED')
           AND command_type IN ('SUBMIT_ORDER', 'MANUAL_ORDER')
           AND COALESCE(payload->>'reduce_only', 'false') <> 'true'
           AND (payload->>'instrument_id' LIKE '%-PERP%' OR upper(payload->>'side') = 'BUY')
           ORDER BY created_at DESC, command_id DESC LIMIT $2""", mandate.instance_id, n)
    if len(commands) == n and all(r['command_outcome'] == 'REJECTED' for r in commands):
        triggers.append(('REJECT_STREAK', str(commands[0]['command_id']),
                         f'BREAKER_REJECT_STREAK: {n} refused commands'))
    stale_coin = await _profile_stale_coin(conn, redis, mandate, now, health)
    if stale_coin is not None:
        # A still-stale feed remains unsafe after operator resume; never suppress this condition.
        triggers.append(('STALE_DATA', stale_coin, f'BREAKER_STALE_DATA: {stale_coin} ingest older than 10 minutes'))
    if not triggers:
        return
    async with conn.transaction():
        row = await conn.fetchrow('SELECT status, paused_reason FROM ai_mandates WHERE id = $1 FOR UPDATE', mandate.id)
        if row is None or row['status'] != 'ACTIVE':
            return
        for kind, event, reason in triggers:
            if kind != 'STALE_DATA' and await conn.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM ai_autonomy_events WHERE mandate_id = $1
                       AND actor = 'risk:breaker' AND kind = 'PAUSED'
                       AND detail->>'trigger_kind' = $2 AND detail->>'trigger_event' = $3)""",
                    mandate.id, kind, event):
                continue
            await pause(conn, mandate.id, actor='risk:breaker', reason=reason)
            episode = await conn.fetchrow(
                """SELECT id, detail FROM ai_autonomy_events WHERE mandate_id = $1
                   AND actor = 'risk:breaker' AND kind = 'PAUSED' ORDER BY id DESC LIMIT 1""", mandate.id)
            await conn.execute(
                'UPDATE ai_autonomy_events SET detail = detail || $2::jsonb WHERE id = $1',
                episode['id'], json.dumps(dict(trigger_kind=kind, trigger_event=event)))
            break
    # This independent best-effort path can fail repeatedly without reverting the committed PAUSE.
    await notify_profile_breakers(conn, redis)


async def notify_profile_breakers(conn, redis) -> None:
    """Recover missing decision audits and alerts from durable lifecycle PAUSED events."""
    events = await conn.fetch(
        """SELECT e.id, e.mandate_id, e.reason, e.detail, m.name FROM ai_autonomy_events e
           JOIN ai_mandates m ON m.id = e.mandate_id
           WHERE e.kind = 'PAUSED' AND e.actor = 'risk:breaker' AND (
               NOT EXISTS (SELECT 1 FROM ai_profile_decisions d WHERE d.mandate_id = e.mandate_id
                           AND d.stage = 'BREAKER' AND d.detail->>'breaker_event_id' = e.id::text)
               OR NOT EXISTS (SELECT 1 FROM notifications_outbox n
                              WHERE n.dedupe_key = 'profile-breaker-event:' || e.id::text))
           ORDER BY e.id LIMIT 100""")
    for event in events:
        exists = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'BREAKER'
               AND detail->>'breaker_event_id' = $2)""", event['mandate_id'], str(event['id']))
        if not exists:
            await record_decision(conn, event['mandate_id'], 'BREAKER', 'PAUSED', event['reason'],
                                  breaker_event_id=str(event['id']), **_detail(event['detail']))
        await _profile_alert(conn, redis, 'PROFILE_BREAKER',
                             dict(mandate_id=str(event['mandate_id']), mandate=event['name'], reason=event['reason']),
                             f"profile-breaker-event:{event['id']}", tier='CRITICAL')
    # Preserve delivery of decisions created before lifecycle-event reconciliation was introduced.
    rows = await conn.fetch(
        """SELECT d.id, d.mandate_id, d.reason, m.name FROM ai_profile_decisions d
           JOIN ai_mandates m ON m.id = d.mandate_id
           WHERE d.stage = 'BREAKER' AND NOT (d.detail ? 'breaker_event_id') AND NOT EXISTS (
               SELECT 1 FROM ai_autonomy_events e WHERE e.mandate_id = d.mandate_id
                   AND e.kind = 'PAUSED' AND e.actor = 'risk:breaker' AND e.reason = d.reason)
           AND NOT EXISTS (SELECT 1 FROM notifications_outbox n
                           WHERE n.dedupe_key = 'profile-breaker:' || d.id::text)
           ORDER BY d.id LIMIT 100""")
    for row in rows:
        await _profile_alert(conn, redis, 'PROFILE_BREAKER',
                             dict(mandate_id=str(row['mandate_id']), mandate=row['name'], reason=row['reason']),
                             f"profile-breaker:{row['id']}", tier='CRITICAL')


async def profile_daily_summary(conn, redis, mandate: Mandate, now: datetime.datetime) -> None:
    """ADR 0011: previous UTC day's summary and ALL refusal digest, once after 00:05."""
    now = now.astimezone(datetime.UTC)
    if mandate.notify_level == 'NONE' or (now.hour == 0 and now.minute < 5):
        return
    day = now.astimezone(datetime.UTC).date() - datetime.timedelta(days=1)
    key = f'profile-summary:{mandate.id}:{day}'
    if await conn.fetchval('SELECT EXISTS (SELECT 1 FROM notifications_outbox WHERE dedupe_key = $1)', key):
        return
    start = datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.UTC)
    rows = await conn.fetch(
        """SELECT stage, outcome, reason, detail FROM ai_profile_decisions
           WHERE mandate_id = $1 AND ts >= $2 AND ts < $3""",
        mandate.id, start, start + datetime.timedelta(days=1))
    pnl, wins, losses, trades, breakers = D0, 0, 0, 0, 0
    refusals: dict[str, int] = {}
    for row in rows:
        if row['stage'] == 'EXIT':
            value = Decimal(str(_detail(row['detail']).get('realized_pnl') or 0))
            pnl += value
            trades += 1
            wins += value > 0
            losses += value < 0
        elif row['stage'] == 'BREAKER':
            breakers += 1
        elif row['stage'] in ('GATE', 'ROUTE', 'ORDER') and row['outcome'] in ('REFUSED', 'REJECTED', 'NO_TRADE'):
            reason = row['reason'] or row['outcome']
            refusals[reason] = refusals.get(reason, 0) + 1
    detail = dict(mandate_id=str(mandate.id), mandate=mandate.name, day=str(day), pnl=str(pnl),
                  trades=trades, wins=wins, losses=losses, breaker_events=breakers)
    if mandate.notify_level == 'ALL':
        detail['refusals'] = refusals
    await _profile_alert(conn, redis, 'PROFILE_DAILY_SUMMARY', detail, key)
