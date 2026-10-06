"""scanner debate outbox: queue a fresh candidate for debate in the candidate's own transaction (D1, F-H2)

Revision ID: 039
Revises: 036
Create Date: 2026-10-06 12:00:00.000000

The scanner used to XADD ``stream:debate_queue`` inside the candidate transaction but outside its
guarantee: a Redis failure was only logged, and a crash after the commit lost the enqueue. Now, for a
candidate whose bar closed within one bar of now, the scanner inserts one row here in the same
transaction as the ``signal_candidates`` row. After the commit it publishes the row itself and marks it
``PUBLISHED``; a relay loop in the scanner publishes any row still ``PENDING`` once ``next_attempt_at``
has passed (lease or backoff), and gives up after a bounded number of attempts (``FAILED``, kept for
inspection). Delivery is at least once; the debate gate drops a second delivery of the same candidate id.

The shared outbox relay (``services/outbox``) is not used: it publishes ``{event_id, event_type, data}``,
not the ``{instrument_id, trigger_source, candidate_payload, brief_key}`` entry the debate worker reads.

``fields`` holds the stream entry without ``brief_key`` (the brief is cached in Redis by the scanner and
looked up at publish time). One row per candidate (primary key), deleted with the candidate.

Grants: the scanner role reads, inserts and updates its own rows; nothing else touches the table.

Note: 037 (B2, PR #24) and 038 (C3, PR #26) are on parallel branches. If they merge first, set
``down_revision = "038"`` here when integrating.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "039"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE scanner_debate_outbox (
        candidate_id UUID PRIMARY KEY REFERENCES signal_candidates (id) ON DELETE CASCADE,
        fields JSONB NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_stream_id VARCHAR(64),
        published_at TIMESTAMPTZ,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT chk_scanner_debate_outbox_status CHECK (status IN ('PENDING', 'PUBLISHED', 'FAILED'))
    )
    """)
    op.execute("CREATE INDEX ix_scanner_debate_outbox_pending ON scanner_debate_outbox (next_attempt_at) "
               "WHERE status = 'PENDING'")
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE scanner_debate_outbox TO cryptonite_scanner;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS scanner_debate_outbox")
