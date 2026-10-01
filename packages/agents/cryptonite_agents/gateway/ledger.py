"""Cost ledger and decision-trace writes (blueprint §4.1 trace, F6, F11).

One ``llm_cost_ledger`` row and one ``ai_decision_traces`` row per gateway call. Writes are
best-effort: a telemetry outage is logged and never fails the model call.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

logger = logging.getLogger("cryptonite.agents.gateway.ledger")


@dataclass
class CallRecord:
    role: str
    model: str
    status: str  # OK | ERROR | DENIED | BUDGET_BLOCKED
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_trace_id: uuid.UUID | None = None
    session_id: str | None = None
    mandate_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    output: Any = None
    error: str | None = None

    @property
    def provider(self) -> str:
        return self.model.split(":", 1)[0]


async def write_call(pool, rec: CallRecord) -> bool:
    """Write the ledger + trace rows; returns False when nothing could be written."""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():  # ledger + trace together or not at all
                await conn.execute(
                    """
                    INSERT INTO llm_cost_ledger (
                        request_id, agent_name, model_name, input_tokens, output_tokens, total_cost_usd,
                        role, provider, cached_input_tokens, thinking_tokens, latency_ms, trace_id, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $2, $7, $8, $9, $10, $11, $12)
                    """,
                    str(rec.trace_id), rec.role, rec.model, rec.input_tokens, rec.output_tokens, rec.cost_usd,
                    rec.provider, rec.cached_input_tokens, rec.thinking_tokens, rec.latency_ms, rec.trace_id, rec.status,
                )
                await conn.execute(
                    """
                    INSERT INTO ai_decision_traces (
                        id, parent_id, role, model, session_id, mandate_id, status,
                        tool_calls, output, error, cost_usd, latency_ms
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, $12)
                    """,
                    rec.trace_id, rec.parent_trace_id, rec.role, rec.model, rec.session_id, rec.mandate_id, rec.status,
                    json.dumps(rec.tool_calls, default=str), json.dumps(_jsonable(rec.output), default=str),
                    rec.error, rec.cost_usd, rec.latency_ms,
                )
        return True
    except Exception as e:
        logger.error("Ledger/trace write failed for %s (%s): %s", rec.role, rec.trace_id, e)
        return False


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
