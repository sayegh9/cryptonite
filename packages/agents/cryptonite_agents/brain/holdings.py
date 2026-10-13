"""What the AI holds of an instrument: the spot quantity its mandates bought and have not sold (ADR 0004).

Read from the ``agent_safe_ai_holdings`` view (migration 044) over ``paper_orders``, which the orchestrator writes inside
the claim transaction of every fill, so the figure is never stale (unlike ``paper_positions``, written later by the event
bridge). The Brain's role may read only agent-safe views, so it cannot read ``paper_orders`` directly. The Brain uses the
figure to refuse a SELL card for something nothing holds: spot cannot be shorted.
"""

from decimal import Decimal
from typing import Any

from cryptonite_schemas.mandate import canonical_instrument


async def ai_spot_holding(db: Any, instrument_id: str) -> Decimal:
    """The largest net spot quantity any AI mandate instance holds of ``instrument_id``. ``db`` is an asyncpg pool or
    connection. Zero when no mandate holds it."""
    held = await db.fetchval(
        "SELECT held_quantity FROM agent_safe_ai_holdings WHERE instrument_id = $1",
        canonical_instrument(instrument_id),
    )
    return Decimal(str(held)) if held is not None else Decimal("0")
