"""Where a debate verdict goes (AI-first blueprint §4.5, D8, A5.7). Replaces DEBATE_EXECUTION_MODE.

Autonomous execution exists only inside a mandate, at the level it has earned:

- no ACTIVE mandate allows the instrument -> the verdict waits for the operator (PENDING_APPROVAL);
- L1 Copilot -> an ``ai_approvals`` row: ADD_RISK times out as *no*;
- L2 Supervised -> an ``ai_approvals`` veto window: ADD_RISK proceeds at timeout only inside the
  operator's active hours; outside them it times out as *no*;
- L3 Autonomous -> the order is queued at once (the mandate check and the dispatcher still apply);
- risk reducing (a spot SELL of what the mandate holds, an exit debate's REDUCE/CLOSE) runs at once
  at L2/L3; at L1 it waits a short veto window that *proceeds* at timeout (D8).

Entry filters (ADR 0010, refuse only): before anything is sent for approval or queued, an entry is dropped
(NO_TRADE, logged) when its conviction is below the mandate's ``min_conviction``, its reward:risk is below
``min_rr`` (only when the verdict carries a stop, a target and a reference price), or the mandate's daily session
is not ACTIVE (loss latch, profit target or profit lock). The orchestrator's mandate gate refuses the session cases
again at execution.

Perps (auto-trade Phase B, ADR 0010 amendment): a spot verdict no ACTIVE spot mandate takes goes to the single
ACTIVE perps mandate that allows the instrument's USD-M perpetual (``resolve_route``). There a BUY opens or adds to a
long and a SELL to a short; a verdict against the mandate's position closes it reduce-only. An entry needs a
structural stop and a target on the correct side (the scanner candidate's own levels: long levels never become a
short), a setup grade from code (``setup_grade.py``) other than NO_TRADE, and a leverage plan the API's card dry run
(``leverage_card.card_leverage_plan``) accepts; it is always an L1 approval (TOTP to approve) and carries no leverage:
the orchestrator sizes it with ``plan_leveraged_order``.

The allocation is capped at the room the mandate has left under its own position limit and budget
(what it already holds counts) before anything is queued or sent for approval, so a model never
"breaches" by over-sizing; with less than ``MIN_MANDATE_ENTRY_PCT`` of room there is no trade. The
orchestrator sizes the order to the exact room again at execution. This module writes the same rows ``cryptonite_execution.approvals``
writes; the orchestrator's sweeper and the API decide them. The spot path needs only ``cryptonite_schemas``; the
perp path imports the scanner's grade and the API's card dry run lazily (the worker image installs every workspace
package).
"""

import datetime
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.identifiers import to_perp_instrument_id
from cryptonite_schemas.mandate import (
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REDUCE_VETO_S,
    MIN_MANDATE_ENTRY_PCT,
    ActionClass,
    AutonomyLevel,
    Mandate,
    canonical_instrument,
    in_active_hours,
    is_perp_instrument,
    mandate_id_from_instance,
    timeout_policy_for,
)
from cryptonite_schemas.marks import LiveMark
from cryptonite_schemas.marks import entry_room_pct as room_at_marks
from cryptonite_schemas.session import (
    SESSION_COLUMNS,
    SessionVerdict,
    entry_filter_refusal,
    utc_session_date,
    verdict_from_row,
)

logger = logging.getLogger("cryptonite.debate_worker.mandate_routing")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
MANDATE_COLUMNS = ("id, name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct, allow_perps, "
                   "max_leverage, max_risk_per_trade_pct, strategies, autonomy_level, status, active_hours_utc, veto_window_s, "
                   "min_conviction, min_rr, daily_loss_limit_pct, daily_profit_target_pct, profit_lock_pct")


# Recorded in the payload and the summary of a debate entry the R:R filter could not measure (a debate carries no
# stop or target today), so the skipped filter is visible to the operator
MIN_RR_SKIPPED = "min_rr skipped: no stop/target"
# A debate perp entry the routing refuses (NO_TRADE, logged with the reason)
STOP_REQUIRED = "STOP_REQUIRED"
NO_SHORT_STRUCTURE = "NO_SHORT_STRUCTURE"


def side_of(recommendation: str) -> str | None:
    """The order side of a verdict (``normalize_verdict``'s mapping; "strong buy" with a space is
    STRONG_BUY); None for HOLD or an unknown verdict."""
    direction = verdict_direction(str(recommendation or "").strip().replace(" ", "_"))
    return direction if direction in ("BUY", "SELL") else None


async def resolve_mandate(conn, instrument_id: str, mandate_id: str | None = None) -> Mandate | None:
    """The named mandate when it is ACTIVE and allows the instrument, else the single ACTIVE one that does."""
    if mandate_id:
        row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", uuid.UUID(str(mandate_id)))
        m = Mandate.from_row(row) if row else None
        return m if m and m.status.value == "ACTIVE" and m.allows_instrument(instrument_id) else None
    rows = await conn.fetch(
        f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' AND $1 = ANY(instruments) ORDER BY created_at",
        canonical_instrument(instrument_id))
    return Mandate.from_row(rows[0]) if len(rows) == 1 else None


async def _perp_mandates(conn, perp_instrument: str) -> list[Mandate]:
    rows = await conn.fetch(
        f"""SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE status = 'ACTIVE' AND allow_perps AND $1 = ANY(instruments)
            ORDER BY created_at""", perp_instrument)
    return [Mandate.from_row(r) for r in rows]


async def resolve_route(conn, instrument_id: str, mandate_id: str | None = None) -> tuple[Mandate | None, str]:
    """(mandate, the instrument its order trades). The spot mandate as ``resolve_mandate`` finds it; else, for a spot
    instrument no ACTIVE mandate allows, the single ACTIVE perps mandate that allows its perpetual, on the perpetual.
    A named mandate wins: it trades the instrument when it allows it, else its perpetual when it has perps on and
    allows that. When a spot and a perps mandate both match, the spot one is taken (logged); the operator can pick
    another in the Inbox. (None, instrument) when nothing matches or more than one mandate of a kind does."""
    inst = canonical_instrument(instrument_id)
    perp = None if is_perp_instrument(inst) else to_perp_instrument_id(inst.split(".")[0])
    if mandate_id:
        mandate = await resolve_mandate(conn, inst, mandate_id)
        if mandate is not None or perp is None:
            return mandate, inst
        row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", uuid.UUID(str(mandate_id)))
        m = Mandate.from_row(row) if row else None
        if m and m.status.value == "ACTIVE" and m.allow_perps and m.allows_instrument(perp):
            return m, perp
        return None, inst
    spot_count = await conn.fetchval(
        "SELECT count(*) FROM ai_mandates WHERE status = 'ACTIVE' AND $1 = ANY(instruments)", inst)
    if spot_count or perp is None:
        mandate = await resolve_mandate(conn, inst)
        if mandate is not None and perp is not None and (perps := await _perp_mandates(conn, perp)):
            logger.info("%s: spot mandate %s preferred over perps mandate(s) %s", inst, mandate.name,
                        ", ".join(m.name for m in perps))
        return mandate, inst
    perps = await _perp_mandates(conn, perp)
    if len(perps) == 1:
        logger.info("%s: no ACTIVE spot mandate allows it; routed to perps mandate %s on %s", inst, perps[0].name, perp)
        return perps[0], perp
    return None, inst


async def perp_position(conn, mandate: Mandate, instrument_id: str) -> tuple[str | None, Decimal]:
    """The mandate's own open position in a perpetual: (LONG | SHORT, quantity), or (None, 0) when flat."""
    row = await conn.fetchrow(
        """SELECT side, quantity FROM paper_positions
           WHERE account_id = $1 AND instance_id = $2 AND instrument_id = $3 AND status = 'OPEN' AND quantity > 0""",
        PAPER_ACCOUNT_ID, mandate.instance_id, canonical_instrument(instrument_id))
    if row is None or row["side"] not in ("LONG", "SHORT"):
        return None, Decimal("0")
    return row["side"], Decimal(str(row["quantity"]))


async def mandate_holding(conn, mandate: Mandate, instrument_id: str) -> Decimal:
    qty = await conn.fetchval(
        """SELECT COALESCE(SUM(quantity), 0) FROM paper_positions
           WHERE account_id = $1 AND instance_id = $2 AND instrument_id = $3 AND status = 'OPEN'""",
        PAPER_ACCOUNT_ID, mandate.instance_id, canonical_instrument(instrument_id))
    return Decimal(str(qty or 0))


async def entry_room_pct(conn, mandate: Mandate, instrument_id: str, marks: dict[str, LiveMark] | None = None) -> Decimal:
    """Room for a new entry, as % of the mandate budget: under the instrument's position limit and under
    the budget, with the mandate's open positions at their live mark (``marks``, from the orchestrator's
    projector), else at their stored mark (B3)."""
    rows = await conn.fetch(
        """SELECT instrument_id, quantity, current_price FROM paper_positions
           WHERE account_id = $1 AND instance_id = $2 AND status = 'OPEN' AND quantity > 0""",
        PAPER_ACCOUNT_ID, mandate.instance_id)
    return room_at_marks(mandate.capital_budget_usd, mandate.max_position_pct, rows, instrument_id, marks)


async def position_owner(conn, instrument_id: str) -> tuple[Mandate | None, Decimal]:
    """The mandate that alone holds the instrument (exit debates act only on those)."""
    rows = await conn.fetch(
        """SELECT instance_id, SUM(quantity) AS qty FROM paper_positions
           WHERE account_id = $1 AND instrument_id = $2 AND status = 'OPEN' AND quantity > 0 GROUP BY instance_id""",
        PAPER_ACCOUNT_ID, canonical_instrument(instrument_id))
    if len(rows) != 1 or (mid := mandate_id_from_instance(rows[0]["instance_id"])) is None:
        return None, Decimal("0")
    row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mid)
    return (Mandate.from_row(row) if row else None), Decimal(str(rows[0]["qty"]))


async def session_verdict_of(conn, mandate: Mandate, now: datetime.datetime) -> SessionVerdict:
    """Today's (UTC) daily-session verdict of the mandate (ADR 0010), from the orchestrator's session row."""
    row = await conn.fetchrow(
        f"SELECT {SESSION_COLUMNS} FROM ai_mandate_daily_sessions WHERE mandate_id = $1 AND session_date = $2",
        mandate.id, utc_session_date(now))
    return verdict_from_row(mandate, row)


async def entry_filter_reason(conn, mandate: Mandate, conviction: float, *, now: datetime.datetime,
                              reference_price: float | None = None, stop_loss: float | None = None,
                              take_profit: float | None = None, side: str = "BUY") -> str | None:
    """Why the mandate refuses this entry (ADR 0010: its daily session, conviction or R:R), else None."""
    if mandate.min_rr is not None and not (stop_loss and take_profit):
        logger.info("R:R filter of mandate %s skipped: %s", mandate.name, MIN_RR_SKIPPED)
    elif mandate.min_rr is not None and not (reference_price and reference_price > 0):
        logger.info("R:R filter of mandate %s skipped: no reference price", mandate.name)
    refusal = entry_filter_refusal(
        mandate, await session_verdict_of(conn, mandate, now), conviction=conviction,
        entry=Decimal(str(reference_price)) if reference_price and reference_price > 0 else None,
        stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
        take_profit=Decimal(str(take_profit)) if take_profit else None, side=side)
    return f"{refusal[0]}: {refusal[1]}" if refusal else None


async def _approval(conn, mandate: Mandate, action: ActionClass, command_type: str, payload: dict[str, Any],
                    summary: str, source: str, run_id: uuid.UUID, now: datetime.datetime) -> uuid.UUID | None:
    policy = timeout_policy_for(action, mandate.autonomy_level, in_active_hours(mandate.active_hours_utc, now))
    if policy is None:
        return None
    ttl = (DEFAULT_REDUCE_VETO_S if action is ActionClass.REDUCE_RISK
           else mandate.veto_window_s if mandate.autonomy_level == AutonomyLevel.L2 else DEFAULT_APPROVAL_TTL_S)
    approval_id = await conn.fetchval(
        """INSERT INTO ai_approvals (mandate_id, action_class, command_type, payload, summary, source, source_ref,
               level_at_creation, timeout_policy, expires_at)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
           ON CONFLICT (source, source_ref) DO NOTHING RETURNING id""",
        mandate.id, action.value, command_type, json.dumps({**payload, "mandate_id": str(mandate.id)}, default=str),
        summary, source, str(run_id), int(mandate.autonomy_level), policy.value,
        now + datetime.timedelta(seconds=ttl))
    if approval_id is not None:
        await conn.execute(
            """INSERT INTO notifications_outbox (tier, kind, title, body, payload, approval_id, dedupe_key)
               VALUES ('ACTION', 'APPROVAL', $1, $2, $3::jsonb, $4, $5) ON CONFLICT (dedupe_key) DO NOTHING""",
            f"{mandate.name} (L{int(mandate.autonomy_level)})", summary,
            json.dumps({"mandate": mandate.name, "action_class": action.value, "timeout_policy": policy.value,
                        "run_id": str(run_id)}),
            approval_id, f"approval:{approval_id}")
    return approval_id


def _trace_uuid(payload: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload["decision_trace_id"])) if payload.get("decision_trace_id") else None
    except ValueError:
        return None


async def _enqueue(conn, mandate: Mandate, command_type: str, payload: dict[str, Any], key: str) -> None:
    # decision_trace_id is written at insert: the API role cannot UPDATE execution_commands (R7.4)
    await conn.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, target_instance_id, requester_id,
               payload, status, environment, account_id, mandate_id, decision_trace_id)
           VALUES ($1, $2, $3, $4, 'ai:debate_worker', $5::jsonb, 'PENDING', 'PAPER', $6, $7, $8)
           ON CONFLICT (idempotency_key) DO NOTHING""",
        uuid.uuid4(), key, command_type, mandate.instance_id,
        json.dumps({**payload, "mandate_id": str(mandate.id), "account_id": PAPER_ACCOUNT_ID}, default=str),
        PAPER_ACCOUNT_ID, mandate.id, _trace_uuid(payload))


class _OneConn:
    """``pool.acquire()`` over the routing transaction's own connection: the card dry run only reads."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> "_OneConn":
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


async def funding_8h(conn, redis, perp_instrument: str) -> float | None:
    """The perpetual's current funding rate (the derivatives feed's ``perp:mark:<SYM>``) per 8 h, scaled by its
    settlement interval from ``funding_rates`` (8 h when unknown). None without a fresh mark."""
    from cryptonite_scanner.setup_grade import funding_rate_8h

    if redis is None:
        return None
    sym = perp_instrument.split(".")[0].removesuffix("-PERP")
    try:
        raw = await redis.get(f"perp:mark:{sym}")
        rate = _num(json.loads(raw.decode() if isinstance(raw, bytes) else raw).get("funding_rate")) if raw else None
    except Exception as e:
        logger.warning("Could not read the funding rate of %s: %s", perp_instrument, e)
        return None
    if rate is None:
        return None
    rows = await conn.fetch(
        """SELECT funding_interval_hours, funding_time_ns FROM funding_rates WHERE instrument_id = $1
           ORDER BY funding_time_ns DESC LIMIT 2""", perp_instrument)
    hours = None
    if rows and rows[0]["funding_interval_hours"]:
        hours = float(rows[0]["funding_interval_hours"])
    elif len(rows) == 2:
        hours = (int(rows[0]["funding_time_ns"]) - int(rows[1]["funding_time_ns"])) / 3.6e12
    return funding_rate_8h(rate, hours)


def perp_setup_grade(side: str, entry: float, stop: float, target: float, context: dict[str, Any] | None,
                     funding_rate_8h: float | None) -> dict[str, Any]:
    """The setup grade (``setup_grade.grade_setup``, code, never the model) of a debate perp entry, from the frames the
    scanner saw (``brief_data``'s ``grade_context``) and the current funding. Open interest and liquidity are not
    read here, so the grade is at most B (liquidity unknown is a weak input), as a scan grade is."""
    from cryptonite_scanner.setup_grade import GRADE_CONTEXT_KEYS, GradeInputs, grade_setup, side_of

    ctx = context if isinstance(context, dict) else {}
    kw: dict[str, Any] = {}
    for key in GRADE_CONTEXT_KEYS:
        value = ctx.get(key)
        kw[key] = (value if value in ("BULL", "BEAR", "FLAT") else None) if key.startswith("trend_") else _num(value)
    return grade_setup(GradeInputs(side=side_of(side), entry=float(entry), stop=float(stop), target=float(target),
                                   funding_rate_8h=funding_rate_8h, **kw)).as_dict()


def _no_trade(run_id: uuid.UUID, instrument_id: str, mandate: Mandate, reason: str) -> tuple[str, Mandate]:
    logger.info("Debate %s entry on %s refused by mandate %s: %s", run_id, instrument_id, mandate.name, reason)
    return "NO_TRADE", mandate


async def _route_perp(conn, run_id: uuid.UUID, mandate: Mandate, instrument: str, side: str, base: dict[str, Any],
                      conviction: float, allocation_pct: float, *, now: datetime.datetime, redis,
                      marks: dict[str, LiveMark] | None, stop_loss: float | None, take_profit: float | None,
                      reference_price: float | None,
                      grade_context: dict[str, Any] | None) -> tuple[ActionClass, dict[str, Any], str] | tuple[str, Mandate]:
    """A perp verdict: (action, payload, summary) to approve, or a (NO_TRADE, mandate) result."""
    held_side, held = await perp_position(conn, mandate, instrument)
    held = held.normalize()
    if held > 0 and held_side != ("LONG" if side == "BUY" else "SHORT"):
        # Against the mandate's position: close it, reduce-only. Never refused by a filter or the session
        payload = {**base, "quantity": format(held, "f"), "reduce_only": True}
        return (ActionClass.REDUCE_RISK, payload,
                f"{side} {instrument} reduce-only {held:g} (closes the mandate's {held_side}) · conviction {conviction:.2f}")
    if not mandate.allow_perps or mandate.autonomy_level != AutonomyLevel.L1:
        return _no_trade(run_id, instrument, mandate, "MANDATE_PERPS_NOT_ALLOWED: perps need the opt-in at L1")
    long = side == "BUY"
    if not (stop_loss and take_profit):
        return _no_trade(run_id, instrument, mandate,
                         f"{STOP_REQUIRED}: a perp entry needs a structural stop and a target; the debate has none")
    stop, target = float(stop_loss), float(take_profit)
    if long != (stop < target):
        if not long:  # the scanner's levels are a long's: no code derives a short's stop and target from them
            return _no_trade(run_id, instrument, mandate,
                             f"{NO_SHORT_STRUCTURE}: no short structure (the candidate's stop {stop:g} and target "
                             f"{target:g} are a long's)")
        return _no_trade(run_id, instrument, mandate,
                         f"{STOP_REQUIRED}: stop {stop:g} and target {target:g} are a short's, not a long's")
    if not (reference_price and reference_price > 0):
        return _no_trade(run_id, instrument, mandate, "NO_REFERENCE_PRICE: no decision price to measure the stop from")
    entry = float(reference_price)
    if not (stop < entry < target if long else target < entry < stop):
        return _no_trade(run_id, instrument, mandate,
                         f"{STOP_REQUIRED}: stop {stop:g} / target {target:g} on the wrong side of the entry {entry:g}")
    if reason := await entry_filter_reason(conn, mandate, conviction, now=now, reference_price=entry,
                                           stop_loss=stop, take_profit=target, side=side):
        return _no_trade(run_id, instrument, mandate, reason)
    grade = perp_setup_grade(side, entry, stop, target, grade_context, await funding_8h(conn, redis, instrument))
    if grade["grade"] == "NO_TRADE":
        return _no_trade(run_id, instrument, mandate, f"setup grade NO_TRADE: {'; '.join(grade['reasons'])}")
    room = await entry_room_pct(conn, mandate, instrument, marks)
    if room < MIN_MANDATE_ENTRY_PCT:
        return _no_trade(run_id, instrument, mandate, "MANDATE_POSITION_LIMIT_REACHED")
    alloc = round(min(float(allocation_pct), float(mandate.max_position_pct), float(room)), 2)
    # The Brain card's dry run, with the orchestrator's sizing (a margin too large for 1x is cut, not refused)
    from cryptonite_api.leverage_card import card_leverage_plan

    card = {"instrument_id": instrument, "side": side, "order_type": "MARKET", "allocation_pct": alloc,
            "stop_loss": stop, "setup_grade": grade["grade"]}
    plan = await card_leverage_plan(_OneConn(conn), redis, mandate, card, shrink_margin_to_fit=True)
    if not plan.ok:
        return _no_trade(run_id, instrument, mandate,
                         f"{plan.reject}: the {plan.binding_ceiling or 'leverage plan'} allows no order")
    alloc = float((plan.margin_usd / mandate.capital_budget_usd * 100).quantize(Decimal("0.01")))
    payload = {**base, "allocation_pct": alloc, "stop_loss": stop, "margin_mode": "ISOLATED",
               "setup_grade": grade["grade"], "grade_reasons": grade["reasons"][:12], "grade": grade,
               "perp_side": "LONG" if long else "SHORT", "perp_plan": plan.as_dict()}
    loss_pct = f" ({float(plan.loss_at_stop_pct):.2f}% of budget)" if plan.loss_at_stop_pct is not None else ""
    liq = f"{float(plan.liquidation_price):,.2f}" if plan.liquidation_price is not None else "none"
    summary = (f"{payload['perp_side']} {instrument} MARKET ISOLATED, dry run {plan.leverage}x ({plan.binding_ceiling}), "
               f"margin {alloc:g}% of mandate (${float(plan.margin_usd):,.0f}) · grade {grade['grade']} · stop {stop:g} · "
               f"target {target:g} · loss at stop ${float(plan.loss_at_stop_usd):,.2f}{loss_pct} · liq {liq} · "
               f"conviction {conviction:.2f}")
    return ActionClass.ADD_RISK, payload, summary


async def route_entry(conn, run_id: uuid.UUID, instrument_id: str, recommendation: str, conviction: float,
                      allocation_pct: float, *, mandate_id: str | None = None,
                      now: datetime.datetime | None = None,
                      decision_trace_id: str | None = None,
                      marks: dict[str, LiveMark] | None = None,
                      stop_loss: float | None = None,
                      take_profit: float | None = None,
                      reference_price: float | None = None,
                      redis=None,
                      grade_context: dict[str, Any] | None = None) -> tuple[str, Mandate | None]:
    """Returns (debate_runs status, mandate). ``decision_trace_id`` (R7.4) rides in the command and
    approval payloads so the order and its fill can be traced back to the scanner candidate. ``marks``
    (``cryptonite_schemas.marks.read_marks``) values the mandate's positions for its room. ``reference_price``
    (the decision price) is the entry the R:R filter measures from. ``stop_loss`` / ``take_profit``: the scanner
    candidate's levels (none for a Brain- or Inbox-started debate). ``redis`` (the perp mark, funding and leverage
    brackets) and ``grade_context`` (``brief_data``'s) are read only on a perp route."""
    now = now or datetime.datetime.now(datetime.UTC)
    side = side_of(recommendation)
    if side is None:
        return "NO_TRADE", None
    mandate, instrument = await resolve_route(conn, instrument_id, mandate_id)
    if mandate is None:
        return "PENDING_APPROVAL", None  # outside any mandate: only the operator can act on it
    base = {"instrument_id": instrument, "side": side, "order_type": "MARKET",
            "recommendation": recommendation, "conviction_score": conviction, "source": "debate", "run_id": str(run_id)}
    if decision_trace_id:
        base["decision_trace_id"] = str(decision_trace_id)
    # The candidate's levels: the exit monitor's structural stop and TP1 for a spot position. Never ``stop_price``:
    # the dispatcher reads that as a resting STOP_MARKET (perps only), so a spot entry carrying it is refused
    if stop_loss is not None and float(stop_loss) > 0:
        base["proposed_stop_loss"] = float(stop_loss)
    if take_profit is not None and float(take_profit) > 0:
        base["proposed_take_profit"] = float(take_profit)
        base["take_profit"] = float(take_profit)
    if is_perp_instrument(instrument):
        routed = await _route_perp(conn, run_id, mandate, instrument, side, base, conviction, allocation_pct, now=now,
                                   redis=redis, marks=marks, stop_loss=stop_loss, take_profit=take_profit,
                                   reference_price=reference_price, grade_context=grade_context)
        if len(routed) == 2:
            return routed
        action, payload, summary = routed
    elif side == "BUY":
        if reason := await entry_filter_reason(conn, mandate, conviction, now=now, reference_price=reference_price,
                                               stop_loss=stop_loss, take_profit=take_profit):
            return _no_trade(run_id, instrument, mandate, reason)
        room = await entry_room_pct(conn, mandate, instrument, marks)
        if room < MIN_MANDATE_ENTRY_PCT:
            return "NO_TRADE", mandate  # already at the position limit: nothing to ask the operator
        alloc = round(min(float(allocation_pct), float(mandate.max_position_pct), float(room)), 2)
        payload = {**base, "allocation_pct": alloc}
        summary = (f"BUY {base['instrument_id']} {alloc:g}% of mandate "
                   f"(${float(mandate.capital_budget_usd) * alloc / 100:,.0f}) · conviction {conviction:.2f}")
        if mandate.min_rr is not None and not (stop_loss and take_profit):
            payload["filters_skipped"] = [MIN_RR_SKIPPED]
            summary += f" · {MIN_RR_SKIPPED}"
        action = ActionClass.ADD_RISK
    else:
        held = await mandate_holding(conn, mandate, instrument)
        if held <= 0:
            return "NO_TRADE", mandate  # spot is long-only: nothing of the mandate's to sell
        payload = {**base, "quantity": str(held)}
        summary = f"SELL {held} {base['instrument_id']} (the mandate's position) · conviction {conviction:.2f}"
        action = ActionClass.REDUCE_RISK
    if action is ActionClass.REDUCE_RISK and mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, "SUBMIT_ORDER", payload, f"debate:{run_id}")
        return "COMPLETED", mandate
    approval = await _approval(conn, mandate, action, "SUBMIT_ORDER", payload, summary, "debate", run_id, now)
    if approval is None:  # ADD_RISK at L3: no approval
        await _enqueue(conn, mandate, "SUBMIT_ORDER", payload, f"debate:{run_id}")
        return "COMPLETED", mandate
    return "PENDING_APPROVAL", mandate


async def route_exit(conn, run_id: uuid.UUID, instrument_id: str, recommendation: str,
                     now: datetime.datetime | None = None) -> tuple[str, Mandate | None]:
    """An exit debate's REDUCE/CLOSE on a mandate's position: immediate at L2/L3, a veto window that
    proceeds at L1. Anything else waits for the operator."""
    now = now or datetime.datetime.now(datetime.UTC)
    rec = str(recommendation or "").upper()
    if rec not in ("REDUCE", "CLOSE"):
        return "PENDING_APPROVAL", None
    mandate, held = await position_owner(conn, instrument_id)
    if mandate is None or held <= 0:
        return "PENDING_APPROVAL", None
    qty = held if rec == "CLOSE" else (held / 2)
    payload = {"instrument_id": canonical_instrument(instrument_id), "quantity": str(qty), "reason": f"EXIT_DEBATE_{rec}",
               "source": "exit_debate", "run_id": str(run_id)}
    if mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, "CLOSE_POSITION", payload, f"exit_debate:{run_id}")
        return "COMPLETED", mandate
    await _approval(conn, mandate, ActionClass.REDUCE_RISK, "CLOSE_POSITION", payload,
                    f"{rec} {qty} {payload['instrument_id']} (exit debate)", "exit_debate", run_id, now)
    return "PENDING_APPROVAL", mandate
