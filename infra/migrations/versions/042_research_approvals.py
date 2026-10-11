"""research approvals: a separate approval step for the research loop

Revision ID: 042
Revises: 041
Create Date: 2026-10-11 00:00:00.000000

A drafted strategy and a queued research idea wait here for the operator before anything runs: a draft is
not saved or backtested until ``APPROVED``, and a queued idea is not run until ``APPROVED``. Separate from
``ai_approvals`` on purpose: that table is bound to a mandate and to execution commands, which research
work is not. Adds the ``PENDING_APPROVAL`` report status a report carries while its draft awaits approval.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE research_approvals (
            id UUID PRIMARY KEY,
            kind VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
            summary TEXT NOT NULL,
            payload JSONB NOT NULL,
            requested_by VARCHAR(64) NOT NULL,
            decided_by VARCHAR(64),
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_research_approval_kind CHECK (kind IN ('DRAFT_STRATEGY', 'QUEUE_RESEARCH')),
            CONSTRAINT chk_research_approval_status CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED'))
        )
    """)
    op.execute("CREATE INDEX ix_research_approvals_status ON research_approvals (status, created_at DESC)")
    op.execute("ALTER TABLE research_reports DROP CONSTRAINT chk_research_status")
    op.execute("""
        ALTER TABLE research_reports ADD CONSTRAINT chk_research_status
        CHECK (status IN ('PROMOTE', 'REVISE', 'REJECT', 'FAILED', 'PENDING_APPROVAL'))
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON research_approvals TO cryptonite_api;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Reports still waiting for approval cannot keep a status the old constraint refuses
    op.execute("UPDATE research_reports SET status = 'FAILED', reason = 'approval step removed' "
               "WHERE status = 'PENDING_APPROVAL'")
    op.execute("ALTER TABLE research_reports DROP CONSTRAINT chk_research_status")
    op.execute("""
        ALTER TABLE research_reports ADD CONSTRAINT chk_research_status
        CHECK (status IN ('PROMOTE', 'REVISE', 'REJECT', 'FAILED'))
    """)
    op.execute("DROP TABLE IF EXISTS research_approvals")
