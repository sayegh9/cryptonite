import asyncio
import os
import uuid
import pyotp
import asyncpg
from cryptonite_api.auth import OperatorAuthManager

async def create_admin():
    db_url = "postgresql://cryptonite:cryptonite_dev_pass@localhost:5432/cryptonite"
    
    from dotenv import load_dotenv
    load_dotenv()
    
    totp_secret = pyotp.random_base32()
    
    auth_mgr = OperatorAuthManager(db_pool=None, redis_client=None)
    
    password = "SuperSecretPassword123!"
    password_hash = auth_mgr.hash_password(password)
    
    ciphertext, nonce, auth_tag = auth_mgr.encrypt_totp_secret(totp_secret)
    
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("DELETE FROM operator_users WHERE username = 'admin'")
        user_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO operator_users (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, role)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            user_id, 'admin', password_hash, ciphertext, nonce, auth_tag, 'admin'
        )
        print("Admin user created successfully.")
        print(f"Username: admin")
        print(f"Password: {password}")
        print(f"TOTP Secret: {totp_secret}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(create_admin())
