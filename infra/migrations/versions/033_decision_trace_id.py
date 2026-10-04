"""decision trace id: follow one setup from scanner candidate to fill (remediation R7.4)

Revision ID: 033
Revises: 032
Create Date: 2026-10-05 01:00:00.000000

The scanner gives every candidate a ``decision_trace_id`` (UUID) that the debate worker logs through
the gate, triage and debate, stores on ``debate_runs`` and passes in the approval and command
payloads; the orchestrator logs it when it processes the command and fills the order.
``execution_commands.decision_trace_id`` is set at insert only: the API role (``cryptonite_api``) has
INSERT but not UPDATE on that table. Both columns are nullable (operator orders, kill-switch commands
and rows from before this migration have none). Grants on both tables are table-level, so the new
columns need no new grants.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE debate_runs ADD COLUMN decision_trace_id UUID")
    op.execute("ALTER TABLE execution_commands ADD COLUMN decision_trace_id UUID")
    op.execute(
        "CREATE INDEX ix_execution_commands_decision_trace_id ON execution_commands (decision_trace_id) "
        "WHERE decision_trace_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_execution_commands_decision_trace_id")
    op.execute("ALTER TABLE execution_commands DROP COLUMN IF EXISTS decision_trace_id")
    op.execute("ALTER TABLE debate_runs DROP COLUMN IF EXISTS decision_trace_id")
