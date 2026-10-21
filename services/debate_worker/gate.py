"""Pre-debate gate (AI-first blueprint §6, A2.1 + A2.3): spend nothing on debates that cannot matter.

Automatic (scanner) triggers pass, in order, before any model is called:
  0. one decision per candidate id    -> the scanner's enqueue is at least once (D1): a delivery whose
     durable run (``debate_runs.candidate_id``) already exists is dropped; a retry before any run exists
     passes again (nothing durable was written, so a transient failure cannot poison the retry)
  1. kill switch / daily-loss latch   -> a new entry could not be placed anyway
  2. position at the concentration cap -> a new entry could not be sized
  3. stale candidate                  -> the setup is old news
  4. cooldown per instrument+timeframe -> one debate per setup window. The key holds the candidate's own
     token: a crash or cancellation after this point leaves a window the same candidate's redelivery
     still passes (no durable row yet), while other candidates wait it out
  4b. a candidate that would route to a perps mandate (Phase B: no ACTIVE spot mandate allows the instrument, one
     perps mandate allows its perpetual) -> only from a closed 1h bar (``PERP_MANDATE_NOT_1H``), and at most
     ``debate.perp_mandate_daily_cap`` such debates per perps mandate per UTC day (``PERP_MANDATE_DAILY_CAP``; a slot
     is taken when the gate lets the debate run, and not given back if the debate fails)
  5. hard triage rules in code (R3.1, ``cryptonite_scanner.brief.hard_rejects`` on the scanner's
     structured brief): reward-to-risk < 1.5, the two highest timeframes both trending down, chop
     -> ``TRIAGE_RULE_<NAME>`` with no model call. Missing data never rejects: it falls through to 6.
  6. triage (Flash-Lite, one cheap call on the market brief) -> worth a full debate?
Brain-started debates (``brain_on_demand``, WS-1E) run only the account checks, kill switch, daily loss and
position cap, then pass as ``BRAIN_TRIGGER``: no triage call and no dedupe, stale, brief or cooldown check. The
API's per-day cap bounds how many there are. Debates staged by an AI tool (the Brain and the research chat,
``trigger_source = "brain_on_demand"``) pass the account safety checks and count toward the daily Brain debate cap.
Only debates the operator starts from the dashboard skip the gate: that spend is an explicit choice.
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
from cryptonite_schemas.mandate import is_perp_instrument
from cryptonite_schemas.runtime_settings import PERP_DEBATE_CAP_KEY, RUNTIME_BOUNDS, read_number
from mandate_routing import resolve_route

logger = logging.getLogger("debate_worker.gate")

PAPER_ACCOUNT_ID = "PAPER-ACCOUNT-001"
AUTOMATIC_SOURCES = frozenset({"scanner"})
BRAIN_SOURCES = frozenset({"brain_on_demand"})
MAX_POSITION_PCT = 25.0
MIN_ENTRY_PCT = 1.0  # the dispatcher's floor: less than this headroom means no entry can be placed
COOLDOWN_S = 30 * 60
MIN_STALE_S = 15 * 60
# D1: the scanner's debate outbox can publish a candidate twice (a crash between XADD and marking its row
# published). Dedupe is the durable run row (debate_runs.candidate_id, partial unique index in migration
# 039), not a key set before any work: a failure before the run leaves nothing behind, so a PEL retry
# (F-H6) of the same message passes the gate again, while a delivery whose run exists is dropped.
TIMEFRAME_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
PERP_DEBATE_TIMEFRAME = "1h"  # scanner debates routed to a perps mandate come only from closed 1h bars
PERP_CAP_TTL_S = 2 * 86400


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    triage: TriageDecision | None = None
    triage_trace_id: Any = None
    cooldown_key: str | None = None  # released by the caller if the debate it allowed fails
    cooldown_token: str | None = None  # the caller's own token: release only while it still holds the key


# Atomic compare-and-delete: read and delete in one script, so between the two no TTL expiry or other
# candidate's SET can slip in and leave the stale owner deleting a window it no longer holds.
_RELEASE_COOLDOWN_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def release_cooldown(redis, key: str | None, token: str | None) -> None:
    """Compare-and-delete the cooldown: only the attempt whose token still holds it may release it, so a
    failing attempt never deletes a window that has since become another candidate's."""
    if not key or token is None:
        return
    try:
        await redis.eval(_RELEASE_COOLDOWN_LUA, 1, key, token)
    except Exception:  # a failed release only costs the cooldown window, never the retry itself
        logger.warning("Cooldown %s not released", key)


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


async def _account_refusal(conn, instrument_id: str) -> str | None:
    """The account-level checks that come before anything else for a debate: the reason it is refused, or None.
    Scanner candidates run them after their own dedupe; Brain-started debates run only these."""
    latched = await conn.fetchval(
        "SELECT is_latched FROM execution_kill_switch WHERE environment = 'PAPER' AND account_id = $1",
        PAPER_ACCOUNT_ID,
    )
    if latched:
        return "KILL_SWITCH_LATCHED"
    if await conn.fetchval("SELECT daily_loss_latched FROM paper_accounts WHERE account_id = $1", PAPER_ACCOUNT_ID):
        return "DAILY_LOSS_LATCHED"
    weight = await conn.fetchval(
        "SELECT COALESCE(SUM(weight_pct), 0) FROM agent_safe_positions WHERE instrument_id = $1", instrument_id
    )
    if float(weight or 0) > MAX_POSITION_PCT - MIN_ENTRY_PCT:
        return "POSITION_AT_CAP"
    return None


def perp_cap_key(mandate_id: Any, now_s: float) -> str:
    day = datetime.datetime.fromtimestamp(now_s, datetime.UTC).date().isoformat()
    return f"debate:perp_cap:{mandate_id}:{day}"


async def perp_debate_cap(conn) -> int:
    """``debate.perp_mandate_daily_cap`` as stored (a value that does not parse or is out of bounds: the default)."""
    raw = await conn.fetchval("SELECT value FROM system_settings WHERE key = $1", PERP_DEBATE_CAP_KEY)
    bound = RUNTIME_BOUNDS[PERP_DEBATE_CAP_KEY]
    value, problem = read_number(raw, bound, bound.default)
    if problem:
        logger.warning("%s: %s; the default %g applies", PERP_DEBATE_CAP_KEY, problem, bound.default)
    return int(value)


async def _perp_route(conn, instrument_id: str):
    """The perps mandate a scanner debate on ``instrument_id`` would be routed to, or None (spot, or no mandate)."""
    mandate, routed = await resolve_route(conn, instrument_id)
    return mandate if mandate is not None and is_perp_instrument(routed) else None


async def _reserve_perp_slot(redis, key: str, cap: int) -> bool:
    """Take one of the day's slots atomically (several workers): INCR, and give it back when it went over the cap."""
    used = await redis.incr(key)
    await redis.expire(key, PERP_CAP_TTL_S)
    if used > cap:
        await redis.decr(key)
        return False
    return True


async def _evaluate(pg_pool, redis, payload, gateway, brief, now_s, brief_data=None) -> GateDecision:
    source = payload.get("trigger_source", "scanner")
    if source in BRAIN_SOURCES:
        async with pg_pool.acquire() as conn:
            refusal = await _account_refusal(conn, payload["instrument_id"])
        return GateDecision(False, refusal) if refusal else GateDecision(True, "BRAIN_TRIGGER")
    if source not in AUTOMATIC_SOURCES:
        return GateDecision(True, "OPERATOR_TRIGGER")

    instrument_id = payload["instrument_id"]
    candidate = json.loads(payload.get("candidate_payload") or "{}")
    timeframe = candidate.get("timeframe", "?")
    now_s = now_s if now_s is not None else datetime.datetime.now(datetime.UTC).timestamp()

    cid = None
    if candidate.get("candidate_id"):
        try:
            cid = uuid.UUID(str(candidate["candidate_id"]))
        except (TypeError, ValueError):
            cid = None  # a malformed id matches nothing: it can never dedupe another candidate

    async with pg_pool.acquire() as conn:
        if cid is not None and await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM debate_runs WHERE candidate_id = $1)", cid
        ):
            return GateDecision(False, "DUPLICATE_CANDIDATE")
        if refusal := await _account_refusal(conn, instrument_id):
            return GateDecision(False, refusal)

    close_ns = candidate.get("bar_close_inclusive_ns")
    if close_ns:
        max_age = max(MIN_STALE_S, 2 * TIMEFRAME_S.get(timeframe, 0))
        if now_s - int(close_ns) / 1e9 > max_age:
            return GateDecision(False, "STALE_CANDIDATE")

    if not brief:  # no market data: a debate would argue from nothing
        return GateDecision(False, "NO_BRIEF")

    # Phase B cadence and cost: a debate for a perps mandate only from a closed 1h bar, and a daily cap per mandate
    async with pg_pool.acquire() as conn:
        perp_mandate = await _perp_route(conn, instrument_id)
        cap = await perp_debate_cap(conn) if perp_mandate is not None else None
    slot_key = None
    if perp_mandate is not None:
        if timeframe != PERP_DEBATE_TIMEFRAME:
            return GateDecision(False, "PERP_MANDATE_NOT_1H")
        slot_key = perp_cap_key(perp_mandate.id, now_s)
        if int(await redis.get(slot_key) or 0) >= cap:
            return GateDecision(False, "PERP_MANDATE_DAILY_CAP")

    cooldown_key = f"debate:cooldown:{instrument_id}:{timeframe}"
    # Candidate-aware: the value is the candidate's token, so a crash or cancellation after this point
    # leaves a window that its OWN redelivery still passes (no durable row exists yet), while every other
    # candidate waits it out. Unidentified payloads keep the legacy opaque value and the old semantics.
    cooldown_token = f"cid:{cid}" if cid else "1"
    held = await redis.get(cooldown_key)
    if held is None:
        if not await redis.set(cooldown_key, cooldown_token, nx=True, ex=COOLDOWN_S):
            return GateDecision(False, "COOLDOWN")  # lost the set to a concurrent candidate
    elif cid is None or held != cooldown_token:
        return GateDecision(False, "COOLDOWN")

    # Like a model rejection, a rule rejection keeps the cooldown: one decision per setup window
    if (rule := _rule_rejection(brief_data)) is not None:
        return GateDecision(False, rule)

    if gateway is None:
        if slot_key is not None and not await _reserve_perp_slot(redis, slot_key, cap):
            await release_cooldown(redis, cooldown_key, cooldown_token)
            return GateDecision(False, "PERP_MANDATE_DAILY_CAP")
        return GateDecision(True, "NO_TRIAGE_GATEWAY", cooldown_key=cooldown_key, cooldown_token=cooldown_token)
    try:
        # The setup's decision_trace_id as the trace parent links triage (and its F2 shadow) to outcomes
        result = await TRIAGE.run(gateway, brief, parent_trace_id=_trace_uuid(payload))
    except Exception as e:  # gateway, budget or anything else: no triage, no paid debate; retry next candidate
        logger.warning("Triage unavailable for %s: %s", instrument_id, e)
        await release_cooldown(redis, cooldown_key, cooldown_token)
        return GateDecision(False, "TRIAGE_UNAVAILABLE")
    if not result.output.worth_debate:
        return GateDecision(False, "TRIAGE_REJECTED", result.output, result.trace_id)
    if slot_key is not None and not await _reserve_perp_slot(redis, slot_key, cap):
        await release_cooldown(redis, cooldown_key, cooldown_token)
        return GateDecision(False, "PERP_MANDATE_DAILY_CAP", result.output, result.trace_id)
    return GateDecision(True, "TRIAGE_ACCEPTED", result.output, result.trace_id, cooldown_key, cooldown_token)
