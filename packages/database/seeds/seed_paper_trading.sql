-- Seed script for Paper Trading Operational Readiness
-- Initializes paper_accounts, system paper_strategy_instances, and purges poisoned command loops.

BEGIN;

-- 1. Seed default paper account
INSERT INTO paper_accounts (
    account_id, venue, currency, starting_balance, cash_balance, total_equity,
    realized_pnl, utc_trading_day, daily_realized_pnl, daily_loss_latched, updated_at
) VALUES (
    'PAPER-ACCOUNT-001', 'BINANCE', 'USDT', 100000.0, 100000.0, 100000.0,
    0.0, CURRENT_DATE, 0.0, false, now()
) ON CONFLICT (account_id) DO UPDATE SET
    starting_balance = 100000.0,
    cash_balance = 100000.0,
    total_equity = 100000.0,
    realized_pnl = 0.0,
    daily_realized_pnl = 0.0,
    daily_loss_latched = false,
    updated_at = now();

-- 2. Seed system strategy instances for Manual Orders and AI Debate Worker
-- Satisfies foreign key constraints on paper_orders.instance_id and paper_positions.instance_id
DO $$
DECLARE
    v_strategy_version_id uuid;
BEGIN
    SELECT id INTO v_strategy_version_id FROM strategy_versions ORDER BY created_at ASC LIMIT 1;
    IF v_strategy_version_id IS NULL THEN
        -- If no strategy version exists, generate a dummy parent strategy & version
        INSERT INTO strategies (id, name, description, created_at, updated_at)
        VALUES ('00000000-0000-0000-0000-000000000001'::uuid, 'SystemBaseline', 'Baseline strategy for system orders', now(), now())
        ON CONFLICT (id) DO NOTHING;

        INSERT INTO strategy_versions (id, strategy_id, version_num, code, parameters, risk_policy, created_at)
        VALUES ('00000000-0000-0000-0000-000000000001'::uuid, '00000000-0000-0000-0000-000000000001'::uuid, 1, '# system', '{}'::jsonb, '{}'::jsonb, now())
        ON CONFLICT (id) DO NOTHING;

        v_strategy_version_id := '00000000-0000-0000-0000-000000000001'::uuid;
    END IF;

    INSERT INTO paper_strategy_instances (
        instance_id, account_id, strategy_name, strategy_version_id,
        strategy_source_hash, risk_policy_hash, instrument_id, timeframe,
        resolved_parameters, allocation_usd, status, last_processed_bar_ns,
        created_at, updated_at
    ) VALUES 
    (
        'MANUAL_TRADE', 'PAPER-ACCOUNT-001', 'ManualTrading', v_strategy_version_id,
        'system_manual', 'system_manual', 'MANUAL.DISCRETIONARY', '1m',
        '{}'::jsonb, 50000.0, 'SYSTEM', 0, now(), now()
    ),
    (
        'AI_DEBATE_WORKER', 'PAPER-ACCOUNT-001', 'AIDebateTrading', v_strategy_version_id,
        'system_debate', 'system_debate', 'AI.DEBATE', '1m',
        '{}'::jsonb, 50000.0, 'SYSTEM', 0, now(), now()
    )
    ON CONFLICT (instance_id) DO UPDATE SET
        status = 'SYSTEM',
        updated_at = now();
END $$;

-- 3. Purge poisoned commands stuck in retry loops
UPDATE execution_commands
SET status = 'REJECTED',
    command_outcome = 'REJECTED',
    error_message = 'Purged during Paper Trading readiness initialization',
    lease_owner = NULL,
    lease_expires_at = NULL,
    processed_at = now()
WHERE status = 'RUNNING' OR attempt_count > 10;

COMMIT;
