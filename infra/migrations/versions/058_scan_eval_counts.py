"""H5: preset firing-rate counts (scan_eval_counts)

Revision ID: 058
Revises: 057
Create Date: 2026-11-01 20:00:00.000000

H5 item 2. The scanner re-evaluates a spec's whole history on every closed bar and kept no count of what the newest
bar came to. ``scan_eval_counts`` holds one counter per hour bucket (of the bar close), instrument, timeframe, preset,
side and outcome, upserted ``n = n + 1`` for the newest closed bar of each committed-bar evaluation only:
``MATCHED_FRESH``, ``REJECTED_RR`` (reward below 1.5x the risk), ``STALE`` (the match is older than one bar) or
``NO_TRIGGER``. The scanner (``cryptonite_scanner``) writes; the API reads; the Brain reads it only through the
``agent_safe_scan_eval_counts`` security-barrier view. No trading rule reads it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "058"
down_revision: str | None = "057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEW = "agent_safe_scan_eval_counts"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE scan_eval_counts (
            bucket_start TIMESTAMPTZ NOT NULL,
            instrument_id TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            preset_slug TEXT NOT NULL,
            side TEXT NOT NULL,
            outcome TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT pk_scan_eval_counts
                PRIMARY KEY (bucket_start, instrument_id, timeframe, preset_slug, side, outcome),
            CONSTRAINT chk_scan_eval_counts_outcome
                CHECK (outcome IN ('MATCHED_FRESH', 'REJECTED_RR', 'STALE', 'NO_TRIGGER')),
            CONSTRAINT chk_scan_eval_counts_side CHECK (side IN ('LONG', 'SHORT'))
        )
    """)
    op.execute(f"CREATE VIEW {VIEW} WITH (security_barrier) AS SELECT * FROM scan_eval_counts")
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
                GRANT SELECT, INSERT, UPDATE ON scan_eval_counts TO cryptonite_scanner;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
                GRANT SELECT ON scan_eval_counts TO cryptonite_api;
                GRANT SELECT ON {VIEW} TO cryptonite_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
                GRANT SELECT ON {VIEW} TO cryptonite_agent_ro;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute(f"DROP VIEW IF EXISTS {VIEW}")
    op.execute("DROP TABLE IF EXISTS scan_eval_counts")
