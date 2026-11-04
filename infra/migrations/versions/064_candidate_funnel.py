"""H13c: where a scanner setup stopped, and what it would have done

Revision ID: 064
Revises: 063
Create Date: 2026-11-04 18:00:00.000000

``candidate_funnel_stops`` holds the one stage nothing else records: why a HOLD got no proposal (it was only logged).
Every other stage is derived at read time (``cryptonite_schemas.funnel``). ``candidate_provisional_outcomes`` is the
learner's what-if for a candidate whose ``net_r_live`` label is not due yet; it is a model figure, before costs.

The API role gains a read of the outbox row's state (not its ``fields``): a candidate without a row was not fresh enough
to queue, and only the outbox tells that apart from a queued candidate no debate ever picked up.

Scanner HOLDs from before this revision get a ``BEFORE_TRACKING`` row: they had no way to record a reason, and without
it the alarm that looks for a HOLD with neither a proposal nor a reason would fire on all of them at deploy.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "064"
down_revision: str | None = "063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE candidate_funnel_stops (
            candidate_id UUID PRIMARY KEY REFERENCES signal_candidates(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            reason TEXT NOT NULL,
            detail JSONB,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE candidate_provisional_outcomes (
            candidate_id UUID PRIMARY KEY REFERENCES signal_candidates(id) ON DELETE CASCADE,
            gross_r NUMERIC,
            state TEXT NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        INSERT INTO candidate_funnel_stops (candidate_id, stage, reason, detail)
        SELECT r.candidate_id, 'PROPOSAL_SKIPPED', 'BEFORE_TRACKING', jsonb_build_object('run_id', r.id)
        FROM debate_runs r
        JOIN signal_candidates c ON c.id = r.candidate_id
        LEFT JOIN setup_proposals p ON p.candidate_id = r.candidate_id
        WHERE r.trigger_source = 'scanner' AND p.id IS NULL AND (
            SELECT d.recommendation FROM portfolio_decisions d
            WHERE d.run_id = r.id ORDER BY d.created_at DESC, d.id DESC LIMIT 1) = 'HOLD'
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON candidate_funnel_stops, candidate_provisional_outcomes TO cryptonite_api;
                GRANT SELECT (candidate_id, status, last_error) ON scanner_debate_outbox TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON candidate_funnel_stops, candidate_provisional_outcomes TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                REVOKE SELECT (candidate_id, status, last_error) ON scanner_debate_outbox FROM cryptonite_api;
            END IF;
        END $$;
    """)
    op.execute("DROP TABLE candidate_provisional_outcomes")
    op.execute("DROP TABLE candidate_funnel_stops")
