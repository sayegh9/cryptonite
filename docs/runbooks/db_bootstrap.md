# Database Bootstrap Runbook

## Description
This runbook covers the initialization of the PostgreSQL database, roles, and schema for a fresh Cryptonite environment.

## 1. Initializing Roles
Cryptonite uses a strict Least Privilege model. Roles must be created before Alembic migrations apply `ALTER DEFAULT PRIVILEGES`.

**Using Docker Compose (Recommended)**
The `init-roles.sh` script runs automatically on the first boot of the `postgres` container in docker-compose.
Ensure your `.env` contains the required passwords:
```env
CRYPTONITE_API_PASSWORD=super_secret
CRYPTONITE_EXECUTOR_PASSWORD=executor_secret
CRYPTONITE_AGENT_RO_PASSWORD=agent_ro_secret
```

**Manual Execution**
If bootstrapping an external managed database (e.g., Cloud SQL, RDS), execute the script from a machine with `psql` access as a superuser:
```bash
export POSTGRES_USER=postgres
export POSTGRES_PASSWORD=...
export POSTGRES_DB=cryptonite
# Ensure the cryptonite passwords are set
./infra/docker/init-roles.sh
```

## 2. Applying Migrations
Once roles exist, run Alembic migrations from the `cryptonite_api` container or a local environment:
```bash
# Inside docker
docker compose -f infra/docker-compose.yml exec api alembic -c infra/alembic.ini upgrade head
```

Migrations enforce schema creation and Least Privilege grants:
- `001` - `007`: Core tables, accounts, instruments, signals, outbox, and spot reconciliation.
- `008_market_data_and_alerts.py`: Adds `coingecko_id` to `instruments`, creates `user_alerts` table, and grants full DDL/DML permissions to `cryptonite_api`.

## 3. Creating the First Operator / Admin
To seed the default administrator account (`admin` / `admin123` with TOTP `000000` bypass in development mode):

```sql
-- Connect via psql:
-- docker compose -f infra/docker-compose.yml exec postgres psql -U cryptonite -d cryptonite

INSERT INTO operator_users (
    username, 
    password_hash, 
    totp_secret_ciphertext, 
    totp_secret_nonce, 
    totp_secret_auth_tag, 
    role
)
VALUES (
    'admin', 
    '$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$R01v0U6u6B+z5x8yJqEw5f2kZ6xYn7hQ8o1iK4uA9mE', -- 'admin123'
    '\x00', 
    '\x00', 
    '\x00', 
    'admin'
)
ON CONFLICT (username) DO NOTHING;
```
*Note: In production environments, use the `agy` CLI or admin onboarding script to generate a unique random Argon2id hash and encrypted AES-GCM TOTP secrets.*


