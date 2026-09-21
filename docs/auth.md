# Cryptonite Authentication & Identity

## Overview
Cryptonite uses a unified identity policy matrix:
- **Cookie Session + Mutable Endpoints**: Requires RBAC and strict CSRF validation.
- **Cookie Session + Safe Reads**: Requires RBAC without CSRF.
- **Machine API Tokens**: Requires exact scoped access without CSRF (used by automation).

## TOTP Enrollment
Time-Based One-Time Passwords (TOTP) are required for all interactive Operator sessions.
1. When generating a new operator, the `agy` CLI uses the `AES_ENCRYPTION_KEY` to securely encrypt the TOTP secret.
2. The user is presented with a QR code during enrollment.
3. On login, the operator must provide their Username, Password, and the 6-digit TOTP code. Replay attacks are prevented via a temporal nonce check.

## Machine API Tokens
Automation scripts must no longer use the deprecated "Unauthenticated compatibility mode" or try to spoof browser sessions. Instead:
1. Generate a machine token using the Admin UI or via the database.
2. The token is hashed via SHA-256 at rest.
3. Send the token via the `X-API-Token` header:
   ```bash
   curl -H "X-API-Token: <your_plaintext_token>" https://api.cryptonite.com/api/v1/paper/kill-switch
   ```
4. Machine tokens are scoped (e.g., `operator:read`, `execution:control`). Without the required scope, the request will receive a `403 Forbidden`.

## Execution Modes
- **API Gateway (port 8000)**: Serves all public and operator browser traffic (bound to `0.0.0.0` for local and LAN access).
- **Agent Worker (port 8085)**: Strictly isolated to internal API Gateway traffic. Verified via `X-Internal-Signature` HMAC (`X-Internal-Timestamp`, `X-Internal-Nonce`, `X-Internal-Signature`). External ingress should be firewalled.

## Development & Test Credentials
In local and development environments:
- **Default Operator**: `admin`
- **Default Password**: `admin123`
- **Development TOTP Bypass**: `000000`

## AI Agent Least Privilege (cryptonite_agent_ro)
AI Agents access PostgreSQL strictly through the `cryptonite_agent_ro` role:
- Configured with `ALTER ROLE cryptonite_agent_ro SET default_transaction_read_only = 'on';`
- Permitted strictly `SELECT` on authorized analytical and ledger tables.
- Mutation commands (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`) are rejected at the database engine level.


