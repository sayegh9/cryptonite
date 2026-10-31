"""ADR 0011: route debate verdicts by explicit profile or exclusive coin ownership."""

import datetime
import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptonite_execution import profile_lifecycle
from cryptonite_execution.leverage import (
    ACCOUNT_EXPOSURE_CAP,
    ACCOUNT_MAX_LEVERAGE,
    DEFAULT_FEE_ROUND_TRIP,
    DEFAULT_SLIPPAGE_ALLOWANCE,
    LeverageLimits,
    plan_spot_margin_order,
)
from cryptonite_execution.profile_log import record_decision
from cryptonite_execution.spot_margin import size_margin_borrow
from cryptonite_schemas.debate import verdict_direction
from cryptonite_schemas.mandate import (
    DEFAULT_APPROVAL_TTL_S,
    DEFAULT_REDUCE_VETO_S,
    MIN_MANDATE_ENTRY_PCT,
    ActionClass,
    AutonomyLevel,
    Mandate,
    canonical_instrument,
    in_active_hours,
    mandate_id_from_instance,
    timeout_policy_for,
)
from cryptonite_schemas.marks import LiveMark
from cryptonite_schemas.marks import entry_room_pct as room_at_marks
from cryptonite_schemas.profile import (
    Market,
    allows_side,
    base_asset,
    instrument_for,
    market_of,
    select_market,
    symbol_of,
)
from cryptonite_schemas.runtime_settings import default_margin_settings, read_margin_settings
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
                   "min_conviction, min_rr, daily_loss_limit_pct, daily_profit_target_pct, profit_lock_pct, "
                   "markets, direction, style, max_open_positions, max_debates_per_day, dry_run")


# Recorded in the payload and the summary of a debate entry the R:R filter could not measure (a debate carries no
# stop or target today), so the skipped filter is visible to the operator
MIN_RR_SKIPPED = "min_rr skipped: no stop/target"
# A debate perp entry the routing refuses (NO_TRADE, logged with the reason)
STOP_REQUIRED = "STOP_REQUIRED"
NO_SHORT_STRUCTURE = "NO_SHORT_STRUCTURE"
NO_STRUCTURE = "NO_STRUCTURE"


def side_of(recommendation: str) -> str | None:
    """The order side of a verdict (``normalize_verdict``'s mapping; "strong buy" with a space is
    STRONG_BUY); None for HOLD or an unknown verdict."""
    direction = verdict_direction(str(recommendation or "").strip().replace(" ", "_"))
    return direction if direction in ("BUY", "SELL") else None


async def resolve_profile(conn, instrument_id: str, mandate_id: str | None = None) -> Mandate | None:
    """ADR 0011 D6: the payload profile wins; otherwise resolve only the base coin's lock owner.

    Keep stopped profiles visible so routing can record PROFILE_NOT_RUNNING instead of silently
    treating their candidates as operator trades. Never infer ownership from overlapping scopes.
    """
    if mandate_id:
        mid = uuid.UUID(str(mandate_id))
    else:
        try:
            coin = base_asset(instrument_id)
        except ValueError:
            return None
        mid = await conn.fetchval("SELECT mandate_id FROM ai_profile_coin_locks WHERE base_asset = $1", coin)
        if mid is None:
            return None
    row = await conn.fetchrow(f"SELECT {MANDATE_COLUMNS} FROM ai_mandates WHERE id = $1", mid)
    return Mandate.from_row(row) if row else None


async def entry_profile_route(conn, mandate: Mandate, instrument_id: str, side: str) -> tuple[str | None, str]:
    """ADR 0011: apply the profile entry refusals in their specified order, then select its market."""
    if mandate.status.value != "ACTIVE":
        return "PROFILE_NOT_RUNNING", instrument_id
    if not allows_side(mandate.direction, "LONG" if side == "BUY" else "SHORT"):
        return "DIRECTION_NOT_ALLOWED", instrument_id
    count = await conn.fetchval(
        "SELECT count(*) FROM paper_positions WHERE account_id = $1 AND instance_id = $2 "
        "AND status = 'OPEN' AND quantity > 0", PAPER_ACCOUNT_ID, mandate.instance_id)
    if int(count or 0) >= mandate.max_open_positions:
        return "MAX_OPEN_POSITIONS", instrument_id
    if await profile_lifecycle.coin_lock_conflict(conn, mandate, instrument_id, "debate"):
        return "COIN_LOCKED", instrument_id
    market = select_market("LONG" if side == "BUY" else "SHORT", mandate.max_leverage, mandate.markets)
    if market is None:
        return "NO_MARKET", instrument_id
    instrument = instrument_for(instrument_id, market)
    return None, instrument


async def reduction_instrument(conn, mandate: Mandate, instrument_id: str, side: str) -> str | None:
    """Find an owned holding this verdict reduces before entry-only status, direction and market checks."""
    if side == "SELL":
        spot = instrument_for(instrument_id, Market.SPOT)
        if await mandate_holding(conn, mandate, spot) > 0:
            return spot
    perp = instrument_for(instrument_id, Market.PERP)
    held_side, held = await perp_position(conn, mandate, perp)
    if held > 0 and held_side != ("LONG" if side == "BUY" else "SHORT"):
        return perp
    margin = instrument_for(instrument_id, Market.SPOT_MARGIN)
    held_side, held = await perp_position(conn, mandate, margin)
    if held > 0 and held_side != ("LONG" if side == "BUY" else "SHORT"):
        return margin
    return None


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


# debate_runs status for a run routed twice, from its existing approval's status
_EXISTING_APPROVAL_STATUS = {"PENDING": "PENDING_APPROVAL", "APPROVED": "COMPLETED", "EXPIRED_PROCEEDED": "COMPLETED",
                             "VETOED": "NO_TRADE", "EXPIRED": "NO_TRADE", "CANCELLED": "NO_TRADE"}


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


async def _no_trade(conn, run_id: uuid.UUID, instrument_id: str, mandate: Mandate, reason: str) -> tuple[str, Mandate]:
    logger.info("Debate %s entry on %s refused by mandate %s: %s", run_id, instrument_id, mandate.name, reason)
    await record_decision(conn, mandate.id, "ROUTE", "NO_TRADE", reason.split(":", 1)[0],
                          instrument_id=instrument_id, run_id=run_id, explanation=reason)
    return "NO_TRADE", mandate


async def _route_perp(conn, run_id: uuid.UUID, mandate: Mandate, instrument: str, side: str, base: dict[str, Any],
                      conviction: float, allocation_pct: float, *, now: datetime.datetime, redis,
                      marks: dict[str, LiveMark] | None, stop_loss: float | None, take_profit: float | None,
                      reference_price: float | None,
                      grade_context: dict[str, Any] | None,
                      structure_reason: str | None = None) -> tuple[ActionClass, dict[str, Any], str] | tuple[str, Mandate]:
    """A perp verdict: (action, payload, summary) to approve, or a (NO_TRADE, mandate) result."""
    held_side, held = await perp_position(conn, mandate, instrument)
    held = held.normalize()
    if held > 0 and held_side != ("LONG" if side == "BUY" else "SHORT"):
        # Against the mandate's position: close it, reduce-only. Never refused by a filter or the session. Sent as
        # CLOSE_POSITION with a quantity (as route_exit does): the orchestrator caps it to the live perp position and
        # checks ownership at execution, so a position that shrank during the veto window (TP1 half close, a partial
        # stop fill) is still reduced, and one that closed is a no-op, instead of a REDUCE_ONLY_OVERSIZE refusal. ``position_side``
        # makes it a no-op too when the mandate holds the other side by then (never a flip)
        payload = {**base, "quantity": format(held, "f"), "reduce_only": True, "reason": "DEBATE_REDUCE",
                   "position_side": held_side}
        return (ActionClass.REDUCE_RISK, payload,
                f"{side} {instrument} reduce-only {held:g} (closes the mandate's {held_side}) · conviction {conviction:.2f}")
    if not mandate.allow_perps:
        return await _no_trade(conn, run_id, instrument, mandate, "MANDATE_PERPS_NOT_ALLOWED")
    from cryptonite_api.mandate_choice import instrument_conflict

    # Another owner holding the perpetual: the orchestrator's mandate gate would refuse the entry after the operator
    # spent a TOTP on it (the same read-only check the Brain card and the Inbox run)
    if conflict := await instrument_conflict(conn, mandate, instrument, Decimal("0"), "debate"):
        return await _no_trade(conn, run_id, instrument, mandate, f"INSTRUMENT_CONFLICT: {conflict}")
    long = side == "BUY"
    if not (stop_loss and take_profit):
        return await _no_trade(conn, run_id, instrument, mandate,
                         f"{NO_STRUCTURE}: {structure_reason or 'no structural stop and target are available for this entry'}")
    stop, target = float(stop_loss), float(take_profit)
    if long != (stop < target):
        if not long:  # the scanner's levels are a long's: no code derives a short's stop and target from them
            return await _no_trade(conn, run_id, instrument, mandate,
                             f"{NO_SHORT_STRUCTURE}: no short structure (the candidate's stop {stop:g} and target "
                             f"{target:g} are a long's)")
        return await _no_trade(conn, run_id, instrument, mandate,
                         f"{STOP_REQUIRED}: stop {stop:g} and target {target:g} are a short's, not a long's")
    if not (reference_price and reference_price > 0):
        return await _no_trade(conn, run_id, instrument, mandate, "NO_REFERENCE_PRICE: no decision price to measure the stop from")
    entry = float(reference_price)
    if not (stop < entry < target if long else target < entry < stop):
        return await _no_trade(conn, run_id, instrument, mandate,
                         f"{STOP_REQUIRED}: stop {stop:g} / target {target:g} on the wrong side of the entry {entry:g}")
    if reason := await entry_filter_reason(conn, mandate, conviction, now=now, reference_price=entry,
                                           stop_loss=stop, take_profit=target, side=side):
        return await _no_trade(conn, run_id, instrument, mandate, reason)
    grade = perp_setup_grade(side, entry, stop, target, grade_context, await funding_8h(conn, redis, instrument))
    if grade["grade"] == "NO_TRADE":
        return await _no_trade(conn, run_id, instrument, mandate, f"setup grade NO_TRADE: {'; '.join(grade['reasons'])}")
    room = await entry_room_pct(conn, mandate, instrument, marks)
    if room < MIN_MANDATE_ENTRY_PCT:
        return await _no_trade(conn, run_id, instrument, mandate, "MANDATE_POSITION_LIMIT_REACHED")
    alloc = round(min(float(allocation_pct), float(mandate.max_position_pct), float(room)), 2)
    # The Brain card's dry run, with the orchestrator's sizing (a margin too large for 1x is cut, not refused)
    from cryptonite_api.leverage_card import card_leverage_plan

    card = {"instrument_id": instrument, "side": side, "order_type": "MARKET", "allocation_pct": alloc,
            "stop_loss": stop, "setup_grade": grade["grade"]}
    plan = await card_leverage_plan(_OneConn(conn), redis, mandate, card, shrink_margin_to_fit=True)
    if not plan.ok:
        return await _no_trade(conn, run_id, instrument, mandate,
                         f"{plan.reject}: the {plan.binding_ceiling or 'leverage plan'} allows no order")
    alloc = float((plan.margin_usd / mandate.capital_budget_usd * 100).quantize(Decimal("0.01")))
    # ``leverage``: the dry run's (code, never the model's), the ceiling the operator approved. The orchestrator sizes
    # again at execution with plan_leveraged_order, which clamps a requested leverage to what its ceilings allow now,
    # so the executed leverage is at most this one even when the price moved toward the stop meanwhile
    payload = {**base, "allocation_pct": alloc, "stop_loss": stop, "margin_mode": "ISOLATED", "leverage": plan.leverage,
               "setup_grade": grade["grade"], "grade_reasons": grade["reasons"][:12], "grade": grade,
               "perp_side": "LONG" if long else "SHORT", "perp_plan": plan.as_dict()}
    loss_pct = f" ({float(plan.loss_at_stop_pct):.2f}% of budget)" if plan.loss_at_stop_pct is not None else ""
    liq = f"{float(plan.liquidation_price):,.2f}" if plan.liquidation_price is not None else "none"
    summary = (f"{payload['perp_side']} {instrument} MARKET ISOLATED, dry run {plan.leverage}x ({plan.binding_ceiling}), "
               f"margin {alloc:g}% of mandate (${float(plan.margin_usd):,.0f}) · grade {grade['grade']} · stop {stop:g} · "
               f"target {target:g} · loss at stop ${float(plan.loss_at_stop_usd):,.2f}{loss_pct} · liq {liq} · "
               f"conviction {conviction:.2f}")
    return ActionClass.ADD_RISK, payload, summary


async def _route_spot_margin(conn, run_id: uuid.UUID, mandate: Mandate, instrument: str, side: str,
                             base: dict[str, Any], conviction: float, allocation_pct: float, *,
                             now: datetime.datetime, marks: dict[str, LiveMark] | None,
                             stop_loss: float | None, take_profit: float | None,
                             reference_price: float | None,
                             structure_reason: str | None = None) -> tuple[ActionClass, dict[str, Any], str] | tuple[str, Mandate]:
    """Create a fail-closed margin plan for the approval payload using the runtime pair settings."""
    long = side == "BUY"
    held_side, held = await perp_position(conn, mandate, instrument)
    held = held.normalize()
    if held > 0 and held_side != ("LONG" if long else "SHORT"):
        payload = {**base, "quantity": format(held, "f"), "reduce_only": True,
                   "reason": "DEBATE_REDUCE", "position_side": held_side}
        return (ActionClass.REDUCE_RISK, payload,
                f"{side} {instrument} reduce-only {held:g} (closes the mandate's {held_side}) · conviction {conviction:.2f}")
    try:
        entry = Decimal(str(reference_price))
        stop = Decimal(str(stop_loss))
        target = Decimal(str(take_profit))
    except (InvalidOperation, TypeError, ValueError):
        return await _no_trade(conn, run_id, instrument, mandate,
                               f"{NO_STRUCTURE}: {structure_reason or 'no structural stop and target are available for this entry'}")
    if not all(x.is_finite() and x > 0 for x in (entry, stop, target)):
        return await _no_trade(conn, run_id, instrument, mandate, STOP_REQUIRED)
    if not (stop < entry < target if long else target < entry < stop):
        return await _no_trade(conn, run_id, instrument, mandate, STOP_REQUIRED)
    if reason := await entry_filter_reason(conn, mandate, conviction, now=now, reference_price=float(entry),
                                           stop_loss=float(stop), take_profit=float(target), side=side):
        return await _no_trade(conn, run_id, instrument, mandate, reason)
    room_pct = await entry_room_pct(conn, mandate, instrument, marks)
    if room_pct < MIN_MANDATE_ENTRY_PCT:
        return await _no_trade(conn, run_id, instrument, mandate, "MANDATE_POSITION_LIMIT_REACHED")
    settings_rows = await conn.fetch("SELECT key, value FROM system_settings WHERE key LIKE 'margin.%'")
    settings, problems = read_margin_settings({r["key"]: r["value"] for r in settings_rows},
                                              default_margin_settings())
    for problem in problems:
        logger.warning("Margin setting refused in route plan; using its last-good default: %s", problem)
    equity_raw = await conn.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID)
    if equity_raw is None or Decimal(str(equity_raw)) <= 0:
        return await _no_trade(conn, run_id, instrument, mandate, "NON_POSITIVE_EQUITY")
    equity = Decimal(str(equity_raw))
    budget = mandate.capital_budget_usd
    requested_margin = budget * Decimal(str(allocation_pct)) / Decimal("100")
    max_notional = min(budget * room_pct / Decimal("100"), equity * ACCOUNT_EXPOSURE_CAP)
    limits = LeverageLimits(
        risk_budget_usd=budget * mandate.max_risk_per_trade_pct / Decimal("100"),
        max_notional_usd=max_notional,
        max_leverage=min(mandate.max_leverage, ACCOUNT_MAX_LEVERAGE),
        slippage_pct=DEFAULT_SLIPPAGE_ALLOWANCE,
        fee_pct=DEFAULT_FEE_ROUND_TRIP,
        min_notional_usd=equity * Decimal("0.01"),
    )
    plan = plan_spot_margin_order(
        instrument, side, entry, stop, requested_margin, limits,
        requested_leverage=mandate.max_leverage, pair_settings=settings.pair_settings.get(symbol_of(instrument)),
        base_asset=base_asset(instrument), budget_usd=budget, shrink_margin_to_fit=True,
    )
    if not plan.ok:
        return await _no_trade(conn, run_id, instrument, mandate,
                               f"{plan.reject}: margin dry run does not allow an order")
    size = size_margin_borrow("LONG" if long else "SHORT", plan.margin_usd, entry, Decimal(plan.leverage))
    asset = "USDT" if long else base_asset(instrument)
    borrowed = size.borrowed_quote if long else size.borrowed_base
    hourly_rate_pct = settings.interest_hourly_pct.get(asset, Decimal("0"))
    interest_per_hour = borrowed * hourly_rate_pct / Decimal("100")
    margin_pct = (plan.margin_usd / budget * Decimal("100")).quantize(Decimal("0.01"))
    plan_payload = plan.as_dict()
    margin_fields = {
        "side": "LONG" if long else "SHORT", "leverage": plan.leverage,
        "liquidation_price": str(plan.liquidation_price), "loss_at_stop_usd": str(plan.loss_at_stop_usd),
        "borrow_asset": asset, "borrow_amount": str(borrowed), "interest_per_hour": str(interest_per_hour),
    }
    payload = {
        **base, "allocation_pct": float(margin_pct), "stop_loss": float(stop), "take_profit": float(target),
        "margin_mode": "ISOLATED", "leverage": plan.leverage, "margin_plan": plan_payload,
        "margin": margin_fields,
    }
    summary = (f"{margin_fields['side']} {instrument} MARKET ISOLATED, dry run {plan.leverage}x, "
               f"margin {margin_pct}% of mandate (${plan.margin_usd:,.2f}) · stop {stop} · target {target} · "
               f"loss at stop ${plan.loss_at_stop_usd:,.2f} · liq {plan.liquidation_price} · "
               f"borrow {borrowed} {asset} · interest/hour {interest_per_hour} · conviction {conviction:.2f}")
    return ActionClass.ADD_RISK, payload, summary


async def route_entry(conn, run_id: uuid.UUID, instrument_id: str, recommendation: str, conviction: float,
                      allocation_pct: float, *, mandate_id: str | None = None,
                      now: datetime.datetime | None = None,
                      decision_trace_id: str | None = None,
                      marks: dict[str, LiveMark] | None = None,
                      stop_loss: float | None = None,
                      take_profit: float | None = None,
                      reference_price: float | None = None,
                      structure_reason: str | None = None,
                      redis=None,
                      grade_context: dict[str, Any] | None = None) -> tuple[str, Mandate | None]:
    """Returns (debate_runs status, mandate). ``decision_trace_id`` (R7.4) rides in the command and
    approval payloads so the order and its fill can be traced back to the scanner candidate. ``marks``
    (``cryptonite_schemas.marks.read_marks``) values the mandate's positions for its room. ``reference_price``
    (the decision price) is the entry the R:R filter measures from. ``stop_loss`` / ``take_profit``: the scanner
    candidate's levels (none for a Brain- or Inbox-started debate). ``redis`` (the perp mark, funding and leverage
    brackets) and ``grade_context`` (``brief_data``'s) are read only on a perp route."""
    now = now or datetime.datetime.now(datetime.UTC)
    mandate = await resolve_profile(conn, instrument_id, mandate_id)
    side = side_of(recommendation)
    if mandate is None:
        return ("NO_TRADE" if side is None else "PENDING_APPROVAL"), None
    await record_decision(conn, mandate.id, "DEBATE", recommendation, instrument_id=instrument_id,
                          run_id=run_id, conviction=conviction)
    if side is None:
        return await _no_trade(conn, run_id, instrument_id, mandate, "HOLD")
    instrument = await reduction_instrument(conn, mandate, instrument_id, side)
    if instrument is None:
        reason, instrument = await entry_profile_route(conn, mandate, instrument_id, side)
        if reason:
            return await _no_trade(conn, run_id, instrument, mandate, reason)
    routed_market = market_of(instrument)
    if routed_market is Market.SPOT and side == "BUY":
        # A profile spot entry must have the structural stop used by the exit monitor.
        # Refuse before creating an ASK/VETO approval, including Brain and Inbox debates.
        try:
            entry = Decimal(str(reference_price))
            stop = Decimal(str(stop_loss))
        except (InvalidOperation, TypeError, ValueError):
            return await _no_trade(conn, run_id, instrument, mandate,
                                   f"{NO_STRUCTURE}: {structure_reason or 'no structural stop is available for this spot buy'}")
        if not entry.is_finite() or not stop.is_finite() or entry <= 0 or stop <= 0 or stop >= entry:
            return await _no_trade(conn, run_id, instrument, mandate, STOP_REQUIRED)
    elif routed_market is Market.SPOT_MARGIN:
        try:
            entry = Decimal(str(reference_price))
            stop = Decimal(str(stop_loss))
        except (InvalidOperation, TypeError, ValueError):
            return await _no_trade(conn, run_id, instrument, mandate,
                                   f"{NO_STRUCTURE}: {structure_reason or 'no structural stop is available for this margin entry'}")
        if (not entry.is_finite() or not stop.is_finite() or entry <= 0 or stop <= 0
                or (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry)):
            return await _no_trade(conn, run_id, instrument, mandate, STOP_REQUIRED)
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
    market = market_of(instrument)
    if market is Market.PERP:
        routed = await _route_perp(conn, run_id, mandate, instrument, side, base, conviction, allocation_pct, now=now,
                                   redis=redis, marks=marks, stop_loss=stop_loss, take_profit=take_profit,
                                   reference_price=reference_price, grade_context=grade_context,
                                   structure_reason=structure_reason)
        if len(routed) == 2:
            return routed
        action, payload, summary = routed
    elif market is Market.SPOT_MARGIN:
        routed = await _route_spot_margin(
            conn, run_id, mandate, instrument, side, base, conviction, allocation_pct, now=now,
            marks=marks, stop_loss=stop_loss, take_profit=take_profit, reference_price=reference_price,
            structure_reason=structure_reason,
        )
        if len(routed) == 2:
            return routed
        action, payload, summary = routed
    elif side == "BUY":
        if reason := await entry_filter_reason(conn, mandate, conviction, now=now, reference_price=reference_price,
                                               stop_loss=stop_loss, take_profit=take_profit):
            return await _no_trade(conn, run_id, instrument, mandate, reason)
        room = await entry_room_pct(conn, mandate, instrument, marks)
        if room < MIN_MANDATE_ENTRY_PCT:
            return await _no_trade(conn, run_id, instrument, mandate, "MANDATE_POSITION_LIMIT_REACHED")
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
            return await _no_trade(conn, run_id, instrument, mandate, "NO_OWN_HOLDING")
        payload = {**base, "quantity": str(held)}
        summary = f"SELL {held} {base['instrument_id']} (the mandate's position) · conviction {conviction:.2f}"
        action = ActionClass.REDUCE_RISK
    if action is ActionClass.ADD_RISK:
        # ADR 0011 D10 at runtime: with the master switch off, a Veto window or Fully automatic profile behaves as Ask
        # me: a TOTP approval, never a veto window or a direct enqueue
        if mandate.autonomy_level > AutonomyLevel.L1 and not await profile_lifecycle.autotrade_switch_on(conn):
            logger.info("Debate %s: auto-trade switch off; mandate %s (level %d) asks for approval as at level 1",
                        run_id, mandate.name, int(mandate.autonomy_level))
            mandate = mandate.model_copy(update={"autonomy_level": AutonomyLevel.L1})
    # A perp reduction closes the mandate's own position (capped to what is live at execution); every other order is a
    # SUBMIT_ORDER
    command_type = "CLOSE_POSITION" if action is ActionClass.REDUCE_RISK and market in (Market.PERP, Market.SPOT_MARGIN) else "SUBMIT_ORDER"
    if action is ActionClass.REDUCE_RISK and mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, command_type, payload, f"debate:{run_id}")
        await record_decision(conn, mandate.id, "ROUTE", "ENQUEUED", instrument_id=instrument, run_id=run_id)
        return "COMPLETED", mandate
    if timeout_policy_for(action, mandate.autonomy_level, in_active_hours(mandate.active_hours_utc, now)) is None:
        await _enqueue(conn, mandate, command_type, payload, f"debate:{run_id}")  # ADD_RISK at L3: no approval needed
        await record_decision(conn, mandate.id, "ROUTE", "DRY_RUN_ROUTED" if mandate.dry_run else "ENQUEUED",
                              instrument_id=instrument, run_id=run_id)
        return "COMPLETED", mandate
    approval = await _approval(conn, mandate, action, command_type, payload, summary, "debate", run_id, now)
    if approval is None:
        # The insert hit ON CONFLICT (source, source_ref): this run was routed before. Report that approval's state and
        # queue nothing (it was never "no approval needed")
        existing = await conn.fetchval(
            "SELECT status FROM ai_approvals WHERE source = 'debate' AND source_ref = $1", str(run_id))
        status = _EXISTING_APPROVAL_STATUS.get(str(existing), "PENDING_APPROVAL")
        logger.warning("Debate %s was already routed (approval %s): returning %s, nothing queued", run_id, existing, status)
        return status, mandate
    outcome = "VETO_WINDOW" if action is ActionClass.ADD_RISK and mandate.autonomy_level == AutonomyLevel.L2 else "APPROVAL_CREATED"
    await record_decision(conn, mandate.id, "ROUTE", outcome, instrument_id=instrument, run_id=run_id)
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
    await record_decision(conn, mandate.id, "DEBATE", rec, instrument_id=instrument_id, run_id=run_id)
    qty = held if rec == "CLOSE" else (held / 2)
    payload = {"instrument_id": canonical_instrument(instrument_id), "quantity": str(qty), "reason": f"EXIT_DEBATE_{rec}",
               "source": "exit_debate", "run_id": str(run_id)}
    if mandate.autonomy_level >= AutonomyLevel.L2:
        await _enqueue(conn, mandate, "CLOSE_POSITION", payload, f"exit_debate:{run_id}")
        await record_decision(conn, mandate.id, "ROUTE", "ENQUEUED", instrument_id=instrument_id, run_id=run_id)
        return "COMPLETED", mandate
    await _approval(conn, mandate, ActionClass.REDUCE_RISK, "CLOSE_POSITION", payload,
                    f"{rec} {qty} {payload['instrument_id']} (exit debate)", "exit_debate", run_id, now)
    await record_decision(conn, mandate.id, "ROUTE", "APPROVAL_CREATED", instrument_id=instrument_id, run_id=run_id)
    return "PENDING_APPROVAL", mandate
