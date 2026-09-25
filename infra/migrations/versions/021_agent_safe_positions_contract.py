"""agent_safe_positions contract: open positions only, signed size, PnL and weight

Revision ID: 021
Revises: 020
Create Date: 2026-09-25 12:00:00.000000

One view contract for every agent consumer (debate_worker reads `size`, exit_monitor reads
`unrealized_pnl_pct`). The 014 columns keep their names and order; the new ones are appended,
so CREATE OR REPLACE works on upgrade. Percent columns are 0-100 (like allocation_pct), not fractions.
"""
from collections.abc import Sequence

from alembic import op

revision: str = '021'
down_revision: str | None = '020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE VIEW agent_safe_positions AS
    SELECT
        p.instrument_id,
        p.quantity,
        p.entry_price AS average_entry_price,
        p.opened_at AS created_at,
        COALESCE(p.closed_at, p.opened_at) AS updated_at,
        p.side,
        CASE WHEN p.side = 'SHORT' THEN -p.quantity ELSE p.quantity END AS size,
        p.current_price,
        p.unrealized_pnl,
        CASE WHEN p.entry_price > 0 AND p.quantity > 0
             THEN p.unrealized_pnl / (p.entry_price * p.quantity) * 100 END AS unrealized_pnl_pct,
        CASE WHEN a.total_equity > 0
             THEN p.quantity * COALESCE(NULLIF(p.current_price, 0), p.entry_price) / a.total_equity * 100 END AS weight_pct,
        p.leverage,
        p.margin_mode
    FROM paper_positions p
    JOIN paper_accounts a ON a.account_id = p.account_id
    WHERE p.status = 'OPEN';
    """)
    op.execute("GRANT SELECT ON agent_safe_positions TO cryptonite_agent_ro;")


def downgrade() -> None:
    # CREATE OR REPLACE cannot drop columns, so restore the 014 view from scratch.
    op.execute("DROP VIEW IF EXISTS agent_safe_positions;")
    op.execute("""
    CREATE VIEW agent_safe_positions AS
    SELECT
        instrument_id,
        quantity,
        entry_price AS average_entry_price,
        opened_at AS created_at,
        COALESCE(closed_at, opened_at) AS updated_at
    FROM paper_positions;
    """)
    op.execute("GRANT SELECT ON agent_safe_positions TO cryptonite_agent_ro;")
