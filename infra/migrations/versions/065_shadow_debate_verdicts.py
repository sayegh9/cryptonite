"""H13d: shadow_debate_verdicts, the answer of a reworked lite-debate prompt on the setups the live prompt saw

Revision ID: 065
Revises: 064
Create Date: 2026-11-04 18:00:00.000000

One row per candidate, written after the live debate has finished (``services/debate_worker/shadow_debate.py``). Nothing
live reads it; only ``scripts/shadow_prompt_report.py`` does (docs/adr/0012). It is its own table because the CHECK on
``policy_decisions.policy`` and Gate P read policies A, B and C only.

``cryptonite_agent_ro`` gets no grant, unlike 063: that is the role of the models' SQL tool (``query_postgres_readonly``),
and a debate or Brain model that could read these rows would break the shadow's isolation.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "065"
down_revision: str | None = "064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE shadow_debate_verdicts (
            candidate_id UUID PRIMARY KEY REFERENCES signal_candidates(id) ON DELETE CASCADE,
            run_id UUID NOT NULL,
            prompt_version TEXT NOT NULL,
            state TEXT NOT NULL,
            recommendation TEXT,
            conviction NUMERIC,
            reasoning TEXT,
            model TEXT,
            cost_usd NUMERIC,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_shadow_debate_verdicts_state CHECK (state IN ('OK', 'ERROR', 'TIMEOUT', 'SKIPPED_BUDGET'))
        )
    """)
    op.execute("CREATE INDEX ix_shadow_debate_verdicts_created_at ON shadow_debate_verdicts (created_at DESC)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT ON shadow_debate_verdicts TO cryptonite_api;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE shadow_debate_verdicts")
