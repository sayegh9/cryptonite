"""H13e: the debate workers can write shadow_debate_verdicts

Revision ID: 066
Revises: 065
Create Date: 2026-11-04 20:00:00.000000

Migration 065 granted ``cryptonite_api`` SELECT only, but the debate workers connect as that role, so every shadow answer
was lost and the daily spend cap, which reads this table, saw nothing. ``cryptonite_agent_ro`` still gets no access: live
models must not read shadow answers.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "066"
down_revision: str | None = "065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT INSERT ON shadow_debate_verdicts TO cryptonite_api;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                REVOKE INSERT ON shadow_debate_verdicts FROM cryptonite_api;
            END IF;
        END $$;
    """)
