"""H6: the Brain reads scan spec slugs (scan_specs.slug via scan_spec_versions)

Revision ID: 059
Revises: 058
Create Date: 2026-11-02 12:00:00.000000

H6 item 3. Global scanner specs write neither ``preset_slug`` nor ``spec_slug`` into ``signal_candidates.metrics``, so
the Brain's ``_recent_scanner_setups`` dropped them. It now joins ``scan_spec_versions`` and ``scan_specs`` for the
slug, as the read-only agent role, which migration 014 left without access to either table. Column grants only: the
join keys and the slug, never a spec's rules or universe.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "059"
down_revision: str | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT (id, spec_id) ON scan_spec_versions TO cryptonite_agent_ro;
                GRANT SELECT (id, slug) ON scan_specs TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                REVOKE SELECT (id, spec_id) ON scan_spec_versions FROM cryptonite_agent_ro;
                REVOKE SELECT (id, slug) ON scan_specs FROM cryptonite_agent_ro;
            END IF;
        END $$;
    """)
