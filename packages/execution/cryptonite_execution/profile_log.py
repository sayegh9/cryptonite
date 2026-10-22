"""Auto-trade profile decision log (ADR 0011, ``docs/specs/migration-050.md`` §4).

``record_decision`` writes one row to ``ai_profile_decisions`` for each profile-stage decision
(SCAN, GATE, DEBATE, ROUTE, APPROVAL, ORDER, FILL, EXIT, LIFECYCLE, BREAKER, SESSION).

It never raises. A failed write must never block trading, so every failure is logged as a
WARNING and reported as ``False``.
"""

import json
import logging
import uuid
from typing import Any

from cryptonite_schemas.profile import DecisionStage

logger = logging.getLogger(__name__)

_INSERT_SQL = (
    "INSERT INTO ai_profile_decisions (mandate_id, instrument_id, stage, outcome, reason, detail, run_id, command_id) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)"
)
_OUTCOME_MAX = 40
_INSTRUMENT_MAX = 64


async def record_decision(conn: Any, mandate_id, stage, outcome: str, reason: str | None = None, *,
                          instrument_id: str | None = None, run_id=None, command_id=None, **detail) -> bool:
    """Insert one decision row. Return True on success, False on any failure (never raises)."""
    try:
        stage_value = DecisionStage(stage).value
        await conn.execute(
            _INSERT_SQL,
            uuid.UUID(str(mandate_id)),
            instrument_id[:_INSTRUMENT_MAX] if instrument_id is not None else None,
            stage_value,
            outcome[:_OUTCOME_MAX],
            reason,
            json.dumps(detail, default=str),
            uuid.UUID(str(run_id)) if run_id is not None else None,
            uuid.UUID(str(command_id)) if command_id is not None else None,
        )
    except Exception as e:
        logger.warning("profile decision not recorded: %s", e)
        return False
    return True
