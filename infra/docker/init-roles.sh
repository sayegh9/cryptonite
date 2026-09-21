#!/bin/bash
set -euo pipefail

# Initialize roles for Cryptonite
# Safely extract from environment with fallbacks

DB_USER=${POSTGRES_USER:-postgres}
DB_NAME=${POSTGRES_DB:-cryptonite}

API_PASS=${CRYPTONITE_API_PASSWORD:-api_dev_pass}
EXEC_PASS=${CRYPTONITE_EXECUTOR_PASSWORD:-executor_dev_pass}
AGENT_RO_PASS=${CRYPTONITE_AGENT_RO_PASSWORD:-agent_ro_dev_pass}
WRITER_PASS=${CRYPTONITE_WRITER_PASSWORD:-writer_dev_pass}
SCANNER_PASS=${CRYPTONITE_SCANNER_PASSWORD:-scanner_dev_pass}
OUTBOX_PASS=${CRYPTONITE_OUTBOX_PASSWORD:-outbox_dev_pass}
BACKTESTER_PASS=${CRYPTONITE_BACKTESTER_PASSWORD:-backtester_dev_pass}

psql -v ON_ERROR_STOP=1 --username "$DB_USER" --dbname "$DB_NAME" <<-EOSQL
DO \$\$
BEGIN
    -- 1. Ingestion service role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_writer') THEN
        CREATE ROLE cryptonite_writer WITH LOGIN PASSWORD '$WRITER_PASS';
    END IF;

    -- 2. Scanner service role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_scanner') THEN
        CREATE ROLE cryptonite_scanner WITH LOGIN PASSWORD '$SCANNER_PASS';
    END IF;

    -- 3. API Gateway role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_api') THEN
        CREATE ROLE cryptonite_api WITH LOGIN PASSWORD '$API_PASS';
    END IF;

    -- 4. Dedicated Outbox Relay role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_outbox') THEN
        CREATE ROLE cryptonite_outbox WITH LOGIN PASSWORD '$OUTBOX_PASS';
    END IF;

    -- 5. Dedicated Backtester role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_backtester') THEN
        CREATE ROLE cryptonite_backtester WITH LOGIN PASSWORD '$BACKTESTER_PASS';
    END IF;

    -- 6. Dedicated Paper Executor role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_executor') THEN
        CREATE ROLE cryptonite_executor WITH LOGIN PASSWORD '$EXEC_PASS';
    END IF;

    -- 7. Read-only Agent role
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cryptonite_agent_ro') THEN
        CREATE ROLE cryptonite_agent_ro WITH LOGIN PASSWORD '$AGENT_RO_PASS';
    END IF;
END \$\$;
EOSQL
