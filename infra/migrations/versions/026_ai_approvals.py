"""ai approvals: approvals with expiry and a timeout policy, and the notifications outbox

Revision ID: 026
Revises: 025
Create Date: 2026-09-30 12:00:00.000000

AI-first blueprint §4.6 / §5 / D8 (A5.3, A5.4).

- ``ai_approvals``: one row per AI action waiting for (or given) an operator decision. ``timeout_policy``
  says what silence means: DENY for new risk, PROCEED for risk reducing (and for an L2 veto window
  inside the operator's active hours). The orchestrator's sweeper applies it at ``expires_at``.
  ``(source, source_ref)`` is unique, so a retried producer (debate run, Brain card) never creates a
  second approval. ``command_id`` binds an approval to exactly one execution command.
- ``debate_runs.mandate_id``: the mandate a debate decided for.
- ``notifications_outbox``: durable Info / Action / Critical notifications for the notifier
  (Telegram, ntfy). ``dedupe_key`` is unique, so a repeated alert is written once.

Roles: ``cryptonite_api`` (API, debate worker, notifier) reads and writes both; the orchestrator runs
as the owner. ``cryptonite_agent_ro`` gets nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '026'
down_revision: str | None = '025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ai_approvals',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('mandate_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ai_mandates.id'), nullable=False),
        sa.Column('action_class', sa.String(16), nullable=False),
        sa.Column('command_type', sa.String(32), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('source', sa.String(32), nullable=False),
        sa.Column('source_ref', sa.String(128), nullable=False),
        sa.Column('level_at_creation', sa.SmallInteger(), nullable=False),
        sa.Column('timeout_policy', sa.String(8), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('decided_via', sa.String(16), nullable=True),
        sa.Column('decided_by', sa.String(128), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('command_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("action_class IN ('ADD_RISK', 'REDUCE_RISK')", name='chk_ai_approvals_action_class'),
        sa.CheckConstraint("command_type IN ('SUBMIT_ORDER', 'CLOSE_POSITION')", name='chk_ai_approvals_command_type'),
        sa.CheckConstraint("timeout_policy IN ('DENY', 'PROCEED')", name='chk_ai_approvals_timeout_policy'),
        # D8: silence never adds risk at L1; an ADD_RISK PROCEED is only an L2 veto window
        sa.CheckConstraint("NOT (action_class = 'ADD_RISK' AND timeout_policy = 'PROCEED' AND level_at_creation <> 2)",
                           name='chk_ai_approvals_add_risk_timeout'),
        sa.CheckConstraint("action_class = 'ADD_RISK' OR timeout_policy = 'PROCEED'", name='chk_ai_approvals_reduce_timeout'),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'VETOED', 'EXPIRED', 'EXPIRED_PROCEEDED', 'CANCELLED')",
            name='chk_ai_approvals_status'),
        sa.CheckConstraint("decided_via IS NULL OR decided_via IN ('web', 'telegram', 'timeout', 'system')",
                           name='chk_ai_approvals_decided_via'),
        sa.UniqueConstraint('source', 'source_ref', name='uq_ai_approvals_source'),
    )
    op.create_index('ix_ai_approvals_pending_expiry', 'ai_approvals', ['expires_at'],
                    postgresql_where=sa.text("status = 'PENDING'"))
    op.create_index('ix_ai_approvals_mandate', 'ai_approvals', ['mandate_id', sa.text('created_at DESC')])

    op.create_table(
        'notifications_outbox',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('tier', sa.String(8), nullable=False),
        sa.Column('kind', sa.String(48), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False, server_default=''),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('approval_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ai_approvals.id'), nullable=True),
        sa.Column('dedupe_key', sa.String(200), nullable=True, unique=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("tier IN ('INFO', 'ACTION', 'CRITICAL')", name='chk_notifications_outbox_tier'),
        sa.CheckConstraint("status IN ('PENDING', 'SENT', 'FAILED', 'SKIPPED')", name='chk_notifications_outbox_status'),
    )
    op.create_index('ix_notifications_outbox_pending', 'notifications_outbox', ['id'],
                    postgresql_where=sa.text("status = 'PENDING'"))

    # the mandate a debate ran for (A5.7: autonomous execution exists only inside a mandate)
    op.add_column('debate_runs', sa.Column('mandate_id', postgresql.UUID(as_uuid=True), nullable=True))

    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE ai_approvals, notifications_outbox TO cryptonite_api;
            GRANT USAGE, SELECT ON SEQUENCE notifications_outbox_id_seq TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE ai_approvals, notifications_outbox FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_column('debate_runs', 'mandate_id')
    op.drop_index('ix_notifications_outbox_pending', table_name='notifications_outbox')
    op.drop_table('notifications_outbox')
    op.drop_index('ix_ai_approvals_mandate', table_name='ai_approvals')
    op.drop_index('ix_ai_approvals_pending_expiry', table_name='ai_approvals')
    op.drop_table('ai_approvals')
