"""H15 PR B: the paper coverage ledger, and the profiles' preset picks for the Brain's watchlist

Revision ID: 067
Revises: 066
Create Date: 2026-11-06 10:00:00.000000

``paper_coverage_ledger`` records each item the coverage worker added to a paper profile. An item is applied once per
policy version, so one the operator later removes is never added back. ``agent_safe_profile_picks`` lets the read-only
agent role say whether a watchlist item is monitored. The policy itself lives in ``system_settings`` and starts absent,
which reads as disabled.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "067"
down_revision: str | None = "066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE paper_coverage_ledger (
            id BIGSERIAL PRIMARY KEY,
            policy_version INTEGER NOT NULL,
            mandate_id UUID NOT NULL REFERENCES ai_mandates(id),
            item_kind VARCHAR(16) NOT NULL,
            item_value TEXT NOT NULL,
            old_value TEXT,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_paper_coverage_item UNIQUE (policy_version, mandate_id, item_kind, item_value)
        )
    """)
    op.execute("""
        CREATE VIEW agent_safe_profile_picks WITH (security_barrier) AS
        SELECT id, name, status, instruments, direction, style, strategies, timeframes
        FROM ai_mandates WHERE status <> 'ARCHIVED'
    """)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON agent_safe_profile_picks TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS agent_safe_profile_picks")
    op.execute("DROP TABLE IF EXISTS paper_coverage_ledger")
