"""research validation: walk-forward folds, deflated Sharpe, holdout and robustness on research reports

Revision ID: 032
Revises: 031
Create Date: 2026-10-05 02:00:00.000000

Remediation Phase 5 (R5.1-R5.5). The research loop now scores 4 anchored walk-forward folds, the
Deflated Sharpe Ratio over every trial tested on the instrument/timeframe, a +-10% parameter
robustness check and, for PROMOTE candidates only, a final holdout window. They are queryable columns
next to the full report (which keeps them too). Nullable, no backfill: earlier reports predate them.
Grants on research_reports are table-level (031), so the new columns need none.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE research_reports
            ADD COLUMN holdout JSONB,
            ADD COLUMN dsr NUMERIC(8, 6),
            ADD COLUMN folds JSONB,
            ADD COLUMN robustness JSONB
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE research_reports
            DROP COLUMN IF EXISTS robustness,
            DROP COLUMN IF EXISTS folds,
            DROP COLUMN IF EXISTS dsr,
            DROP COLUMN IF EXISTS holdout
    """)
