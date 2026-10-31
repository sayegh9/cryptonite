"""Profile timeframe picks (P3b).

Revision ID: 054
Revises: 053

``ai_mandates.timeframes`` holds the bar timeframes a profile scans; empty means the style defaults, so every
existing row keeps today's behaviour. ``strategies`` (migration 025) already holds the preset picks. Grants on
``ai_mandates`` are table-level, so the new column needs none.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "054"
down_revision: str | None = "053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_mandates ADD COLUMN timeframes text[] NOT NULL DEFAULT '{}'::text[]")


def downgrade() -> None:
    op.execute("ALTER TABLE ai_mandates DROP COLUMN timeframes")
