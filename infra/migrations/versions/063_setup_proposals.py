"""H13b: setup_proposals, a scanner setup the debate said HOLD on, for the operator to decide

Revision ID: 063
Revises: 062
Create Date: 2026-11-04 12:00:00.000000

A scanner debate that ends in HOLD on a setup policy A would take creates one proposal (``services/debate_worker/
proposals.py``). A proposal never executes by itself: it has no timeout path and unanswered it simply expires. Only the
operator's approve (``routes/proposals.py``) queues an order, through the Brain card's checks, as an ``ai_approvals``
row of source ``scanner_proposal``. The debate's run and its scoring stay as they were: the run keeps NO_TRADE.

``agent_safe_trade_timeline`` needs no change: ``entry_source`` reads the approval's source as it is
(``scanner_proposal``), and its debate columns stay NULL, as they do for a Brain card with a debate link (they read
only a source ``debate`` approval, whose source_ref is the run). ``close_reason`` reads the closing command, and a
proposal is always an entry.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "063"
down_revision: str | None = "062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE setup_proposals (
            id UUID PRIMARY KEY,
            candidate_id UUID NOT NULL UNIQUE REFERENCES signal_candidates(id) ON DELETE CASCADE,
            run_id UUID NOT NULL,
            mandate_id UUID NOT NULL,
            instrument_id TEXT NOT NULL,
            side TEXT NOT NULL,
            card JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            refusal TEXT,
            command_id UUID,
            approval_id UUID,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            decided_at TIMESTAMPTZ,
            decided_by TEXT,
            CONSTRAINT chk_setup_proposals_status CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
            CONSTRAINT chk_setup_proposals_side CHECK (side IN ('BUY', 'SELL'))
        )
    """)
    op.execute("CREATE INDEX ix_setup_proposals_status_expiry ON setup_proposals (status, expires_at)")
    op.execute("CREATE INDEX ix_setup_proposals_created_at ON setup_proposals (created_at DESC)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON setup_proposals TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON setup_proposals TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE setup_proposals")
