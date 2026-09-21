import asyncio
import asyncpg
import pyotp
import redis.asyncio as aioredis
from cryptonite_api.auth import OperatorAuthManager

async def seed_admin():
    db_url = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
    redis_url = "redis://localhost:6379/0"

    # 1. Clear throttle in Redis
    r = aioredis.from_url(redis_url)
    await r.delete("auth:throttle:admin")
    # Also delete any other throttle keys
    keys = await r.keys("auth:throttle:*")
    for k in keys:
        await r.delete(k)
    print("Cleared Redis throttle keys")
    await r.aclose()

    # 2. Generate correct Argon2id hash and encrypted TOTP secret
    auth_mgr = OperatorAuthManager(db_pool=None, redis_client=None)
    password = "admin123"
    password_hash = auth_mgr.hash_password(password)
    totp_secret = pyotp.random_base32()
    ciphertext, nonce, auth_tag = auth_mgr.encrypt_totp_secret(totp_secret)

    # 3. Connect to DB and insert
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("DELETE FROM operator_users WHERE username = 'admin'")
        await conn.execute(
            """
            INSERT INTO operator_users (
                username, 
                password_hash, 
                totp_secret_ciphertext, 
                totp_secret_nonce, 
                totp_secret_auth_tag, 
                totp_key_version,
                role, 
                is_active
            )
            VALUES ($1, $2, $3, $4, $5, 'v1', $6, true)
            """,
            "admin",
            password_hash,
            ciphertext,
            nonce,
            auth_tag,
            "admin",
        )
        print("Admin user created successfully in PostgreSQL.")
        print("Username: admin")
        print("Password: admin123")
        print(f"TOTP Secret: {totp_secret}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(seed_admin())
