"""H13b: a scanner setup the debate said HOLD on reaches the operator as a proposal, and only their click can execute it.

A HOLD ends the run: ``route_entry`` queues nothing and writes no approval, at any autonomy level. This module writes
one ``setup_proposals`` row next to that result. It never touches ``_approval`` or ``_enqueue``
(``timeout_policy_for`` makes an L2 ADD_RISK proceed on timeout and an L3 need no approval), the run's status or its
policy B row. The row expires unanswered; the API's approve is the only way from it to an order.

H15 PR C, behind ``PAPER_RULE_ENTRIES`` (default off): on a PAPER profile set to Fully automatic with the master switch on,
a setup that would have become a card goes through ``route_entry`` instead, tagged ``paper_rule_entry``. The HOLD still
stands as the run's verdict and policy B's decision. Every other profile gets a card exactly as before.

``create_for_hold`` runs inside the debate's finalization transaction, in its own savepoint, and never raises: a failure
here leaves the debate's result intact.
"""

import datetime
import json
import logging
import os
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from cryptonite_execution import profile_lifecycle
from cryptonite_execution.related_positions import (
    opposite_positions,
    read_related_positions,
    related_instruments,
)
from cryptonite_scanner.brief import candidate_direction
from cryptonite_scanner.costs import cost_in_r, round_trip_cost_fraction
from cryptonite_schemas.funnel import PROPOSAL_SKIPPED
from cryptonite_schemas.mandate import DEFAULT_APPROVAL_TTL_S, AutonomyLevel, Mandate
from cryptonite_schemas.marks import read_live_quote, resolve_quote_price
from cryptonite_schemas.profile import Market, market_of, underlying_spot
from cryptonite_schemas.reward_risk import meets_min_rr, reward_to_risk
from gate import AUTOMATIC_SOURCES
from mandate_routing import PAPER_ACCOUNT_ID, entry_profile_route, route_entry

logger = logging.getLogger("cryptonite.debate_worker.proposals")

SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
ALERT_KIND = "SCANNER_PROPOSAL"
REASONING_CHARS = 600
_SIDE_OF_DIRECTION = {"LONG": "BUY", "SHORT": "SELL"}
MAX_COST_R = 0.25  # the cheapest round trip (perp) as a share of the risk: a stop nearer than 0.8% of the price
_GRADE_OF_REASON = {"GRADE_A": "A", "GRADE_B": "B", "GRADE_C": "C"}
RULE_ENTRIES_FLAG = "PAPER_RULE_ENTRIES"
RULE_ENTRY_TAG = "paper_rule_entry"
_SETTLED_COMMANDS = ("COMPLETED", "REJECTED", "FAILED", "CANCELLED", "EXPIRED")


@dataclass(frozen=True)
class HoldDebate:
    """What the finalization knows of a finished debate."""
    run_id: uuid.UUID
    candidate_id: uuid.UUID | None
    instrument_id: str
    trigger_source: str
    recommendation: str
    final_status: str
    conviction: float
    reasoning: str
    trace_id: str | None
    non_actionable: str | None  # the run's evidence_reason or block_reason
    stop_loss: float | None
    take_profit: float | None
    candidate: dict[str, Any] | None
    grade_reasons: list[str]
    replay: dict[str, Any] | None = None
    grade_context: dict[str, Any] | None = None


def _candidate_of(payload: dict) -> dict[str, Any] | None:
    try:
        candidate = json.loads(payload.get("candidate_payload") or "{}")
    except (TypeError, ValueError):
        return None
    return candidate if isinstance(candidate, dict) else None


def hold_debate(payload: dict, *, run_id: uuid.UUID, candidate_id: uuid.UUID | None, instrument_id: str,
                trigger_source: str, recommendation: str, final_status: str, conviction: float, reasoning: str,
                trace_id: str | None, non_actionable: str | None, stop_loss: float | None, take_profit: float | None,
                brief_data: dict | None) -> HoldDebate:
    facts = brief_data if isinstance(brief_data, dict) else {}
    grade = facts.get("setup_grade") if isinstance(facts.get("setup_grade"), dict) else {}
    replay = facts.get("replay") if isinstance(facts.get("replay"), dict) else None
    return HoldDebate(run_id, candidate_id, instrument_id, trigger_source, recommendation, final_status, conviction,
                      reasoning, trace_id, non_actionable, stop_loss, take_profit, _candidate_of(payload),
                      list(grade.get("reasons") or [])[:12], replay, facts.get("grade_context"))


async def _price_tick(conn, instrument: str) -> Decimal | None:
    """The perp's price increment, else its spot's (as the orchestrator's stop ratchet reads it)."""
    spot = underlying_spot(instrument)
    rows = await conn.fetch("SELECT instrument_id, price_increment FROM instruments WHERE instrument_id = ANY($1::text[])",
                            [instrument, spot])
    found = {r["instrument_id"]: r["price_increment"] for r in rows if r["price_increment"]}
    raw = found.get(instrument) or found.get(spot)
    tick = Decimal(str(raw)) if raw is not None else None
    return tick if tick is not None and tick.is_finite() and tick > 0 else None


def _on_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


async def _at_perp_price(conn, redis, instrument: str, side: str, entry: Any, stop: Any,
                         target: Any) -> tuple[str | None, dict[str, Any]]:
    """A spot candidate's levels moved to the perp's price by one amount (mark - suggested entry), read now, so the stop
    and reward distances, and with them the setup's R:R, hold on the market it will trade. Without this the spot/perp
    gap alone reads as a slip at approve. A real move after this is still the slip check's to refuse."""
    from cryptonite_api.leverage_card import perp_mark  # as mandate_routing reaches the card's plan

    mark, _ = await perp_mark(redis, instrument)
    if mark is None or not mark.is_finite() or mark <= 0:
        return "NO_PERP_MARK", {}
    tick = await _price_tick(conn, instrument)
    if tick is None:
        return "NO_PRICE_TICK", {}
    spot_entry = Decimal(str(entry))
    shift = mark - spot_entry
    moved_entry = _on_tick(mark, tick)
    moved_stop = _on_tick(Decimal(str(stop)) + shift, tick)
    moved_target = _on_tick(Decimal(str(target)) + shift, tick)
    in_order = moved_stop < moved_entry < moved_target if side == "BUY" else moved_target < moved_entry < moved_stop
    # Rounding may break the floor by a tick: no proposal, and no level is nudged to keep it
    if moved_stop <= 0 or moved_target <= 0 or not in_order or not meets_min_rr(
            reward_to_risk(moved_entry, moved_stop, moved_target)):
        return "RR_BELOW_FLOOR", {}
    return None, {"entry": float(moved_entry), "stop": float(moved_stop), "target": float(moved_target),
                  "spot_levels": {"entry": float(spot_entry), "stop": float(stop), "target": float(target)},
                  "perp_shift": float(shift)}


async def _at_spot_price(redis, instrument: str, side: str, stop: Any, target: Any) -> tuple[str | None, dict[str, Any]]:
    """A margin entry trades the spot's price, so the scanner's stop and target stand, measured from the price now: the
    debate took minutes, and the route checks R:R from the entry it is given."""
    spot = underlying_spot(instrument)
    live = resolve_quote_price(spot, await read_live_quote(redis, spot)).price
    if live is None:
        return "NO_LIVE_PRICE", {}
    stop, target = Decimal(str(stop)), Decimal(str(target))
    if not (stop < live < target if side == "BUY" else target < live < stop) or not meets_min_rr(
            reward_to_risk(live, stop, target)):
        return "RR_BELOW_FLOOR", {}
    return None, {"entry": float(live), "stop": float(stop), "target": float(target)}


async def _skip_reason(conn, debate: HoldDebate, mandate: Mandate | None, redis) -> tuple[str | None, dict[str, Any]]:
    """(why no proposal, the fields a proposal is built from). Reads only."""
    if debate.trigger_source not in AUTOMATIC_SOURCES or debate.candidate_id is None:
        return "NOT_A_SCANNER_DEBATE", {}
    if debate.recommendation != "HOLD" or debate.final_status != "NO_TRADE":
        return "NOT_A_HOLD", {}
    if debate.non_actionable:
        return f"NON_ACTIONABLE: {debate.non_actionable}", {}
    if mandate is None:
        return "NO_PROFILE", {}
    # A HOLD's conviction is its confidence in holding, not in this trade, so the filter cannot be measured and the
    # card would always be refused
    if mandate.min_conviction is not None:
        return "MIN_CONVICTION_UNMEASURED", {}
    policy_a = await conn.fetchrow(
        "SELECT decision, reason FROM policy_decisions WHERE candidate_id = $1 AND policy = 'A'", debate.candidate_id)
    if policy_a is None or policy_a["decision"] != "TAKE" or policy_a["reason"] not in _GRADE_OF_REASON:
        return f"POLICY_A_{policy_a['reason'] if policy_a else 'MISSING'}", {}
    side = _SIDE_OF_DIRECTION.get(candidate_direction(debate.candidate) or "")
    if side is None:
        return "NO_DIRECTION", {}
    stop, target = debate.stop_loss, debate.take_profit
    entry = (debate.candidate or {}).get("suggested_entry")
    # Without these the card's slip check (grade_inputs.entry) and its R:R floor would not bind
    if not (stop and target and entry):
        return "NO_STRUCTURE", {}
    if not (stop < float(entry) < target if side == "BUY" else target < float(entry) < stop):
        return f"NO_STRUCTURE: stop {stop:g} / target {target:g} are not a {side}'s around {entry}", {}
    if not meets_min_rr(reward_to_risk(entry, stop, target)):
        return "RR_BELOW_FLOOR", {}
    # A stop this close loses too much of its risk to fees and slippage for the setup to pay (edge search, 2026-11-05)
    if (cost_in_r(float(entry), stop, round_trip_cost_fraction("perp")) or 0.0) > MAX_COST_R:
        return "STOP_TOO_TIGHT", {}
    # A HOLD carries no block_reason even with an opposite position open (opposite_block_reason reads the verdict's
    # side), so the candidate's side is checked here
    if opposite_positions(await read_related_positions(conn, debate.instrument_id, PAPER_ACCOUNT_ID), side):
        return "OPPOSITE_POSITION_OPEN", {}
    refused, instrument = await entry_profile_route(conn, mandate, debate.instrument_id, side, redis=redis)
    if refused:
        return refused, {}
    by_rule = await trades_by_rule(conn, mandate)  # read once: the switch's cache can turn between two reads
    market = market_of(instrument)
    # A Brain card plans spot and perp orders only: a margin SELL would read as closing a spot holding. A rule entry
    # makes no card, and route_entry plans margin
    if (market is Market.SPOT_MARGIN and not by_rule) or (side == "SELL" and market not in (Market.PERP, Market.SPOT_MARGIN)):
        return f"MARKET_NOT_PROPOSABLE: {market.value}", {}
    levels: dict[str, Any] = {"entry": float(entry), "stop": float(stop), "target": float(target)}
    if market is Market.PERP:
        refused, levels = await _at_perp_price(conn, redis, instrument, side, entry, stop, target)
    elif market is Market.SPOT_MARGIN:
        refused, levels = await _at_spot_price(redis, instrument, side, stop, target)
    if refused:
        return refused, {}
    return None, {"side": side, "instrument": instrument, "grade": _GRADE_OF_REASON[policy_a["reason"]],
                  "by_rule": by_rule, **levels}


def proposal_card(proposal_id: uuid.UUID, debate: HoldDebate, mandate: Mandate, f: dict[str, Any]) -> dict[str, Any]:
    """Shaped as ``OrderCardWidget``, so the Brain card's ``_plan_card`` plans it; the last fields are display only."""
    candidate = debate.candidate or {}
    card = {
        "id": str(proposal_id), "kind": "order_card", "status": "PENDING_APPROVAL", "instrument_id": f["instrument"],
        "side": f["side"], "order_type": "MARKET", "allocation_pct": float(mandate.max_position_pct),
        "stop_loss": f["stop"], "take_profit": f["target"], "confidence": None, "setup_grade": f["grade"],
        "grade_reasons": debate.grade_reasons, "grade_inputs": {"entry": f["entry"]}, "trace_id": debate.trace_id,
        "debate_link": {"run_id": str(debate.run_id), "recommendation": debate.recommendation,
                        "conviction_score": debate.conviction, "candidate_id": str(debate.candidate_id),
                        "against": True, "status": debate.final_status},
        "against_debate_verdict": "HOLD", "debate_reasoning": debate.reasoning[:REASONING_CHARS],
        "preset_slug": candidate.get("preset_slug") or candidate.get("spec_slug"),
        "timeframe": candidate.get("timeframe"),
    }
    if debate.replay:
        card["replay"] = debate.replay
    if "spot_levels" in f:  # display only: the levels the scanner found, and how far they moved to the perp's price
        card.update(spot_levels=f["spot_levels"], perp_shift=f["perp_shift"])
    return card


def proposal_alert(proposal_id: uuid.UUID, debate: HoldDebate, mandate: Mandate, f: dict[str, Any]) -> dict[str, Any]:
    side = "long" if f["side"] == "BUY" else "short"
    return {"kind": ALERT_KIND, "severity": "ACTION", "proposal_id": str(proposal_id), "mandate": mandate.name,
            "instrument_id": f["instrument"], "side": f["side"], "grade": f["grade"],
            "message": (f"{f['instrument']} {side}, grade {f['grade']}: the debate said HOLD. Open Setups to review to "
                        "decide within 15 minutes; nothing trades unless you approve.")}


async def _skipped(conn, debate: HoldDebate, reason: str) -> None:
    """H13c: the funnel's record of why (a failed write rolls back only the caller's savepoint)."""
    await conn.execute(
        """INSERT INTO candidate_funnel_stops (candidate_id, stage, reason, detail)
           VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (candidate_id) DO NOTHING""",
        debate.candidate_id, PROPOSAL_SKIPPED, reason, json.dumps({"run_id": str(debate.run_id)}))


async def trades_by_rule(conn, mandate: Mandate) -> bool:
    """The flag, a PAPER profile, Fully automatic and the master switch. Anything else gets a card."""
    return (os.environ.get(RULE_ENTRIES_FLAG, "0") == "1"
            and str(getattr(mandate.environment, "value", mandate.environment)) == "PAPER"
            and mandate.autonomy_level == AutonomyLevel.L3
            and await profile_lifecycle.autotrade_switch_on(conn))


async def _rule_entry_block(conn, debate: HoldDebate, mandate: Mandate) -> str | None:
    """A rule entry only opens a position on a flat coin: repeated signals must not pyramid, which a card's human would
    have judged. The lock makes two debates finishing together see each other's queued command."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"{RULE_ENTRY_TAG}:{mandate.id}")
    if await read_related_positions(conn, debate.instrument_id, PAPER_ACCOUNT_ID):
        return "POSITION_ALREADY_OPEN"
    instruments = list(related_instruments(debate.instrument_id).values())
    if await conn.fetchval(
            """SELECT 1 FROM execution_commands WHERE mandate_id = $1 AND NOT (status = ANY($2::text[]))
               AND payload->>'instrument_id' = ANY($3::text[]) LIMIT 1""",
            mandate.id, list(_SETTLED_COMMANDS), instruments):
        return "ENTRY_ALREADY_QUEUED"
    return None


async def _enter_by_rule(conn, debate: HoldDebate, mandate: Mandate, f: dict[str, Any], redis,
                         marks: dict | None, now: datetime.datetime | None) -> None:
    """Route the candidate's own side and levels. ``route_entry``'s run key makes a redelivery queue nothing, and a
    refusal is recorded and never retried."""
    if blocked := await _rule_entry_block(conn, debate, mandate):
        logger.info("Run %s: no rule entry for candidate %s: %s", debate.run_id, debate.candidate_id, blocked)
        await _skipped(conn, debate, blocked)
        return
    status, _ = await route_entry(
        conn, debate.run_id, debate.instrument_id, f["side"], 0.0, 0.0, mandate_id=str(mandate.id), now=now,
        decision_trace_id=debate.trace_id, marks=marks, stop_loss=f["stop"], take_profit=f["target"],
        reference_price=f["entry"], levels_source=RULE_ENTRY_TAG, redis=redis, grade_context=debate.grade_context,
        entry_source=RULE_ENTRY_TAG)
    logger.info("Run %s: rule entry %s %s for candidate %s: %s", debate.run_id, f["side"], f["instrument"],
                debate.candidate_id, status)
    if status != "COMPLETED":
        await _skipped(conn, debate, f"RULE_ENTRY_{status}")


async def create_for_hold(conn, debate: HoldDebate, mandate: Mandate | None, redis,
                          now: datetime.datetime | None = None, marks: dict | None = None) -> dict[str, Any] | None:
    """Write the debate's proposal when it qualifies, or trade it when the profile says so (``trades_by_rule``). Returns
    the alert to publish after the commit, else None."""
    if debate.recommendation != "HOLD" or debate.candidate_id is None:
        return None  # the common case: a BUY or SELL keeps its route, and a Brain or operator debate has no candidate
    try:
        async with conn.transaction():  # a savepoint: a failure here never aborts the debate's transaction
            reason, fields = await _skip_reason(conn, debate, mandate, redis)
            if reason:
                logger.info("Run %s: no proposal for candidate %s: %s", debate.run_id, debate.candidate_id, reason)
                await _skipped(conn, debate, reason)
                return None
            if fields["by_rule"]:
                await _enter_by_rule(conn, debate, mandate, fields, redis, marks, now)
                return None
            proposal_id = uuid.uuid4()
            now = now or datetime.datetime.now(datetime.UTC)
            inserted = await conn.fetchval(
                """INSERT INTO setup_proposals (id, candidate_id, run_id, mandate_id, instrument_id, side, card, expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                   ON CONFLICT (candidate_id) DO NOTHING RETURNING id""",
                proposal_id, debate.candidate_id, debate.run_id, mandate.id, fields["instrument"], fields["side"],
                json.dumps(proposal_card(proposal_id, debate, mandate, fields), default=str),
                now + datetime.timedelta(seconds=DEFAULT_APPROVAL_TTL_S))
            if inserted is None:
                return None
            logger.info("Run %s: proposal %s for %s %s", debate.run_id, proposal_id, fields["side"], fields["instrument"])
            return proposal_alert(proposal_id, debate, mandate, fields)
    except Exception as e:
        logger.warning("Run %s: the proposal was not created: %s", debate.run_id, e)
        return None


async def publish_alert(redis_client, alert: dict[str, Any] | None) -> None:
    """After the commit; a failed publish costs only the notice (the row is in Setups to review)."""
    if alert is None:
        return
    try:
        await redis_client.publish(SYSTEM_ALERTS_CHANNEL, json.dumps(alert))
    except Exception as e:
        logger.warning("Proposal alert %s not published: %s", alert.get("proposal_id"), e)
