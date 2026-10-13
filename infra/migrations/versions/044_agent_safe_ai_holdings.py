"""agent_safe_ai_holdings: what the AI holds per instrument, for the Brain's SELL check

Revision ID: 044
Revises: 042
Create Date: 2026-10-13 00:00:00.000000

The Brain refuses a SELL card for something no AI mandate holds (spot cannot be shorted). It reads that holding as
``cryptonite_agent_ro``, which may read only the ``agent_safe_*`` views (migration 014), not ``paper_orders``. This view
gives the largest net spot quantity any AI mandate instance holds of each instrument, and nothing else: no mandate,
account or order detail. The view runs as its owner, so the agent role needs no grant on ``paper_orders``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "044"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW agent_safe_ai_holdings AS
        SELECT instrument_id, GREATEST(MAX(net), 0) AS held_quantity
        FROM (
            SELECT instance_id, instrument_id,
                   SUM(CASE WHEN side = 'BUY' THEN filled_qty ELSE -filled_qty END) AS net
            FROM paper_orders
            WHERE instance_id LIKE 'AI!_MANDATE!_%' ESCAPE '!' AND filled_qty > 0
            GROUP BY instance_id, instrument_id
        ) per_mandate
        GROUP BY instrument_id
    """)
    op.execute("GRANT SELECT ON agent_safe_ai_holdings TO cryptonite_agent_ro")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS agent_safe_ai_holdings")
