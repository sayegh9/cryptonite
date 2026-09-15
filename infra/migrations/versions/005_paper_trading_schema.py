"""paper trading schema

Revision ID: 005
Revises: 004
Create Date: 2026-09-08 20:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '005'
down_revision: str | None = '004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0. Ensure pgcrypto extension is available
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto";')

    # 1. execution_commands
    op.create_table(
        'execution_commands',
        sa.Column('command_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('idempotency_key', sa.String(128), unique=True, nullable=False),
        sa.Column('command_type', sa.String(64), nullable=False),
        sa.Column('target_instance_id', sa.String(64), nullable=True),
        sa.Column('requester_id', sa.String(64), nullable=False, server_default='web-ui'),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('status', sa.String(32), nullable=False, server_default='PENDING'),
        sa.Column('lease_owner', sa.String(64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempt_id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_execution_commands_pending',
        'execution_commands',
        ['status', 'created_at'],
        postgresql_where=sa.text("status = 'PENDING'")
    )
    op.create_index('ix_execution_commands_attempt_id', 'execution_commands', ['attempt_id'])

    # 2. paper_execution_events (Zero-loss lifecycle journal)
    op.create_table(
        'paper_execution_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('engine_run_id', sa.String(64), nullable=False),
        sa.Column('native_event_id', sa.String(128), nullable=False),
        sa.Column('idempotency_key', sa.String(128), unique=True, nullable=False),
        sa.Column('event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('venue_order_id', sa.String(64), nullable=True),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('event_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('occurred_at_ns', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('client_order_id', 'event_version', name='uq_order_event_version'),
        sa.UniqueConstraint('engine_run_id', 'native_event_id', name='uq_engine_native_event'),
    )
    op.create_index('ix_paper_execution_events_occurred', 'paper_execution_events', ['occurred_at_ns'])
    op.create_index('ix_paper_execution_events_client_order', 'paper_execution_events', ['client_order_id'])

    # 3. integrity_incidents
    op.create_table(
        'integrity_incidents',
        sa.Column('incident_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('engine_run_id', sa.String(64), nullable=False),
        sa.Column('reason', sa.String(64), nullable=False),
        sa.Column('last_durable_journal_id', sa.BigInteger(), nullable=True),
        sa.Column('queue_size', sa.Integer(), nullable=False),
        sa.Column('incident_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reconciled_by', sa.String(64), nullable=True),
        sa.Column('reconciliation_notes', sa.Text(), nullable=True),
    )

    # 4. paper_accounts
    op.create_table(
        'paper_accounts',
        sa.Column('account_id', sa.String(64), primary_key=True),
        sa.Column('venue', sa.String(32), nullable=False, server_default='BINANCE'),
        sa.Column('currency', sa.String(16), nullable=False, server_default='USDT'),
        sa.Column('starting_balance', sa.Numeric(38, 18), nullable=False),
        sa.Column('cash_balance', sa.Numeric(38, 18), nullable=False),
        sa.Column('total_equity', sa.Numeric(38, 18), nullable=False),
        sa.Column('realized_pnl', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('utc_trading_day', sa.Date(), nullable=False, server_default=sa.text('CURRENT_DATE')),
        sa.Column('daily_realized_pnl', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('daily_loss_latched', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 5. paper_strategy_instances
    op.create_table(
        'paper_strategy_instances',
        sa.Column('instance_id', sa.String(64), primary_key=True),
        sa.Column('account_id', sa.String(64), sa.ForeignKey('paper_accounts.account_id'), nullable=False),
        sa.Column('strategy_name', sa.String(64), nullable=False),
        sa.Column('strategy_version_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('strategy_versions.id'), nullable=False),
        sa.Column('strategy_source_hash', sa.String(64), nullable=False),
        sa.Column('risk_policy_hash', sa.String(64), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('timeframe', sa.String(16), nullable=False),
        sa.Column('resolved_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('allocation_usd', sa.Numeric(38, 18), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, server_default='RUNNING'),
        sa.Column('last_processed_bar_ns', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index(
        'uq_active_strategy_per_instrument',
        'paper_strategy_instances',
        ['account_id', 'instrument_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('RUNNING', 'PAUSED')")
    )

    # 6. paper_orders
    op.create_table(
        'paper_orders',
        sa.Column('client_order_id', sa.String(64), primary_key=True),
        sa.Column('venue_order_id', sa.String(64), nullable=True),
        sa.Column('instance_id', sa.String(64), sa.ForeignKey('paper_strategy_instances.instance_id'), nullable=False),
        sa.Column('account_id', sa.String(64), sa.ForeignKey('paper_accounts.account_id'), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('side', sa.String(16), nullable=False),
        sa.Column('order_type', sa.String(32), nullable=False),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('price', sa.Numeric(38, 18), nullable=True),
        sa.Column('filled_qty', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('avg_fill_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('reject_reason', sa.Text(), nullable=True),
        sa.Column('last_event_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name='chk_paper_order_side'),
        sa.CheckConstraint("order_type IN ('MARKET', 'LIMIT', 'STOP_MARKET')", name='chk_paper_order_type'),
    )
    op.create_index(
        'uq_orders_account_venue_order_id',
        'paper_orders',
        ['account_id', 'venue_order_id'],
        unique=True,
        postgresql_where=sa.text("venue_order_id IS NOT NULL")
    )

    # 7. paper_positions
    op.create_table(
        'paper_positions',
        sa.Column('position_id', sa.String(64), primary_key=True),
        sa.Column('account_id', sa.String(64), sa.ForeignKey('paper_accounts.account_id'), nullable=False),
        sa.Column('instance_id', sa.String(64), sa.ForeignKey('paper_strategy_instances.instance_id'), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('side', sa.String(16), nullable=False),
        sa.Column('entry_price', sa.Numeric(38, 18), nullable=False),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('current_price', sa.Numeric(38, 18), nullable=False),
        sa.Column('unrealized_pnl', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('realized_pnl', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("side IN ('LONG', 'FLAT')", name='chk_paper_position_side'),
        sa.CheckConstraint("quantity >= 0", name='chk_paper_position_quantity'),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name='chk_paper_position_status'),
    )

    # 8. paper_trades
    op.create_table(
        'paper_trades',
        sa.Column('trade_id', sa.String(64), primary_key=True),
        sa.Column('account_id', sa.String(64), sa.ForeignKey('paper_accounts.account_id'), nullable=False),
        sa.Column('client_order_id', sa.String(64), sa.ForeignKey('paper_orders.client_order_id'), nullable=False),
        sa.Column('instance_id', sa.String(64), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('side', sa.String(16), nullable=False),
        sa.Column('price', sa.Numeric(38, 18), nullable=False),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('commission', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('commission_currency', sa.String(16), nullable=False, server_default='USDT'),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name='chk_paper_trade_side'),
    )

    # 9. paper_risk_events
    op.create_table(
        'paper_risk_events',
        sa.Column('event_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('instance_id', sa.String(64), nullable=True),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('rule_name', sa.String(64), nullable=False),
        sa.Column('severity', sa.String(16), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('blocked_action', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 10. execution_kill_switch
    op.create_table(
        'execution_kill_switch',
        sa.Column('id', sa.Integer(), primary_key=True, server_default='1'),
        sa.Column('is_latched', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('latched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('latched_by', sa.String(64), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('reset_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reset_by', sa.String(64), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('id = 1', name='chk_single_kill_switch_row'),
    )
    op.execute("INSERT INTO execution_kill_switch (id, is_latched) VALUES (1, false) ON CONFLICT (id) DO NOTHING;")

    # Seed default paper account if none exists
    op.execute("""
    INSERT INTO paper_accounts (account_id, venue, currency, starting_balance, cash_balance, total_equity)
    VALUES ('PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 10000.0, 10000.0, 10000.0)
    ON CONFLICT (account_id) DO NOTHING;
    """)

    # 11. Role permissions
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            GRANT USAGE ON SCHEMA public TO cryptonite_executor;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_executor;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
                paper_accounts, paper_strategy_instances, paper_orders, paper_positions,
                paper_trades, paper_risk_events, paper_execution_events, integrity_incidents,
                execution_commands, execution_kill_switch TO cryptonite_executor;
            GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_executor;
            GRANT SELECT ON TABLE strategies, strategy_versions, instruments TO cryptonite_executor;
        END IF;

        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT SELECT ON TABLE
                paper_accounts, paper_strategy_instances, paper_orders, paper_positions,
                paper_trades, paper_risk_events, paper_execution_events, integrity_incidents,
                execution_kill_switch TO cryptonite_api;
            GRANT INSERT, SELECT ON TABLE execution_commands TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    op.drop_table('execution_kill_switch')
    op.drop_table('paper_risk_events')
    op.drop_table('paper_trades')
    op.drop_table('paper_positions')
    op.drop_table('paper_orders')
    op.drop_table('paper_strategy_instances')
    op.drop_table('paper_accounts')
    op.drop_table('integrity_incidents')
    op.drop_table('paper_execution_events')
    op.drop_table('execution_commands')
