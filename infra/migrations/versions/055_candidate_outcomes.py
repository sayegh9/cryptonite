"""candidate outcomes: did each scanner candidate hit its target or its stop first

Revision ID: 055
Revises: 054
Create Date: 2026-10-31 12:00:00.000000

AI Phase 1 / Track B. One row per signal candidate, written by the learner (``cryptonite_api``
role) once the candidate's hold has elapsed; readable by the read-only agent role.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "055"
down_revision: str | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE candidate_outcomes (
            candidate_id UUID PRIMARY KEY REFERENCES signal_candidates(id) ON DELETE CASCADE,
            direction TEXT,
            entry NUMERIC,
            stop NUMERIC,
            target NUMERIC,
            outcome TEXT NOT NULL,
            realized_r NUMERIC,
            mfe_r NUMERIC,
            mae_r NUMERIC,
            bars_to_exit INTEGER,
            max_hold_hours INTEGER,
            labelled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_candidate_outcome CHECK (outcome IN ('TARGET', 'STOP', 'EXPIRED', 'INVALID', 'NO_DATA'))
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON candidate_outcomes TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON candidate_outcomes TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_outcomes")
