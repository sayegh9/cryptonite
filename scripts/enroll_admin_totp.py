"""Re-enroll the dev `admin` operator's TOTP: new random secret, encrypted with the API's own key.

Run it INSIDE the api container from YOUR terminal (the secret is printed once, to you only):
    docker exec -i cryptonite_api python - < scripts/enroll_admin_totp.py
Then add the printed key to your authenticator app (time-based, 6 digits, 30 s).
"""
import asyncio
import os
from unittest.mock import MagicMock

import asyncpg
import pyotp
import redis.asyncio as aioredis
from cryptonite_api.auth import OperatorAuthManager

USERNAME = "admin"
# The api role cannot UPDATE operator_users; use the dev owner role (dev password from compose).
OWNER_DSN = os.getenv("ENROLL_DSN", "postgresql://cryptonite:cryptonite_dev_pass@postgres:5432/cryptonite")


async def main() -> None:
    secret = pyotp.random_base32()
    ct, nonce, tag = OperatorAuthManager(MagicMock(), MagicMock()).encrypt_totp_secret(secret)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await conn.fetchrow(
            "UPDATE operator_users SET totp_secret_ciphertext=$1, totp_secret_nonce=$2, totp_secret_auth_tag=$3, "
            "updated_at=now() WHERE username=$4 AND is_active RETURNING id", ct, nonce, tag, USERNAME)
        if row is None:
            raise SystemExit(f"no active operator named {USERNAME!r}; nothing changed")
        await conn.execute("DELETE FROM totp_replay_cache WHERE user_id=$1", row["id"])
        user_id = row["id"]
    finally:
        await conn.close()
    r = aioredis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    await r.delete(f"ai:totp_failures:user:{user_id}")  # clear the lockout counter for this user
    await r.aclose()
    print(f"\nTOTP re-enrolled for '{USERNAME}'. Add this to your authenticator app (time-based):")
    print(f"  Key (manual entry): {secret}")
    print(f"  otpauth URI:        {pyotp.TOTP(secret).provisioning_uri(name=USERNAME, issuer_name='Cryptonite')}")
    print("Then clear this terminal. The key is not stored anywhere in plain text.\n")


asyncio.run(main())
