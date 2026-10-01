"""ai mandates: mandates, mandate equity / high-water mark, autonomy events, execution_commands.mandate_id

Revision ID: 025
Revises: 024
Create Date: 2026-09-30 10:00:00.000000

AI-first blueprint §4.4 / §4.5 / §5 (A5.1, A5.2, A5.5).

- ``ai_mandates``: the scope an AI may trade in. The CHECK constraints repeat the hard ceilings of
  ``cryptonite_schemas.mandate`` (position <= 25% of budget, hard stop <= 20%, leverage <= 5, perps
  only at L3 per O1), so no row can loosen them whoever writes it.
- ``ai_mandate_equity``: equity / high-water-mark snapshots from the orchestrator's tracker.
  ``mandate_id IS NULL`` rows track the account-wide 20% backstop (O2).
- ``ai_autonomy_events``: promotions (proposed by the system, confirmed with TOTP), demotions,
  halts, resumes and breaches. Append-mostly; a proposal row carries its own resolution.
- ``execution_commands.mandate_id``: the orchestrator resolves it on every AI-originated command.

Roles: the orchestrator runs as the owner. ``cryptonite_api`` (API and debate worker) reads and edits
mandates and writes autonomy events; it only reads equity. ``cryptonite_agent_ro`` gets nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '025'
down_revision: str | None = '024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'ai_mandates',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('name', sa.String(80), nullable=False, unique=True),
        sa.Column('capital_budget_usd', sa.Numeric(38, 8), nullable=False),
        sa.Column('instruments', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('max_position_pct', sa.Numeric(6, 3), nullable=False),
        sa.Column('max_drawdown_pct', sa.Numeric(6, 3), nullable=False, server_default='20'),
        sa.Column('allow_perps', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('max_leverage', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('strategies', postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column('autonomy_level', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(16), nullable=False, server_default='ACTIVE'),
        sa.Column('active_hours_utc', sa.String(11), nullable=True),
        sa.Column('veto_window_s', sa.Integer(), nullable=False, server_default='600'),
        sa.Column('halted_reason', sa.Text(), nullable=True),
        sa.Column('halted_at', sa.DateTime(timezone=True), nullable=True),
        # the tracker's high-water mark restarts here (creation, resume after a halt)
        sa.Column('hwm_rebased_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        # a drawdown >= half the hard stop demotes once per episode: the HWM that episode started from
        sa.Column('last_dd_demotion_hwm', sa.Numeric(38, 8), nullable=True),
        sa.Column('level_since', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('capital_budget_usd > 0', name='chk_ai_mandates_budget'),
        sa.CheckConstraint('cardinality(instruments) >= 1', name='chk_ai_mandates_instruments'),
        sa.CheckConstraint('max_position_pct >= 1 AND max_position_pct <= 25', name='chk_ai_mandates_position_pct'),
        sa.CheckConstraint('max_drawdown_pct > 0 AND max_drawdown_pct <= 20', name='chk_ai_mandates_drawdown_pct'),
        sa.CheckConstraint('max_leverage >= 1 AND max_leverage <= 5', name='chk_ai_mandates_leverage'),
        sa.CheckConstraint('autonomy_level IN (1, 2, 3)', name='chk_ai_mandates_level'),
        sa.CheckConstraint('NOT allow_perps OR autonomy_level = 3', name='chk_ai_mandates_perps_l3'),
        sa.CheckConstraint("status IN ('ACTIVE', 'HALTED', 'PAUSED', 'ARCHIVED')", name='chk_ai_mandates_status'),
        sa.CheckConstraint('veto_window_s BETWEEN 60 AND 3600', name='chk_ai_mandates_veto_window'),
        sa.CheckConstraint(r"active_hours_utc IS NULL OR active_hours_utc ~ '^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$'",
                           name='chk_ai_mandates_active_hours'),
    )

    op.create_table(
        'ai_mandate_equity',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('mandate_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ai_mandates.id'), nullable=True),
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('equity', sa.Numeric(38, 8), nullable=False),
        sa.Column('hwm', sa.Numeric(38, 8), nullable=False),
        sa.Column('drawdown_pct', sa.Numeric(10, 4), nullable=False),
        sa.Column('benchmark_price', sa.Numeric(38, 8), nullable=True),  # BTC at ts, for "Sortino vs BTC hold"
    )
    op.create_index('ix_ai_mandate_equity_mandate_ts', 'ai_mandate_equity', ['mandate_id', sa.text('ts DESC')])

    op.create_table(
        'ai_autonomy_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('mandate_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ai_mandates.id'), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('from_level', sa.SmallInteger(), nullable=True),
        sa.Column('to_level', sa.SmallInteger(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('detail', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('actor', sa.String(128), nullable=False, server_default='system'),
        # PROPOSED rows only: PENDING -> CONFIRMED | CANCELLED | EXPIRED
        sa.Column('proposal_status', sa.String(16), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "kind IN ('CREATED', 'PROPOSED', 'PROMOTED', 'DEMOTED', 'HALTED', 'RESUMED', 'PAUSED', 'BREACH', 'EDITED', 'BACKSTOP')",
            name='chk_ai_autonomy_events_kind'),
        sa.CheckConstraint(
            "proposal_status IS NULL OR proposal_status IN ('PENDING', 'CONFIRMED', 'CANCELLED', 'EXPIRED')",
            name='chk_ai_autonomy_events_proposal_status'),
    )
    op.create_index('ix_ai_autonomy_events_mandate', 'ai_autonomy_events', ['mandate_id', sa.text('id DESC')])
    # at most one open promotion proposal per mandate
    op.create_index('uq_ai_autonomy_open_proposal', 'ai_autonomy_events', ['mandate_id'], unique=True,
                    postgresql_where=sa.text("kind = 'PROPOSED' AND proposal_status = 'PENDING'"))

    op.add_column('execution_commands', sa.Column('mandate_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index('ix_execution_commands_mandate_id', 'execution_commands', ['mandate_id'],
                    postgresql_where=sa.text('mandate_id IS NOT NULL'))

    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE ai_mandates, ai_autonomy_events TO cryptonite_api;
            GRANT SELECT ON TABLE ai_mandate_equity TO cryptonite_api;
            GRANT USAGE, SELECT ON SEQUENCE ai_autonomy_events_id_seq TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            REVOKE ALL ON TABLE ai_mandates, ai_mandate_equity, ai_autonomy_events FROM cryptonite_agent_ro;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_index('ix_execution_commands_mandate_id', table_name='execution_commands')
    op.drop_column('execution_commands', 'mandate_id')
    op.drop_index('uq_ai_autonomy_open_proposal', table_name='ai_autonomy_events')
    op.drop_index('ix_ai_autonomy_events_mandate', table_name='ai_autonomy_events')
    op.drop_table('ai_autonomy_events')
    op.drop_index('ix_ai_mandate_equity_mandate_ts', table_name='ai_mandate_equity')
    op.drop_table('ai_mandate_equity')
    op.drop_table('ai_mandates')
