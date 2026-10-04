"""research reports: queryable history of research-loop results (LE-6)

Revision ID: 031
Revises: 030
Create Date: 2026-10-04 15:30:00.000000

Research reports were JSON files in ``data/research_reports``. ``research_reports`` keeps one row per
researched idea (status, reason, in-/out-of-sample metrics, cost, the full report) so the digest and
the Brain can query the history. Written through the API (``cryptonite_api``); readable by the
read-only agent role for the Brain's SQL tool. The JSON file stays as the artifact copy.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE research_reports (
            id UUID PRIMARY KEY,
            idea TEXT NOT NULL,
            status VARCHAR(16) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            strategy_slug VARCHAR(128),
            strategy_name VARCHAR(256),
            in_sample JSONB NOT NULL DEFAULT '{}'::jsonb,
            out_of_sample JSONB NOT NULL DEFAULT '{}'::jsonb,
            cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
            report JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT chk_research_status CHECK (status IN ('PROMOTE', 'REVISE', 'REJECT', 'FAILED'))
        )
    """)
    op.execute("CREATE INDEX ix_research_reports_created ON research_reports (created_at DESC)")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT, INSERT ON research_reports TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON research_reports TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS research_reports")
