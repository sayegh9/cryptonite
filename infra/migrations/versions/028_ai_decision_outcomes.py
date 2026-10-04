"""ai decision outcomes: what happened after each portfolio decision, at fixed horizons

Revision ID: 028
Revises: 027
Create Date: 2026-10-04 13:00:00.000000

AI-first blueprint §5 / A7.1 (replaces the F10 "position closed = 1 hour old" heuristic).

``ai_decision_outcomes`` holds, per portfolio decision and horizon (1h, 4h, 24h, 72h), the asset's
return from the decision price to the horizon close, and the best (MFE) and worst (MAE) excursion
in between, all as fractions of the decision price and unsigned (direction is the decision's).
Written by the learner service (``cryptonite_api`` role); readable by the read-only agent role so
the Brain and evals can rank lessons by outcome.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ai_decision_outcomes (
            decision_id UUID NOT NULL REFERENCES portfolio_decisions(id) ON DELETE CASCADE,
            horizon VARCHAR(8) NOT NULL,
            return_pct NUMERIC(14, 8) NOT NULL,
            mfe NUMERIC(14, 8) NOT NULL,
            mae NUMERIC(14, 8) NOT NULL,
            bars INTEGER NOT NULL,
            labelled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (decision_id, horizon),
            CONSTRAINT chk_outcome_horizon CHECK (horizon IN ('1h', '4h', '24h', '72h')),
            CONSTRAINT chk_outcome_excursions CHECK (mfe >= mae)
        )
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT, UPDATE ON ai_decision_outcomes TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON ai_decision_outcomes TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_decision_outcomes")
