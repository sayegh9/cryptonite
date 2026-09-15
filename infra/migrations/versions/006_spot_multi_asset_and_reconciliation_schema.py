"""spot multi-asset, authoritative reconciliation, and reservation ledger schema

Revision ID: 006
Revises: 005
Create Date: 2026-09-09 16:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '006'
down_revision: str | None = '005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0. Alter execution_commands to add command_outcome with strict domain values
    op.add_column('execution_commands', sa.Column('command_outcome', sa.String(64), nullable=True))
    op.create_check_constraint(
        'chk_execution_commands_outcome',
        'execution_commands',
        "command_outcome IS NULL OR command_outcome IN ('SUCCESS', 'REJECTED', 'UNKNOWN_PENDING_RECONCILIATION')"
    )

    # 1. operator_users (AES-256-GCM encrypted TOTP secret, Argon2id password hash)
    op.create_table(
        'operator_users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('username', sa.String(64), unique=True, nullable=False),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('totp_secret_ciphertext', sa.LargeBinary(), nullable=False),
        sa.Column('totp_secret_nonce', sa.LargeBinary(), nullable=False),
        sa.Column('totp_secret_auth_tag', sa.LargeBinary(), nullable=False),
        sa.Column('totp_key_version', sa.String(32), nullable=False, server_default='v1'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )

    # 2. auth_audit_events
    op.create_table(
        'auth_audit_events',
        sa.Column('event_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('username', sa.String(64), nullable=False),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_auth_audit_events_user_time', 'auth_audit_events', ['username', 'occurred_at'])

    # 3. totp_replay_cache
    op.create_table(
        'totp_replay_cache',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('operator_users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('totp_time_step', sa.BigInteger(), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('user_id', 'totp_time_step', name='uq_user_totp_step'),
    )

    # 4. trading_balances (Multi-asset live & demo balance projection)
    op.create_table(
        'trading_balances',
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('asset', sa.String(16), nullable=False),
        sa.Column('free', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('locked', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('account_update_time_ns', sa.BigInteger(), nullable=False),
        sa.Column('observed_at_ns', sa.BigInteger(), nullable=False),
        sa.Column('reconciliation_status', sa.String(32), nullable=False, server_default='RECONCILED'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('environment', 'account_id', 'asset', name='pk_trading_balances'),
        sa.CheckConstraint('free >= 0', name='chk_trading_balance_free_positive'),
        sa.CheckConstraint('locked >= 0', name='chk_trading_balance_locked_positive'),
    )

    # 5. trading_balance_snapshots (Authoritative immutable balance snapshots)
    op.create_table(
        'trading_balance_snapshots',
        sa.Column('snapshot_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('snapshot_type', sa.String(32), nullable=False),
        sa.Column('account_update_time_ns', sa.BigInteger(), nullable=False),
        sa.Column('observed_at_ns', sa.BigInteger(), nullable=False),
        sa.Column('balances_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('balances_checksum', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('environment', 'account_id', 'account_update_time_ns', 'balances_checksum', name='uq_balance_snapshot_checksum'),
    )
    op.create_index('ix_trading_balance_snapshots_acc', 'trading_balance_snapshots', ['environment', 'account_id', 'account_update_time_ns'])

    # 6. trading_order_lists (Authoritative OCO journal)
    op.create_table(
        'trading_order_lists',
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('order_list_id', sa.BigInteger(), nullable=False),
        sa.Column('contingency_type', sa.String(32), nullable=False, server_default='OCO'),
        sa.Column('list_status_type', sa.String(32), nullable=False),
        sa.Column('list_order_status', sa.String(32), nullable=False),
        sa.Column('list_client_order_id', sa.String(64), nullable=False),
        sa.Column('transaction_time_ns', sa.BigInteger(), nullable=False),
        sa.Column('limit_client_order_id', sa.String(64), nullable=True),
        sa.Column('stop_client_order_id', sa.String(64), nullable=True),
        sa.Column('first_fill_observed_at_ns', sa.BigInteger(), nullable=True),
        sa.Column('protection_timer_expires_at_ns', sa.BigInteger(), nullable=True),
        sa.Column('protection_status', sa.String(32), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('environment', 'account_id', 'order_list_id', name='pk_trading_order_lists'),
        sa.CheckConstraint("list_status_type IN ('RESPONSE', 'EXEC_STARTED', 'ALL_DONE')", name='chk_order_list_status_type'),
        sa.CheckConstraint("list_order_status IN ('EXECUTING', 'ALL_DONE', 'REJECT')", name='chk_order_list_order_status'),
        sa.CheckConstraint("protection_status IN ('ACTIVE', 'TIMER_EXPIRED', 'PROTECTION_TRIGGERED', 'SETTLED')", name='chk_order_list_protection_status'),
    )

    # 7. trading_orders
    op.create_table(
        'trading_orders',
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('order_list_id', sa.BigInteger(), nullable=True),
        sa.Column('contingency_type', sa.String(32), nullable=True),
        sa.Column('venue_order_id', sa.String(64), nullable=True),
        sa.Column('strategy_id', sa.String(64), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('side', sa.String(16), nullable=False),
        sa.Column('order_type', sa.String(32), nullable=False),
        sa.Column('time_in_force', sa.String(16), nullable=False, server_default='GTC'),
        sa.Column('price', sa.Numeric(38, 18), nullable=True),
        sa.Column('stop_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('quote_quantity', sa.Numeric(38, 18), nullable=True),
        sa.Column('filled_qty', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('cumulative_quote_qty', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('avg_fill_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('raw_venue_status', sa.String(64), nullable=True),
        sa.Column('commission_amount', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('commission_asset', sa.String(16), nullable=True),
        sa.Column('reject_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('environment', 'account_id', 'client_order_id', name='pk_trading_orders'),
        sa.ForeignKeyConstraint(
            ['environment', 'account_id', 'order_list_id'],
            ['trading_order_lists.environment', 'trading_order_lists.account_id', 'trading_order_lists.order_list_id'],
            name='fk_trading_orders_order_list',
            ondelete='RESTRICT',
            deferrable=True,
            initially='DEFERRED',
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name='chk_trading_order_side'),
        sa.CheckConstraint("quantity > 0", name='chk_trading_order_quantity_pos'),
        sa.CheckConstraint("price IS NULL OR price > 0", name='chk_trading_order_price_pos'),
        sa.CheckConstraint("stop_price IS NULL OR stop_price > 0", name='chk_trading_order_stop_price_pos'),
        sa.CheckConstraint("quote_quantity IS NULL OR quote_quantity >= 0", name='chk_trading_order_quote_qty_nonneg'),
        sa.CheckConstraint("filled_qty >= 0 AND filled_qty <= quantity", name='chk_trading_order_filled_qty_valid'),
        sa.CheckConstraint("commission_amount >= 0", name='chk_trading_order_commission_nonneg'),
        sa.CheckConstraint("cumulative_quote_qty >= 0", name='chk_trading_order_cum_quote_nonneg'),
    )
    op.create_index('ix_trading_orders_status', 'trading_orders', ['environment', 'account_id', 'status'])
    op.create_index(
        'uq_trading_orders_venue_order_id',
        'trading_orders',
        ['environment', 'account_id', 'venue_order_id'],
        unique=True,
        postgresql_where=sa.text("venue_order_id IS NOT NULL")
    )

    # 8. trading_order_events (Replay-idempotent event journal)
    op.create_table(
        'trading_order_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('source_event_id', sa.String(128), nullable=False),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('event_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('raw_venue_status', sa.String(64), nullable=True),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('occurred_at_ns', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(
            ['environment', 'account_id', 'client_order_id'],
            ['trading_orders.environment', 'trading_orders.account_id', 'trading_orders.client_order_id'],
            name='fk_order_events_trading_orders',
            ondelete='RESTRICT',
            deferrable=True,
            initially='DEFERRED',
        ),
        sa.UniqueConstraint('environment', 'account_id', 'client_order_id', 'source_event_id', name='uq_order_event_source'),
    )
    op.create_index('ix_trading_order_events_version', 'trading_order_events', ['environment', 'account_id', 'client_order_id', 'event_version'])

    # 9. trading_fills (Authoritative trade executions)
    op.create_table(
        'trading_fills',
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('instrument_id', sa.String(64), nullable=False),
        sa.Column('trade_id', sa.BigInteger(), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('venue_order_id', sa.String(64), nullable=False),
        sa.Column('order_list_id', sa.BigInteger(), nullable=True),
        sa.Column('side', sa.String(16), nullable=False),
        sa.Column('price', sa.Numeric(38, 18), nullable=False),
        sa.Column('quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('quote_quantity', sa.Numeric(38, 18), nullable=False),
        sa.Column('commission_amount', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('commission_asset', sa.String(16), nullable=False),
        sa.Column('is_maker', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('trade_time_ns', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('environment', 'account_id', 'instrument_id', 'trade_id', name='pk_trading_fills'),
        sa.ForeignKeyConstraint(
            ['environment', 'account_id', 'client_order_id'],
            ['trading_orders.environment', 'trading_orders.account_id', 'trading_orders.client_order_id'],
            name='fk_trading_fills_orders',
            ondelete='RESTRICT',
            deferrable=True,
            initially='DEFERRED',
        ),
        sa.CheckConstraint("price > 0", name='chk_trading_fills_price_pos'),
        sa.CheckConstraint("quantity > 0", name='chk_trading_fills_qty_pos'),
        sa.CheckConstraint("quote_quantity >= 0", name='chk_trading_fills_quote_qty_nonneg'),
        sa.CheckConstraint("commission_amount >= 0", name='chk_trading_fills_commission_nonneg'),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name='chk_trading_fills_side'),
    )
    op.create_index('ix_trading_fills_order', 'trading_fills', ['environment', 'account_id', 'client_order_id'])

    # 10. execution_reservations (Durable pre-trade reservation ledger)
    op.create_table(
        'execution_reservations',
        sa.Column('reservation_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('command_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('execution_commands.command_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('asset', sa.String(16), nullable=False),
        sa.Column('amount', sa.Numeric(38, 18), nullable=False),
        sa.Column('fee_slippage_headroom', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('state', sa.String(32), nullable=False, server_default='PRE_SEND'),
        sa.Column('venue_reflected', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('reflected_snapshot_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('trading_balance_snapshots.snapshot_id', ondelete='RESTRICT'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['environment', 'account_id', 'client_order_id'],
            ['trading_orders.environment', 'trading_orders.account_id', 'trading_orders.client_order_id'],
            name='fk_reservations_trading_orders',
            ondelete='RESTRICT',
            deferrable=True,
            initially='DEFERRED',
        ),
        sa.CheckConstraint("amount > 0", name='chk_reservations_amount_pos'),
        sa.CheckConstraint("fee_slippage_headroom >= 0", name='chk_reservations_headroom_nonneg'),
        sa.CheckConstraint(
            "state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED', 'VENUE_REFLECTED', 'SETTLEMENT_PENDING', 'RELEASED')",
            name='chk_reservations_state'
        ),
        sa.CheckConstraint(
            "(venue_reflected = FALSE AND state IN ('PRE_SEND', 'INFLIGHT', 'ACKED_UNREFLECTED', 'RELEASED')) OR "
            "(venue_reflected = TRUE AND state IN ('VENUE_REFLECTED', 'SETTLEMENT_PENDING', 'RELEASED'))",
            name='chk_reservations_venue_reflected_consistency'
        ),
    )
    op.create_index(
        'uq_reservation_per_order_asset',
        'execution_reservations',
        ['environment', 'account_id', 'client_order_id', 'asset'],
        unique=True
    )

    # 11. reconciliation_runs
    op.create_table(
        'reconciliation_runs',
        sa.Column('run_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('environment', sa.String(32), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('trigger_type', sa.String(32), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('balances_matched', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('orders_matched', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('positions_matched', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('diff_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("trigger_type IN ('STARTUP', 'PERIODIC', 'POST_RECONNECT', 'MANUAL')", name='chk_recon_trigger_type'),
        sa.CheckConstraint("status IN ('IN_PROGRESS', 'MATCHED', 'DISCREPANCY_DETECTED', 'FAILED')", name='chk_recon_status'),
    )

    # 12. reconciliation_differences
    op.create_table(
        'reconciliation_differences',
        sa.Column('difference_id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('run_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('reconciliation_runs.run_id', ondelete='RESTRICT'), nullable=False),
        sa.Column('entity_type', sa.String(32), nullable=False),
        sa.Column('entity_id', sa.String(64), nullable=False),
        sa.Column('discrepancy_type', sa.String(64), nullable=False),
        sa.Column('local_state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('venue_state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('action_taken', sa.String(64), nullable=False),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("entity_type IN ('BALANCE', 'ORDER', 'FILL')", name='chk_recon_diff_entity_type'),
        sa.CheckConstraint("discrepancy_type IN ('UNMANAGED_ORDER', 'MISSING_ORDER', 'STATE_MISMATCH', 'QUANTITY_MISMATCH', 'BALANCE_DIVERGENCE')", name='chk_recon_diff_type'),
        sa.CheckConstraint("action_taken IN ('EMERGENCY_HALT', 'LATCHED_KILL_SWITCH', 'NOTIFIED', 'ADOPTED')", name='chk_recon_diff_action'),
    )

    # 13. Create operator_auth_view
    op.execute("""
    CREATE OR REPLACE VIEW v_operator_auth AS
    SELECT id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active
    FROM operator_users;
    """)

    # 14. Least Privilege Security Grants & Isolation
    op.execute("""
    -- Revoke all table permissions from public
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;

    DO $$
    BEGIN
        -- Executor role grants: STRICT APPEND-ONLY ON FINANCIAL & AUDIT TABLES (ZERO DELETE)
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            GRANT USAGE ON SCHEMA public TO cryptonite_executor;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_executor;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cryptonite_executor;

            -- Pure append-only tables: INSERT and SELECT only (NO UPDATE, NO DELETE)
            GRANT SELECT, INSERT ON TABLE
                trading_order_events,
                trading_fills,
                trading_balance_snapshots,
                reconciliation_differences TO cryptonite_executor;

            -- Mutable state and projection tables: SELECT, INSERT, UPDATE only (NO DELETE)
            GRANT SELECT, INSERT, UPDATE ON TABLE
                trading_balances,
                trading_orders,
                trading_order_lists,
                execution_reservations,
                reconciliation_runs,
                execution_commands,
                execution_kill_switch TO cryptonite_executor;

            GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_executor;

            -- Explicitly revoke DELETE from cryptonite_executor across all ledger and audit tables
            REVOKE DELETE ON TABLE
                trading_orders,
                trading_order_lists,
                trading_order_events,
                trading_fills,
                trading_balances,
                trading_balance_snapshots,
                execution_reservations,
                reconciliation_runs,
                reconciliation_differences,
                execution_commands,
                execution_kill_switch FROM cryptonite_executor;

            -- Explicit ZERO ACCESS to operator authentication tables
            REVOKE ALL ON TABLE operator_users, auth_audit_events, totp_replay_cache FROM cryptonite_executor;
        END IF;

        -- API role grants
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            GRANT USAGE ON SCHEMA public TO cryptonite_api;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_api;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cryptonite_api;

            -- Authentication access via view & column grant
            GRANT SELECT ON v_operator_auth TO cryptonite_api;
            GRANT SELECT (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active),
                  INSERT, UPDATE (password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active, updated_at)
                  ON TABLE operator_users TO cryptonite_api;

            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_audit_events, totp_replay_cache TO cryptonite_api;

            -- Read-only visibility into trading ledger & reconciliation
            GRANT SELECT ON TABLE
                trading_balances,
                trading_balance_snapshots,
                trading_orders,
                trading_order_lists,
                trading_order_events,
                trading_fills,
                execution_reservations,
                reconciliation_runs,
                reconciliation_differences TO cryptonite_api;

            -- API has INSERT and SELECT only on execution_commands (cannot update outcomes or lease)
            REVOKE UPDATE ON TABLE execution_commands FROM cryptonite_api;
            GRANT INSERT, SELECT ON TABLE execution_commands TO cryptonite_api;
        END IF;
    END $$;
    """)


def downgrade() -> None:
    # 1. Drop view
    op.execute("DROP VIEW IF EXISTS v_operator_auth;")

    # 2. Drop ledger and reconciliation tables
    op.drop_table('reconciliation_differences')
    op.drop_table('reconciliation_runs')
    op.drop_table('execution_reservations')
    op.drop_table('trading_fills')
    op.drop_table('trading_order_events')
    op.drop_table('trading_orders')
    op.drop_table('trading_order_lists')
    op.drop_table('trading_balance_snapshots')
    op.drop_table('trading_balances')

    # 3. Drop auth tables
    op.drop_table('totp_replay_cache')
    op.drop_table('auth_audit_events')
    op.drop_table('operator_users')

    # 4. Remove command_outcome column and constraint from execution_commands
    op.drop_constraint('chk_execution_commands_outcome', 'execution_commands', type_='check')
    op.drop_column('execution_commands', 'command_outcome')
