import asyncio
import argparse
import sys
import os
import pyotp

# Adjust path to import from services/api
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services", "api")))

from cryptonite_api.auth import OperatorAuthManager
import asyncpg

async def main():
    parser = argparse.ArgumentParser(description="Create a new Operator user.")
    parser.add_argument("username", help="Username for the new operator")
    parser.add_argument("password", help="Password for the new operator")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL", "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite")
    pool = await asyncpg.create_pool(db_url)
    
    # We pass None for redis, as we don't need it for password hashing and encrypting
    auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=None)

    # Hash the password
    password_hash = auth_mgr.hash_password(args.password)

    # Generate a TOTP secret
    raw_secret = pyotp.random_base32()
    
    # Encrypt the TOTP secret
    ciphertext, nonce, auth_tag = auth_mgr.encrypt_totp_secret(raw_secret)

    # Insert into the database
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO operator_users (
                    username, password_hash, totp_secret_ciphertext,
                    totp_secret_nonce, totp_secret_auth_tag, is_active
                ) VALUES ($1, $2, $3, $4, $5, true);
                """,
                args.username,
                password_hash,
                ciphertext,
                nonce,
                auth_tag
            )
            print(f"✅ Successfully created operator: {args.username}")
            
            # Print TOTP URI for QR code generation
            totp_uri = pyotp.totp.TOTP(raw_secret).provisioning_uri(
                name=args.username,
                issuer_name="Cryptonite"
            )
            print(f"\n🔐 TOTP Secret: {raw_secret}")
            print(f"📱 Provisioning URI: {totp_uri}")
            print("Import this secret into your authenticator app (Google Authenticator, Authy, etc).")
        except asyncpg.exceptions.UniqueViolationError:
            print(f"❌ User '{args.username}' already exists.")
        except Exception as e:
            print(f"❌ Error: {e}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
