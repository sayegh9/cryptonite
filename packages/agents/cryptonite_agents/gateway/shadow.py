"""Shadow mode (remediation Phase 8, F2): a candidate model answers live traffic and is never acted on.

A route with ``shadow_model`` and ``shadow_sample_rate`` > 0 sends a sample of its calls, after the
primary returned, to the shadow model as a fire-and-forget task (``Gateway._maybe_shadow``). The
shadow sees the same prompt, instructions, output contract and history, but only tools that are
READ and have no side effects; it never touches the primary's result, its latency or its errors.
Each sampled call leaves one ``ai_shadow_results`` row (OK, ERROR or SKIPPED with the reason) and,
when a model was called, one ``llm_cost_ledger`` row under the role ``shadow:<role>`` so the
budget guard counts it.

Structured roles record whether the shadow agrees with the primary on the role's key field
(``worth_debate`` for triage, ``recommendation`` / ``conviction_level`` for verdicts). Outcome
scoring happens later, at report time (``GET /api/v1/ai/shadow``).
"""

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cryptonite_agents.gateway.registry import RiskClass, ToolSpec

logger = logging.getLogger("cryptonite.agents.gateway.shadow")

SHADOW_ROLE_PREFIX = "shadow:"
MAX_SHADOW_INFLIGHT = 4  # a burst of sampled calls must not pile up background model calls
# The field a structured role's decision turns on; free-text roles have none (agreement = NULL)
KEY_FIELDS: dict[str, str] = {
    "triage": "worth_debate",
    "debate.lite": "recommendation",
    "debate.verdict": "recommendation",
    "exit.verdict": "recommendation",
    "debate.cro": "conviction_level",
}
_FALLBACK_KEYS = ("worth_debate", "recommendation", "conviction_level")


def shadow_role(role: str) -> str:
    return f"{SHADOW_ROLE_PREFIX}{role}"


def allowed_tool(spec: ToolSpec) -> bool:
    """A shadow may only read: RiskClass.READ and no side effects (never ``stage_debate_proposal``)."""
    return spec.risk is RiskClass.READ and not spec.side_effects


def shadow_tools(tools: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
    return tuple(t for t in tools if allowed_tool(t))


def shadow_guard(spec: ToolSpec) -> str | None:
    """Second line of defence: deny anything that is not a side-effect-free READ tool."""
    if allowed_tool(spec):
        return None
    logger.warning("Shadow tool call denied: %s (%s)", spec.name, spec.risk)
    return "Tool call denied: shadow runs are read-only."


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def key_field(role: str, primary: Any, shadow: Any) -> str | None:
    if role in KEY_FIELDS:
        return KEY_FIELDS[role]
    if isinstance(primary, dict) and isinstance(shadow, dict):
        return next((k for k in _FALLBACK_KEYS if k in primary and k in shadow), None)
    return None


def _norm(v: Any) -> Any:
    return v.strip().upper() if isinstance(v, str) else v


def agreement(role: str, primary: Any, shadow: Any) -> tuple[str | None, bool | None]:
    """(key field, shadow agrees with the primary on it). ``None`` agreement when either side has no
    value for the field (free text, an error) — unknown is not disagreement."""
    primary, shadow = jsonable(primary), jsonable(shadow)
    key = key_field(role, primary, shadow)
    if key is None or not isinstance(primary, dict) or not isinstance(shadow, dict):
        return key, None
    if primary.get(key) is None or shadow.get(key) is None:
        return key, None
    return key, _norm(primary[key]) == _norm(shadow[key])


@dataclass
class ShadowRecord:
    role: str
    primary_model: str
    shadow_model: str
    status: str  # OK | ERROR | SKIPPED
    primary_trace_id: uuid.UUID | None = None
    parent_trace_id: uuid.UUID | None = None
    primary_output: Any = None
    shadow_output: Any = None
    key_field: str | None = None
    agrees: bool | None = None
    primary_latency_ms: int | None = None
    shadow_latency_ms: int | None = None
    primary_cost_usd: Decimal = Decimal("0")
    shadow_cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    thinking_tokens: int = 0
    error: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


async def write_shadow(pool, rec: ShadowRecord) -> bool:
    """One ``ai_shadow_results`` row, plus a ledger row when a model was called. Best effort."""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                if rec.status != "SKIPPED":
                    await conn.execute(
                        """
                        INSERT INTO llm_cost_ledger (
                            request_id, agent_name, model_name, input_tokens, output_tokens, total_cost_usd,
                            role, provider, cached_input_tokens, thinking_tokens, latency_ms, trace_id, status
                        ) VALUES ($1, $2, $3, $4, $5, $6, $2, $7, $8, $9, $10, $11, $12)
                        """,
                        str(rec.id), shadow_role(rec.role), rec.shadow_model, rec.input_tokens, rec.output_tokens,
                        rec.shadow_cost_usd, rec.shadow_model.split(":", 1)[0], rec.cached_input_tokens,
                        rec.thinking_tokens, rec.shadow_latency_ms, rec.id, rec.status,
                    )
                await conn.execute(
                    """
                    INSERT INTO ai_shadow_results (
                        id, role, primary_model, shadow_model, status, primary_trace_id, parent_trace_id,
                        primary_output, shadow_output, key_field, agrees, primary_latency_ms, shadow_latency_ms,
                        primary_cost_usd, shadow_cost_usd, error
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, $12, $13, $14, $15, $16)
                    """,
                    rec.id, rec.role, rec.primary_model, rec.shadow_model, rec.status, rec.primary_trace_id,
                    rec.parent_trace_id, json.dumps(jsonable(rec.primary_output), default=str),
                    json.dumps(jsonable(rec.shadow_output), default=str), rec.key_field, rec.agrees,
                    rec.primary_latency_ms, rec.shadow_latency_ms, rec.primary_cost_usd, rec.shadow_cost_usd,
                    (rec.error or None) and rec.error[:2000],
                )
        return True
    except Exception as e:
        logger.error("Shadow result write failed for %s (%s): %s", rec.role, rec.id, e)
        return False
