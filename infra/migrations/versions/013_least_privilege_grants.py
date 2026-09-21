"""least privilege grants

Revision ID: 013
Revises: 011
Create Date: 2026-09-19 15:30:00.000000

"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = '013'
down_revision: str | None = '012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # 1. Verify roles exist
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_writer') THEN
            RAISE EXCEPTION 'Role cryptonite_writer does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
            RAISE EXCEPTION 'Role cryptonite_scanner does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
            RAISE EXCEPTION 'Role cryptonite_api does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_outbox') THEN
            RAISE EXCEPTION 'Role cryptonite_outbox does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
            RAISE EXCEPTION 'Role cryptonite_backtester does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
            RAISE EXCEPTION 'Role cryptonite_executor does not exist';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
            RAISE EXCEPTION 'Role cryptonite_agent_ro does not exist';
        END IF;
    END $$;
    """)

    # 2. Base schema usage and default privileges
    op.execute("""
    GRANT USAGE ON SCHEMA public TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor, cryptonite_agent_ro;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor, cryptonite_agent_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor, cryptonite_agent_ro;
    
    -- Agent RO
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO cryptonite_agent_ro;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO cryptonite_agent_ro;
    """)

    # 3. Apply legacy grants
    op.execute("""
    -- 1. cryptonite_writer
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE instruments, ingest_batches, ingest_checkpoints, quarantine_bars, audit_events TO cryptonite_writer;
    GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_writer;

    -- 2. cryptonite_scanner
    GRANT SELECT ON TABLE instruments, ingest_batches, ingest_checkpoints TO cryptonite_scanner;
    GRANT SELECT, UPDATE ON TABLE scan_jobs TO cryptonite_scanner;
    GRANT SELECT ON TABLE scan_specs, scan_spec_versions TO cryptonite_scanner;
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE scan_runs, signal_candidates, scanner_checkpoints TO cryptonite_scanner;
    GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_scanner;

    -- 3. cryptonite_api
    GRANT SELECT ON TABLE instruments, ingest_batches, ingest_checkpoints, scan_runs, signal_candidates, scanner_checkpoints, outbox_events TO cryptonite_api;
    GRANT SELECT, INSERT, UPDATE ON TABLE scan_specs TO cryptonite_api;
    GRANT SELECT, INSERT ON TABLE scan_spec_versions TO cryptonite_api;
    GRANT SELECT, INSERT, UPDATE ON TABLE scan_jobs TO cryptonite_api;
    GRANT SELECT, INSERT, UPDATE ON TABLE strategies, strategy_versions, backtest_runs TO cryptonite_api;
    GRANT SELECT ON TABLE backtest_results TO cryptonite_api;
    GRANT SELECT ON TABLE paper_accounts, paper_strategy_instances, paper_orders, paper_positions, paper_trades, paper_risk_events, paper_execution_events, integrity_incidents, execution_kill_switch TO cryptonite_api;
    GRANT INSERT, SELECT ON TABLE execution_commands TO cryptonite_api;

    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'operator_users') THEN
            GRANT SELECT (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active),
                  INSERT, UPDATE (password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active, updated_at)
                  ON TABLE operator_users TO cryptonite_api;
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_audit_events, totp_replay_cache TO cryptonite_api;
        END IF;
        
        -- The machine token and rbac migration (011) might have added role
        IF EXISTS (SELECT column_name FROM information_schema.columns WHERE table_name='operator_users' and column_name='role') THEN
            GRANT SELECT (role), UPDATE (role) ON TABLE operator_users TO cryptonite_api;
        END IF;

        IF EXISTS (SELECT FROM pg_views WHERE viewname = 'v_operator_auth') THEN
            GRANT SELECT ON v_operator_auth TO cryptonite_api;
        END IF;
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'trading_balances') THEN
            GRANT SELECT ON TABLE trading_balances, trading_balance_snapshots, trading_orders, trading_order_lists, trading_order_events, trading_fills, execution_reservations, reconciliation_runs, reconciliation_differences TO cryptonite_api;
        END IF;
        
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'machine_api_tokens') THEN
            GRANT SELECT, INSERT, UPDATE ON TABLE machine_api_tokens TO cryptonite_api;
            GRANT USAGE, SELECT ON SEQUENCE machine_api_tokens_id_seq TO cryptonite_api;
        END IF;
    END $$;

    -- 4. cryptonite_outbox
    GRANT SELECT, UPDATE ON TABLE outbox_events TO cryptonite_outbox;

    -- 5. cryptonite_backtester
    GRANT SELECT ON TABLE strategies, strategy_versions, instruments, ingest_batches, ingest_checkpoints TO cryptonite_backtester;
    GRANT SELECT, UPDATE ON TABLE backtest_runs TO cryptonite_backtester;
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE backtest_results TO cryptonite_backtester;
    GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_backtester;

    -- 6. cryptonite_executor
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE paper_accounts, paper_strategy_instances, paper_orders, paper_positions, paper_trades, paper_risk_events, paper_execution_events, integrity_incidents, execution_commands, execution_kill_switch TO cryptonite_executor;
    GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_executor;
    GRANT SELECT ON TABLE strategies, strategy_versions, instruments TO cryptonite_executor;

    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'trading_balances') THEN
            GRANT SELECT, INSERT ON TABLE
                trading_order_events, trading_fills, trading_balance_snapshots, reconciliation_differences TO cryptonite_executor;

            GRANT SELECT, INSERT, UPDATE ON TABLE
                trading_balances, trading_orders, trading_order_lists, execution_reservations,
                reconciliation_runs, execution_commands, execution_kill_switch TO cryptonite_executor;

            REVOKE DELETE ON TABLE
                trading_orders, trading_order_lists, trading_order_events, trading_fills,
                trading_balances, trading_balance_snapshots, execution_reservations,
                reconciliation_runs, reconciliation_differences, execution_commands, execution_kill_switch FROM cryptonite_executor;
        END IF;
        IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'operator_users') THEN
            REVOKE ALL ON TABLE operator_users, auth_audit_events, totp_replay_cache FROM cryptonite_executor;
        END IF;
        IF EXISTS (SELECT FROM pg_proc WHERE proname = 'trip_execution_kill_switch') THEN
            GRANT EXECUTE ON FUNCTION trip_execution_kill_switch(text, text, text, text) TO cryptonite_api, cryptonite_executor;
        END IF;
    END $$;
    """)

def downgrade() -> None:
    pass
