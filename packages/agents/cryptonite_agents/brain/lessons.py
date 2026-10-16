"""The past lessons an order card shows (WS-3B): what the learner wrote after earlier debate decisions.

The Brain reads ``agent_safe_reflections`` as the read-only agent role, through ``tools._readonly_rows`` like every
other model-callable read. The lesson text was written by a model: the card shows it as quoted data, clipped, and it
never enters the Brain's prompt (``describe_widget`` leaves it out).
"""

import logging
from typing import Any

from cryptonite_agents import tools

logger = logging.getLogger("cryptonite.agents.brain.lessons")

CARD_LESSONS_LIMIT = 3  # at most this many lessons on one card
LESSON_WINDOW_DAYS = 90  # only reflections this recent are shown

# The reflection writer stores alpha as a fraction (0.012 is 1.2%), so the card converts it to a percent
_LESSONS_SQL = f"""
    SELECT lessons_learned, outcome, alpha_vs_benchmark, created_at
    FROM agent_safe_reflections
    WHERE instrument_id = :instrument_id
      AND created_at >= now() - interval '{LESSON_WINDOW_DAYS} days'
    ORDER BY abs(alpha_vs_benchmark) DESC NULLS LAST, created_at DESC
    LIMIT {CARD_LESSONS_LIMIT}
"""


def card_lessons(instrument_id: str) -> list[dict[str, Any]]:
    """The lessons on the order card of ``instrument_id``, largest alpha first. Never raises: any error gives []."""
    try:
        rows = tools._readonly_rows(_LESSONS_SQL, {"instrument_id": instrument_id})
    except Exception as e:
        logger.warning("Could not read the past lessons for %s; the card shows none: %s", instrument_id, e)
        return []
    return [_lesson(row) for row in rows]


def _lesson(row: dict[str, Any]) -> dict[str, Any]:
    alpha = row["alpha_vs_benchmark"]
    created = row["created_at"]
    return {
        "lesson_text": tools._clip(str(row["lessons_learned"] or "")),
        "outcome": None if row["outcome"] is None else str(row["outcome"]),
        "alpha_pct": None if alpha is None else round(float(alpha) * 100, 2),
        "created_at": created.isoformat(timespec="seconds"),
    }
