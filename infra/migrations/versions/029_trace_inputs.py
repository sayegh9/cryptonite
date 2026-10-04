"""trace inputs: keep the prompt of small distillable roles for future distillation

Revision ID: 029
Revises: 028
Create Date: 2026-10-04 14:00:00.000000

AI-first blueprint A7.4. Distillation needs (input, output, outcome) triples, but traces kept only the
output: the triage brief lived in Redis with a TTL. ``ai_decision_traces.input`` holds the prompt for
the roles listed in ``gateway.LOG_INPUT_ROLES`` (triage, tagging, summarize: market briefs, headlines
and chat summaries the platform already stores); NULL for every other role.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_decision_traces ADD COLUMN input TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE ai_decision_traces DROP COLUMN IF EXISTS input")
