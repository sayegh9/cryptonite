"""ai sessions: Brain conversations, messages and compacted summaries

Revision ID: 024
Revises: 023
Create Date: 2026-09-25 22:00:00.000000

AI-first blueprint §4.2 / §5 (A3.1, A3.5).

Roles:
- ``cryptonite_ai_telemetry`` (the gateway's identity, also agent_worker's session store): SELECT,
  INSERT, UPDATE on the three session tables. No DELETE: history is append/annotate only.
- ``cryptonite_api``: SELECT on ``ai_decision_traces`` so the API can serve the Decision Trace drawer.
  The telemetry role keeps INSERT only on traces.
- ``cryptonite_agent_ro``: the identity model-callable SQL runs as. It gets nothing here, and any
  default privilege is revoked explicitly so a prompt can never read another session's history.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '024'
down_revision: str | None = '023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TELEMETRY_ROLE = "cryptonite_ai_telemetry"
TABLES = ("ai_sessions", "ai_messages", "ai_session_summaries")


def upgrade() -> None:
    op.create_table(
        'ai_sessions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('operator_id', sa.String(128), nullable=False),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_ai_sessions_operator_updated', 'ai_sessions', ['operator_id', sa.text('updated_at DESC')])

    op.create_table(
        'ai_messages',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('ai_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False, server_default=''),
        sa.Column('widgets', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name='chk_ai_messages_role'),
    )
    op.create_index('ix_ai_messages_session_id', 'ai_messages', ['session_id', 'id'])

    op.create_table(
        'ai_session_summaries',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('session_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('ai_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        # the newest ai_messages.id folded into this summary; later turns are kept verbatim
        sa.Column('covers_message_id', sa.BigInteger(), nullable=False),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_ai_session_summaries_session_id', 'ai_session_summaries', ['session_id', 'id'])

    op.execute(f"""
    GRANT SELECT, INSERT, UPDATE ON TABLE ai_sessions, ai_messages, ai_session_summaries TO {TELEMETRY_ROLE};
    GRANT USAGE, SELECT ON SEQUENCE ai_messages_id_seq, ai_session_summaries_id_seq TO {TELEMETRY_ROLE};
    """)
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE ai_decision_traces TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE ai_sessions, ai_messages, ai_session_summaries FROM cryptonite_agent_ro;
            REVOKE ALL ON SEQUENCE ai_messages_id_seq, ai_session_summaries_id_seq FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            REVOKE SELECT ON TABLE ai_decision_traces FROM cryptonite_api;
        END IF;
    END $$;
    """)
    op.drop_index('ix_ai_session_summaries_session_id', table_name='ai_session_summaries')
    op.drop_table('ai_session_summaries')
    op.drop_index('ix_ai_messages_session_id', table_name='ai_messages')
    op.drop_table('ai_messages')
    op.drop_index('ix_ai_sessions_operator_updated', table_name='ai_sessions')
    op.drop_table('ai_sessions')
