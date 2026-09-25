"""ai gateway: model routes, richer cost ledger, decision traces, telemetry role, $25 daily cap

Revision ID: 022
Revises: 021
Create Date: 2026-09-25 18:00:00.000000

AI-first blueprint §4.1 / §5 and decision D12.

Roles:
- ``cryptonite_ai_telemetry`` (new, created here): the gateway's own identity. INSERT on the cost
  ledger and decision traces, SELECT on what the budget/routing reads. No tool ever uses it.
- ``cryptonite_agent_ro``: the identity model-callable SQL runs as. It gets nothing new and becomes
  read-only by default, so a prompt can never write telemetry, routes or anything else.

Downgrade note: the $25 cap written by upgrade is not reverted (the operator may have changed it since).
"""
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '022'
down_revision: str | None = '021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TELEMETRY_ROLE = "cryptonite_ai_telemetry"


def upgrade() -> None:
    op.create_table(
        'ai_model_routes',
        sa.Column('role', sa.String(64), primary_key=True),
        sa.Column('primary_model', sa.String(128), nullable=True),
        sa.Column('fallback_model', sa.String(128), nullable=True),
        sa.Column('fallback_policy', sa.String(16), nullable=True),
        sa.Column('priority', sa.String(16), nullable=True),
        sa.Column('max_output_tokens', sa.Integer(), nullable=True),
        sa.Column('thinking_budget', sa.Integer(), nullable=True),
        sa.Column('daily_cap_usd', sa.Numeric(12, 4), nullable=True),
        sa.Column('certified_primary', sa.Boolean(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("fallback_policy IS NULL OR fallback_policy IN ('FAIL_CLOSED', 'DEGRADE')", name='chk_ai_route_policy'),
        sa.CheckConstraint("priority IS NULL OR priority IN ('optional', 'interactive', 'debate', 'critical')", name='chk_ai_route_priority'),
    )

    for col in [
        sa.Column('role', sa.String(64), nullable=True),
        sa.Column('provider', sa.String(32), nullable=True),
        sa.Column('cached_input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('thinking_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(24), nullable=False, server_default='OK'),
    ]:
        op.add_column('llm_cost_ledger', col)
    # Spend can only be added: a negative row would silently raise everyone's remaining budget
    op.create_check_constraint(
        'chk_llm_cost_ledger_non_negative', 'llm_cost_ledger',
        "total_cost_usd >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
        "AND cached_input_tokens >= 0 AND thinking_tokens >= 0",
    )

    op.create_table(
        'ai_decision_traces',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('parent_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('role', sa.String(64), nullable=False),
        sa.Column('model', sa.String(128), nullable=False),
        sa.Column('session_id', sa.String(128), nullable=True),
        sa.Column('mandate_id', sa.String(128), nullable=True),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('tool_calls', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('output', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(16, 6), nullable=False, server_default='0'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('cost_usd >= 0', name='chk_ai_trace_cost_non_negative'),
    )
    op.create_index('ix_ai_decision_traces_created_at', 'ai_decision_traces', ['created_at'])
    op.create_index('ix_ai_decision_traces_parent_id', 'ai_decision_traces', ['parent_id'])

    # D12: $25/day cap, unless the operator already set their own value
    op.execute("UPDATE system_settings SET value = '25.00' WHERE key = 'LLM_DAILY_BUDGET_USD' AND value IN ('10.00', '18.00')")

    password = os.getenv("AI_TELEMETRY_DB_PASSWORD", "ai_telemetry_dev_pass").replace("'", "''")
    op.execute(f"""
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TELEMETRY_ROLE}') THEN
            CREATE ROLE {TELEMETRY_ROLE} LOGIN PASSWORD '{password}';
        END IF;
    END $$;
    """)
    op.execute(f"""
    GRANT SELECT, INSERT ON TABLE llm_cost_ledger TO {TELEMETRY_ROLE};
    GRANT USAGE, SELECT ON SEQUENCE llm_cost_ledger_id_seq TO {TELEMETRY_ROLE};
    GRANT INSERT ON TABLE ai_decision_traces TO {TELEMETRY_ROLE};
    GRANT SELECT ON TABLE ai_model_routes TO {TELEMETRY_ROLE};
    GRANT SELECT (key, value) ON TABLE system_settings TO {TELEMETRY_ROLE};
    """)
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ai_model_routes TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            ALTER ROLE cryptonite_agent_ro SET default_transaction_read_only = on;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute(f"""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            ALTER ROLE cryptonite_agent_ro RESET default_transaction_read_only;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TELEMETRY_ROLE}') THEN
            REVOKE ALL ON TABLE llm_cost_ledger FROM {TELEMETRY_ROLE};
            REVOKE ALL ON SEQUENCE llm_cost_ledger_id_seq FROM {TELEMETRY_ROLE};
            REVOKE SELECT (key, value) ON TABLE system_settings FROM {TELEMETRY_ROLE};
        END IF;
    END $$;
    """)
    # The role itself is cluster-wide (other databases may still grant it), so it is kept.
    op.drop_index('ix_ai_decision_traces_parent_id', table_name='ai_decision_traces')
    op.drop_index('ix_ai_decision_traces_created_at', table_name='ai_decision_traces')
    op.drop_table('ai_decision_traces')
    op.execute('ALTER TABLE llm_cost_ledger DROP CONSTRAINT IF EXISTS chk_llm_cost_ledger_non_negative')
    for name in ('status', 'trace_id', 'latency_ms', 'thinking_tokens', 'cached_input_tokens', 'provider', 'role'):
        op.drop_column('llm_cost_ledger', name)
    op.drop_table('ai_model_routes')
