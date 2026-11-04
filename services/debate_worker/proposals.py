"""H13b: a scanner setup the debate said HOLD on reaches the operator as a proposal, and only their click can execute it.

A HOLD ends the run: ``route_entry`` queues nothing and writes no approval, at any autonomy level. This module writes
one ``setup_proposals`` row next to that result. It never touches ``_approval`` or ``_enqueue``
(``timeout_policy_for`` makes an L2 ADD_RISK proceed on timeout and an L3 need no approval), the run's status or its
policy B row. The row expires unanswered; the API's approve is the only way from it to an order.

``create_for_hold`` runs inside the debate's finalization transaction, in its own savepoint, and never raises: a failure
here leaves the debate's result intact.
"""

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from cryptonite_execution.related_positions import opposite_positions, read_related_positions
from cryptonite_scanner.brief import candidate_direction
from cryptonite_schemas.mandate import DEFAULT_APPROVAL_TTL_S, Mandate
from cryptonite_schemas.profile import Market, market_of
from cryptonite_schemas.reward_risk import meets_min_rr, reward_to_risk
from gate import AUTOMATIC_SOURCES
from mandate_routing import PAPER_ACCOUNT_ID, entry_profile_route

logger = logging.getLogger("cryptonite.debate_worker.proposals")

SYSTEM_ALERTS_CHANNEL = "channel:system_alerts"
ALERT_KIND = "SCANNER_PROPOSAL"
REASONING_CHARS = 600
_SIDE_OF_DIRECTION = {"LONG": "BUY", "SHORT": "SELL"}
_GRADE_OF_REASON = {"GRADE_A": "A", "GRADE_B": "B", "GRADE_C": "C"}


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
                      list(grade.get("reasons") or [])[:12], replay)


async def _skip_reason(conn, debate: HoldDebate, mandate: Mandate | None) -> tuple[str | None, dict[str, Any]]:
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
    # A HOLD carries no block_reason even with an opposite position open (opposite_block_reason reads the verdict's
    # side), so the candidate's side is checked here
    if opposite_positions(await read_related_positions(conn, debate.instrument_id, PAPER_ACCOUNT_ID), side):
        return "OPPOSITE_POSITION_OPEN", {}
    refused, instrument = await entry_profile_route(conn, mandate, debate.instrument_id, side)
    if refused:
        return refused, {}
    # A Brain card plans spot and perp orders only: a margin SELL would read as closing a spot holding
    if market_of(instrument) is Market.SPOT_MARGIN or (side == "SELL" and market_of(instrument) is not Market.PERP):
        return f"MARKET_NOT_PROPOSABLE: {market_of(instrument).value}", {}
    return None, {"side": side, "instrument": instrument, "entry": float(entry), "stop": float(stop),
                  "target": float(target), "grade": _GRADE_OF_REASON[policy_a["reason"]]}


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
    return card


def proposal_alert(proposal_id: uuid.UUID, debate: HoldDebate, mandate: Mandate, f: dict[str, Any]) -> dict[str, Any]:
    side = "long" if f["side"] == "BUY" else "short"
    return {"kind": ALERT_KIND, "severity": "ACTION", "proposal_id": str(proposal_id), "mandate": mandate.name,
            "instrument_id": f["instrument"], "side": f["side"], "grade": f["grade"],
            "message": (f"{f['instrument']} {side}, grade {f['grade']}: the debate said HOLD. Open Setups to review to "
                        "decide within 15 minutes; nothing trades unless you approve.")}


async def create_for_hold(conn, debate: HoldDebate, mandate: Mandate | None,
                          now: datetime.datetime | None = None) -> dict[str, Any] | None:
    """Write the debate's proposal when it qualifies. Returns the alert to publish after the commit, else None."""
    if debate.recommendation != "HOLD" or debate.candidate_id is None:
        return None  # the common case: a BUY or SELL keeps its route, and a Brain or operator debate has no candidate
    try:
        async with conn.transaction():  # a savepoint: a failure here never aborts the debate's transaction
            reason, fields = await _skip_reason(conn, debate, mandate)
            if reason:
                logger.info("Run %s: no proposal for candidate %s: %s", debate.run_id, debate.candidate_id, reason)
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
