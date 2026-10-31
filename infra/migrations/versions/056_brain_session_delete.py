"""brain session delete: the Brain may delete an operator's conversations for good

Revision ID: 056
Revises: 055
Create Date: 2026-10-31 18:00:00.000000

H2 / O1. Deleting a conversation is permanent. The Brain store runs as ``cryptonite_ai_telemetry`` (SELECT/INSERT/UPDATE
since 024); this grants DELETE on only the three conversation tables. Messages and summaries cascade from
``ai_sessions`` (024 foreign keys are ON DELETE CASCADE).
"""
from collections.abc import Sequence

from alembic import op

revision: str = "056"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = "ai_sessions, ai_messages, ai_session_summaries"


def upgrade() -> None:
    op.execute(f"GRANT DELETE ON TABLE {TABLES} TO cryptonite_ai_telemetry")


def downgrade() -> None:
    op.execute(f"REVOKE DELETE ON TABLE {TABLES} FROM cryptonite_ai_telemetry")
