-- Privileged database roles initialization for Cryptonite
-- Executed by postgres superuser during container initialization

DO $$
BEGIN
    -- 1. Ingestion service role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_writer') THEN
        CREATE ROLE cryptonite_writer WITH LOGIN PASSWORD 'writer_dev_pass';
    END IF;

    -- 2. Scanner service role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
        CREATE ROLE cryptonite_scanner WITH LOGIN PASSWORD 'scanner_dev_pass';
    END IF;

    -- 3. API Gateway role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
        CREATE ROLE cryptonite_api WITH LOGIN PASSWORD 'api_dev_pass';
    END IF;

    -- 4. Dedicated Outbox Relay role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_outbox') THEN
        CREATE ROLE cryptonite_outbox WITH LOGIN PASSWORD 'outbox_dev_pass';
    END IF;

    -- 5. Dedicated Backtester role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
        CREATE ROLE cryptonite_backtester WITH LOGIN PASSWORD 'backtester_dev_pass';
    END IF;

    -- 6. Dedicated Paper Executor role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
        CREATE ROLE cryptonite_executor WITH LOGIN PASSWORD 'executor_dev_pass';
    END IF;
END $$;

-- Schema usage
GRANT USAGE ON SCHEMA public TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cryptonite_writer, cryptonite_scanner, cryptonite_api, cryptonite_outbox, cryptonite_backtester, cryptonite_executor;

-- 1. cryptonite_writer: full CRUD on ingest tables, checkpoints, quarantine, audit + INSERT on outbox
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE instruments, ingest_batches, ingest_checkpoints, quarantine_bars, audit_events TO cryptonite_writer;
GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_writer;

-- 2. cryptonite_scanner: read ingest data, claim scan jobs, write runs & candidates, insert outbox
GRANT SELECT ON TABLE instruments, ingest_batches, ingest_checkpoints TO cryptonite_scanner;
GRANT SELECT, UPDATE ON TABLE scan_jobs TO cryptonite_scanner;
GRANT SELECT ON TABLE scan_specs, scan_spec_versions TO cryptonite_scanner;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE scan_runs, signal_candidates, scanner_checkpoints TO cryptonite_scanner;
GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_scanner;

-- 3. cryptonite_api: read data & candidates; create/archive specs, add immutable versions, enqueue jobs, paper read, auth & trading visibility
GRANT SELECT ON TABLE instruments, ingest_batches, ingest_checkpoints, scan_runs, signal_candidates, scanner_checkpoints, outbox_events TO cryptonite_api;
GRANT SELECT, INSERT, UPDATE ON TABLE scan_specs TO cryptonite_api;
GRANT SELECT, INSERT ON TABLE scan_spec_versions TO cryptonite_api;
GRANT SELECT, INSERT, UPDATE ON TABLE scan_jobs TO cryptonite_api;
GRANT SELECT, INSERT, UPDATE ON TABLE strategies, strategy_versions, backtest_runs TO cryptonite_api;
GRANT SELECT ON TABLE backtest_results TO cryptonite_api;
GRANT SELECT ON TABLE paper_accounts, paper_strategy_instances, paper_orders, paper_positions, paper_trades, paper_risk_events, paper_execution_events, integrity_incidents, execution_kill_switch TO cryptonite_api;
GRANT INSERT, SELECT ON TABLE execution_commands TO cryptonite_api;

-- Grant API access to operator authentication and read-only access to trading ledger
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'operator_users') THEN
        GRANT SELECT (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active),
              INSERT, UPDATE (password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, totp_key_version, is_active, updated_at)
              ON TABLE operator_users TO cryptonite_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE auth_audit_events, totp_replay_cache TO cryptonite_api;
    END IF;
    IF EXISTS (SELECT FROM pg_views WHERE viewname = 'v_operator_auth') THEN
        GRANT SELECT ON v_operator_auth TO cryptonite_api;
    END IF;
    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'trading_balances') THEN
        GRANT SELECT ON TABLE trading_balances, trading_balance_snapshots, trading_orders, trading_order_lists, trading_order_events, trading_fills, execution_reservations, reconciliation_runs, reconciliation_differences TO cryptonite_api;
    END IF;
END $$;

-- 4. cryptonite_outbox: ONLY SELECT and UPDATE on outbox_events
GRANT SELECT, UPDATE ON TABLE outbox_events TO cryptonite_outbox;

-- 5. cryptonite_backtester: read strategies/instruments, claim runs, write results, emit outbox events
GRANT SELECT ON TABLE strategies, strategy_versions, instruments, ingest_batches, ingest_checkpoints TO cryptonite_backtester;
GRANT SELECT, UPDATE ON TABLE backtest_runs TO cryptonite_backtester;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE backtest_results TO cryptonite_backtester;
GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_backtester;

-- 6. cryptonite_executor: paper and live/demo execution engine
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE paper_accounts, paper_strategy_instances, paper_orders, paper_positions, paper_trades, paper_risk_events, paper_execution_events, integrity_incidents, execution_commands, execution_kill_switch TO cryptonite_executor;
GRANT INSERT, SELECT ON TABLE outbox_events TO cryptonite_executor;
GRANT SELECT ON TABLE strategies, strategy_versions, instruments TO cryptonite_executor;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_tables WHERE tablename = 'trading_balances') THEN
        -- Pure append-only tables (NO UPDATE, NO DELETE)
        GRANT SELECT, INSERT ON TABLE
            trading_order_events, trading_fills, trading_balance_snapshots, reconciliation_differences TO cryptonite_executor;

        -- Mutable state and projection tables (SELECT, INSERT, UPDATE, NO DELETE)
        GRANT SELECT, INSERT, UPDATE ON TABLE
            trading_balances, trading_orders, trading_order_lists, execution_reservations,
            reconciliation_runs, execution_commands, execution_kill_switch TO cryptonite_executor;

        -- Revoke DELETE explicitly from financial ledger and audit tables
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

