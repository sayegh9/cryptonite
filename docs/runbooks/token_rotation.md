# Token Rotation Runbook

## Description
This runbook covers the rotation of internal secrets (HMAC, AES) and Machine API Tokens.

## 1. Rotating Internal HMAC Secret
The `INTERNAL_HMAC_SECRET` is used for authentication between the API Gateway and the Agent Worker.

**Procedure:**
1. Generate a new secure secret: `openssl rand -hex 32`
2. Update the environment variable in both `cryptonite_api` and `cryptonite_agent_worker`.
3. Restart both services simultaneously. Traffic will be dropped during the restart.

## 2. Rotating AES Encryption Key
The `AES_ENCRYPTION_KEY` is used to encrypt TOTP secrets at rest in the `operator_users` table.

**Procedure (Requires Downtime):**
1. Bring the API offline to prevent new TOTP enrollments.
2. Run a migration script that:
   - Reads the old key and new key from environment variables.
   - Selects all rows from `operator_users`.
   - Decrypts `totp_secret_ciphertext` using the old key, nonce, and tag.
   - Encrypts the plaintext secret using the new key, generating a new nonce and tag.
   - Updates the row with the new ciphertext, nonce, and tag.
3. Update the `AES_ENCRYPTION_KEY` in the environment to the new key.
4. Bring the API back online.

## 3. Revoking and Issuing Machine API Tokens
Machine tokens are hashed at rest using SHA-256.

**Issuing a new token:**
Use the Admin UI or insert directly:
```sql
-- Hash the token on the client side or via a secure function before inserting
INSERT INTO machine_api_tokens (token_hash, name, scopes, created_by, expires_at)
VALUES (encode(sha256('plaintext_token'::bytea), 'hex'), 'Cron Automation', '{"operator:read"}', '<admin_id>', '2027-01-01');
```

**Agent fleet token:**
The `stage_debate_proposal` agent tool calls `POST /api/v1/agents/debates/trigger`, which requires operator auth.
Issue a token with the `operator` scope and set its plaintext as `CRYPTONITE_AGENT_API_TOKEN` in `.env`
(read by `agent_worker` and `debate_worker`); the tool sends it as `X-API-Token`.
```sql
INSERT INTO machine_api_tokens (token_hash, name, scopes, created_by)
VALUES (encode(sha256('<plaintext_token>'::bytea), 'hex'), 'Agent Fleet', '{"operator"}', '<admin_id>');
```

**Revoking a token:**
Disable the token immediately:
```sql
UPDATE machine_api_tokens SET is_active = false WHERE name = 'Compromised Script';
```
The token will be rejected on the next request.
