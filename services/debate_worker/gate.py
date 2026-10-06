"""Pre-debate gate (AI-first blueprint §6, A2.1 + A2.3): spend nothing on debates that cannot matter.

Automatic (scanner) triggers pass, in order, before any model is called:
  0. one decision per candidate id    -> the scanner's enqueue is at least once (D1): a redelivery is dropped
  1. kill switch / daily-loss latch   -> a new entry could not be placed anyway
  2. position at the concentration cap -> a new entry could not be sized
  3. stale candidate                  -> the setup is old news
  4. cooldown per instrument+timeframe -> one debate per setup window
  5. hard triage rules in code (R3.1, ``cryptonite_scanner.brief.hard_rejects`` on the scanner's
     structured brief): reward-to-risk < 1.5, the two highest timeframes both trending down, chop
     -> ``TRIAGE_RULE_<NAME>`` with no model call. Missing data never rejects: it falls through to 6.
  6. triage (Flash-Lite, one cheap call on the market brief) -> worth a full debate?
Operator-triggered debates (dashboard, chat, stage_debate_proposal) skip the gate: that spend is an
explicit choice.
"""

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from cryptonite_agents.debate_fleet import TRIAGE
from cryptonite_agents.gateway import Gateway
from cryptonite_scanner.brief import hard_rejects
from cryptonite_schemas.debate import TriageDecision

logger = logging.getLogger("debate_worker.gate")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
AUTOMATIC_SOURCES = frozenset({"scanner"})
MAX_POSITION_PCT = 25.0
MIN_ENTRY_PCT = 1.0  # the dispatcher's floor: less than this headroom means no entry can be placed
COOLDOWN_S = 30 * 60
MIN_STALE_S = 15 * 60
# D1: the scanner's debate outbox can publish a candidate twice (a crash between XADD and marking its row
# published). Keyed on the candidate id, not the stream message: a future PEL retry of the same message
# (F-H6) must delete this key before re-running the gate.
DEDUPE_TTL_S = 2 * 86400
TIMEFRAME_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    triage: TriageDecision | None = None
    triage_trace_id: Any = None
    cooldown_key: str | None = None  # released by the caller if the debate it allowed fails


async def _count(redis, reason: str) -> None:
    try:
        key = f"debate_gate:{datetime.datetime.now(datetime.UTC):%Y-%m-%d}:{reason}"
        await redis.incr(key)
        await redis.expire(key, 8 * 86400)
    except Exception:
        pass


def decision_trace_id(payload: dict[str, Any]) -> str:
    """R7.4: the scanner's id for this setup (candidate payload), the trigger's own, or a new one."""
    try:
        candidate = json.loads(payload.get("candidate_payload") or "{}")
    except (TypeError, ValueError):
        candidate = {}
    trace = (candidate.get("decision_trace_id") if isinstance(candidate, dict) else None) or payload.get("decision_trace_id")
    try:
        return str(uuid.UUID(str(trace)))
    except (TypeError, ValueError):
        return str(uuid.uuid4())


def _trace_uuid(payload: dict[str, Any]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload["decision_trace_id"])) if payload.get("decision_trace_id") else None
    except (TypeError, ValueError):
        return None


async def pre_debate_gate(pg_pool, redis, payload: dict[str, Any], gateway: Gateway | None,
                          brief: str | None, now_s: float | None = None,
                          brief_data: dict[str, Any] | None = None) -> GateDecision:
    decision = await _evaluate(pg_pool, redis, payload, gateway, brief, now_s, brief_data)
    await _count(redis, decision.reason)
    trace = payload.get("decision_trace_id")
    if not decision.allowed:
        logger.info("Debate skipped for %s: %s (decision_trace_id=%s)", payload.get("instrument_id"), decision.reason, trace)
    else:
        logger.info("Debate allowed for %s: %s (decision_trace_id=%s)", payload.get("instrument_id"), decision.reason, trace)
    return decision


def _rule_rejection(brief_data: dict[str, Any] | None) -> str | None:
    """The first hard triage rule the structured brief hits, or None (also for malformed data)."""
    try:
        hits = hard_rejects(brief_data) if brief_data else []
    except Exception as e:  # a bad payload must not block the model path
        logger.warning("Triage rules skipped: %s", e)
        return None
    return f"TRIAGE_RULE_{hits[0]}" if hits else None


async def _evaluate(pg_pool, redis, payload, gateway, brief, now_s, brief_data=None) -> GateDecision:
    if payload.get("trigger_source", "scanner") not in AUTOMATIC_SOURCES:
        return GateDecision(True, "OPERATOR_TRIGGER")

    instrument_id = payload["instrument_id"]
    candidate = json.loads(payload.get("candidate_payload") or "{}")
    timeframe = candidate.get("timeframe", "?")
    now_s = now_s if now_s is not None else datetime.datetime.now(datetime.UTC).timestamp()

    candidate_id = candidate.get("candidate_id")
    if candidate_id and not await redis.set(f"debate:candidate:{candidate_id}", "1", nx=True, ex=DEDUPE_TTL_S):
        return GateDecision(False, "DUPLICATE_CANDIDATE")

    async with pg_pool.acquire() as conn:
        latched = await conn.fetchval(
            "SELECT is_latched FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
            PAPER_ACCOUNT_ID,
        )
        if latched:
            return GateDecision(False, "KILL_SWITCH_LATCHED")
        if await conn.fetchval("SELECT daily_loss_latched FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID):
            return GateDecision(False, "DAILY_LOSS_LATCHED")
        weight = await conn.fetchval(
            "SELECT COALESCE(SUM(weight_pct), 0) FROM agent_safe_positions WHERE instrument_id = $1", instrument_id
        )
        if float(weight or 0) > MAX_POSITION_PCT - MIN_ENTRY_PCT:
            return GateDecision(False, "POSITION_AT_CAP")

    close_ns = candidate.get("bar_close_inclusive_ns")
    if close_ns:
        max_age = max(MIN_STALE_S, 2 * TIMEFRAME_S.get(timeframe, 0))
        if now_s - int(close_ns) / 1e9 > max_age:
            return GateDecision(False, "STALE_CANDIDATE")

    if not brief:  # no market data: a debate would argue from nothing
        return GateDecision(False, "NO_BRIEF")

    cooldown_key = f"debate:cooldown:{instrument_id}:{timeframe}"
    if not await redis.set(cooldown_key, "1", nx=True, ex=COOLDOWN_S):
        return GateDecision(False, "COOLDOWN")

    # Like a model rejection, a rule rejection keeps the cooldown: one decision per setup window
    if (rule := _rule_rejection(brief_data)) is not None:
        return GateDecision(False, rule)

    if gateway is None:
        return GateDecision(True, "NO_TRIAGE_GATEWAY", cooldown_key=cooldown_key)
    try:
        # The setup's decision_trace_id as the trace parent links triage (and its F2 shadow) to outcomes
        result = await TRIAGE.run(gateway, brief, parent_trace_id=_trace_uuid(payload))
    except Exception as e:  # gateway, budget or anything else: no triage, no paid debate; retry next candidate
        logger.warning("Triage unavailable for %s: %s", instrument_id, e)
        await redis.delete(cooldown_key)
        return GateDecision(False, "TRIAGE_UNAVAILABLE")
    if not result.output.worth_debate:
        return GateDecision(False, "TRIAGE_REJECTED", result.output, result.trace_id)
    return GateDecision(True, "TRIAGE_ACCEPTED", result.output, result.trace_id, cooldown_key)
