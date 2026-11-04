"""H13c: the single furthest stage a scanner setup reached, read from what the pipeline already wrote.

Shared by the API's read model and the learner's alarms, so the page and the alarm cannot disagree. Pure apart from the
SQL text. Only ``PROPOSAL_SKIPPED`` is stored (``candidate_funnel_stops``); everything else is derived here.
"""

import datetime
from collections import Counter
from typing import Any

from cryptonite_schemas.profile import base_asset

UNACCOUNTED_AFTER = datetime.timedelta(minutes=10)

IN_PROGRESS = "IN_PROGRESS"
UNACCOUNTED = "UNACCOUNTED"
NOT_QUEUED = "NOT_QUEUED"
GATE_REFUSED = "GATE_REFUSED"
DEBATE_FAILED = "DEBATE_FAILED"
VERDICT_NOT_ROUTED = "VERDICT_NOT_ROUTED"
PROPOSAL_SKIPPED = "PROPOSAL_SKIPPED"
ROUTED = "ROUTED"
PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"

_OF_PROPOSAL = {"PENDING": PROPOSED, "APPROVED": APPROVED, "REJECTED": REJECTED, "EXPIRED": EXPIRED}
_ROUTED_STATUSES = frozenset({"COMPLETED", "PENDING_APPROVAL"})  # as policies.policy_b_for_route
FAILED_STATUSES = frozenset({"FAILED", "NO_PRICE"})
# What did not trade although the setup got that far, for the daily summary's "missed"
MISSED_STAGES = frozenset({GATE_REFUSED, VERDICT_NOT_ROUTED, PROPOSAL_SKIPPED, REJECTED, EXPIRED})

FINAL = "FINAL"
PROVISIONAL = "PROVISIONAL"
PENDING = "PENDING"

ROWS_SQL = """
    SELECT sc.id AS candidate_id, sc.created_at, sc.instrument_id, sc.timeframe, s.slug AS preset,
           sc.suggested_entry AS entry, sc.suggested_stop AS stop, sc.suggested_target AS target,
           o.status AS outbox_status, o.last_error AS outbox_error,
           r.id AS run_id, r.status AS debate_status, r.failure_reason, r.trigger_source,
           COALESCE(r.completed_at, r.created_at) AS debate_at, d.recommendation,
           fs.stage AS stop_stage, fs.reason AS stop_reason,
           p.status AS proposal_status, p.refusal AS proposal_refusal, p.expires_at AS proposal_expires_at,
           pa.decision AS policy_a, pa.reason AS policy_a_reason, pb.decision AS policy_b, pb.reason AS policy_b_reason,
           pc.decision AS policy_c, co.net_r_live, po.gross_r, po.state AS provisional_state
    FROM signal_candidates sc
    JOIN scan_runs sr ON sr.id = sc.scan_run_id AND sr.trigger_reason <> 'HISTORICAL_PRIMING'
    LEFT JOIN scan_spec_versions v ON v.id = sc.spec_version_id
    LEFT JOIN scan_specs s ON s.id = v.spec_id
    LEFT JOIN scanner_debate_outbox o ON o.candidate_id = sc.id
    LEFT JOIN debate_runs r ON r.candidate_id = sc.id
    LEFT JOIN LATERAL (SELECT recommendation FROM portfolio_decisions
                       WHERE run_id = r.id ORDER BY created_at DESC, id DESC LIMIT 1) d ON TRUE
    LEFT JOIN candidate_funnel_stops fs ON fs.candidate_id = sc.id
    LEFT JOIN setup_proposals p ON p.candidate_id = sc.id
    LEFT JOIN policy_decisions pa ON pa.candidate_id = sc.id AND pa.policy = 'A'
    LEFT JOIN policy_decisions pb ON pb.candidate_id = sc.id AND pb.policy = 'B'
    LEFT JOIN policy_decisions pc ON pc.candidate_id = sc.id AND pc.policy = 'C'
    LEFT JOIN candidate_outcomes co ON co.candidate_id = sc.id
    LEFT JOIN candidate_provisional_outcomes po ON po.candidate_id = sc.id
    WHERE sc.created_at >= $1
    ORDER BY sc.created_at DESC, sc.id
"""


def _recorded_stage(row: dict[str, Any], now: datetime.datetime) -> tuple[str | None, str | None]:
    """(a stage the pipeline finished at, its reason), or (None, what is missing) while nothing final is recorded."""
    proposal = row.get("proposal_status")
    if proposal:
        expires = row.get("proposal_expires_at")
        # EXPIRED is written on the proposals list's read; this read model writes nothing
        lapsed = proposal == "PENDING" and expires is not None and expires <= now
        return (EXPIRED if lapsed else _OF_PROPOSAL.get(proposal, PROPOSED)), row.get("proposal_refusal")
    status = row.get("debate_status")
    if status in _ROUTED_STATUSES:
        return ROUTED, status
    if row.get("stop_stage"):
        return row["stop_stage"], row.get("stop_reason")
    if status == "GATE_REFUSED":
        return GATE_REFUSED, row.get("failure_reason")
    if status in FAILED_STATUSES:
        return DEBATE_FAILED, row.get("failure_reason") or status
    if status == "RUNNING":
        return None, "DEBATE_STILL_RUNNING"
    if status:
        if str(row.get("recommendation") or "").upper() == "HOLD":
            return None, "HOLD_WITHOUT_RECORD"  # create_for_hold writes a proposal or a stop in the run's transaction
        return VERDICT_NOT_ROUTED, row.get("failure_reason") or row.get("policy_b_reason") or status
    outbox = row.get("outbox_status")
    if outbox is None:  # the outbox row commits with a fresh candidate, so no row means the bar was not fresh
        return NOT_QUEUED, "BAR_NOT_FRESH"
    if outbox in ("EXPIRED", "FAILED"):
        return NOT_QUEUED, row.get("outbox_error") or f"OUTBOX_{outbox}"
    return None, "QUEUED_NO_DEBATE"


def furthest_stage(row: dict[str, Any], now: datetime.datetime) -> tuple[str, str | None]:
    """(stage, reason). Nothing final after ``UNACCOUNTED_AFTER`` is UNACCOUNTED, never hidden."""
    stage, reason = _recorded_stage(row, now)
    if stage is not None:
        return stage, reason
    return (UNACCOUNTED if now - row["created_at"] >= UNACCOUNTED_AFTER else IN_PROGRESS), reason


def what_if(row: dict[str, Any]) -> tuple[float | None, str, str | None]:
    """(R, FINAL / PROVISIONAL / PENDING, the provisional exit state). Only ever read, never computed in a request."""
    if row.get("net_r_live") is not None:
        return float(row["net_r_live"]), FINAL, None
    if row.get("gross_r") is not None:
        return float(row["gross_r"]), PROVISIONAL, row.get("provisional_state")
    return None, PENDING, None


def coin_of(instrument_id: str) -> str:
    try:
        return base_asset(instrument_id)
    except ValueError:
        return instrument_id.split(".")[0]


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def funnel_row(row: dict[str, Any], now: datetime.datetime) -> dict[str, Any]:
    """One candidate as the API returns it and the alarms read it."""
    stage, reason = furthest_stage(row, now)
    r, state, provisional = what_if(row)
    entry, stop = _number(row.get("entry")), _number(row.get("stop"))
    return {
        "candidate_id": str(row["candidate_id"]), "time": row["created_at"].isoformat(),
        "instrument_id": row["instrument_id"], "coin": coin_of(row["instrument_id"]), "timeframe": row["timeframe"],
        "preset": row.get("preset"),
        "direction": None if entry is None or stop is None or entry == stop else ("LONG" if stop < entry else "SHORT"),
        "entry": entry, "stop": stop, "target": _number(row.get("target")),
        "stage": stage, "reason": reason, "debate_status": row.get("debate_status"), "verdict": row.get("recommendation"),
        "policy_a": row.get("policy_a"), "policy_b": row.get("policy_b"), "policy_c": row.get("policy_c"),
        "what_if_r": r, "what_if_state": state, "what_if_exit": provisional,
    }


def funnel_counts(views: list[dict[str, Any]]) -> dict[str, Any]:
    """The funnel's counts over ``funnel_row`` views."""
    proposals = Counter(v["stage"] for v in views if v["stage"] in (PROPOSED, APPROVED, REJECTED, EXPIRED))
    return {
        "candidates": len(views),
        "gate_passed": sum(bool(v["debate_status"]) and v["debate_status"] != GATE_REFUSED for v in views),
        "gate_refused": sum(v["debate_status"] == GATE_REFUSED for v in views),
        "verdicts": dict(Counter(str(v["verdict"]).upper() for v in views if v["verdict"])),
        "proposals": {"PENDING": proposals[PROPOSED], "APPROVED": proposals[APPROVED],
                      "REJECTED": proposals[REJECTED], "EXPIRED": proposals[EXPIRED]},
        "approved": proposals[APPROVED], "unaccounted": sum(v["stage"] == UNACCOUNTED for v in views),
    }
