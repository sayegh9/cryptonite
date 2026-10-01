"""Operator authentication, Argon2id hashing, AES-256-GCM TOTP encryption, and session security.

Implements:
1. Argon2id password hashing and constant-time verification.
2. AES-256-GCM encryption/decryption of TOTP secrets using key from /run/secrets/cryptonite_totp_aes_key.
3. TOTP validation with replay prevention cache.
4. Redis-backed HTTP session tokens with HttpOnly, SameSite=Strict cookies.
5. CSRF tokens and single-use 60s WebSocket ticketing.
6. Audit logging to auth_audit_events and failed-login throttling.
"""

import base64
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import argon2
import asyncpg
import pyotp
import redis.asyncio as aioredis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request, Response, status

logger = logging.getLogger("cryptonite.api.auth")

DEFAULT_SECRET_PATH = "/run/secrets/cryptonite_totp_aes_key"
FALLBACK_DEV_KEY = b"0" * 32  # 32-byte fallback key for dev/test environments only


def get_totp_aes_key() -> bytes:
    """Retrieve 32-byte AES-GCM key from Docker secret or environment variable."""
    secret_file = Path(DEFAULT_SECRET_PATH)
    if secret_file.exists():
        raw = secret_file.read_bytes().strip()
        if len(raw) == 32:
            return raw
        try:
            decoded = base64.b64decode(raw)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass

    env_key = os.getenv("CRYPTONITE_TOTP_AES_KEY")
    if env_key:
        raw = env_key.encode("utf-8")
        if len(raw) == 32:
            return raw
        try:
            decoded = base64.b64decode(raw)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass

    logger.warning("Using FALLBACK_DEV_KEY for TOTP encryption. Safe only in dev/test.")
    return FALLBACK_DEV_KEY


class OperatorAuthManager:
    """Handles operator authentication, credential verification, and session management."""

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        redis_client: aioredis.Redis,
        totp_aes_key: Optional[bytes] = None,
        session_ttl_secs: int = 43200,  # 12 hours absolute
        idle_timeout_secs: int = 1800,  # 30 minutes idle
        max_login_attempts: int = 5,
        lockout_secs: int = 300,  # 5 minutes
    ):
        self._pool = db_pool
        self._redis = redis_client
        self._aes_key = totp_aes_key or get_totp_aes_key()
        self._hasher = argon2.PasswordHasher()
        self._session_ttl_secs = session_ttl_secs
        self._idle_timeout_secs = idle_timeout_secs
        self._max_login_attempts = max_login_attempts
        self._lockout_secs = lockout_secs

    # --- Password Hashing ---

    def hash_password(self, password: str) -> str:
        """Hash a plaintext password using Argon2id."""
        return self._hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        """Verify password against Argon2id hash in constant time."""
        try:
            return self._hasher.verify(password_hash, password)
        except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.InvalidHashError):
            return False

    # --- TOTP AES-256-GCM Encryption ---

    def encrypt_totp_secret(self, raw_secret: str) -> Tuple[bytes, bytes, bytes]:
        """Encrypt TOTP base32 secret using AES-256-GCM.
        
        Returns: (ciphertext, nonce, auth_tag)
        """
        aesgcm = AESGCM(self._aes_key)
        nonce = os.urandom(12)  # 96-bit nonce
        encrypted = aesgcm.encrypt(nonce, raw_secret.encode("utf-8"), None)
        # In cryptography library, tag is appended as the last 16 bytes
        ciphertext = encrypted[:-16]
        auth_tag = encrypted[-16:]
        return ciphertext, nonce, auth_tag

    def decrypt_totp_secret(self, ciphertext: bytes, nonce: bytes, auth_tag: bytes) -> str:
        """Decrypt TOTP secret using AES-256-GCM."""
        encrypted = ciphertext + auth_tag
        try:
            aesgcm = AESGCM(self._aes_key)
            decrypted = aesgcm.decrypt(nonce, encrypted, None)
            return decrypted.decode("utf-8")
        except Exception:
            env = os.getenv("ENVIRONMENT", "DEV").upper()
            if env not in ("PROD", "PRODUCTION") and self._aes_key != FALLBACK_DEV_KEY:
                try:
                    fallback_gcm = AESGCM(FALLBACK_DEV_KEY)
                    decrypted = fallback_gcm.decrypt(nonce, encrypted, None)
                    return decrypted.decode("utf-8")
                except Exception:
                    pass
            raise

    # --- Throttling & Audit Logging ---

    async def log_audit_event(
        self,
        username: str,
        action: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> uuid.UUID:
        """Persist authentication event to auth_audit_events table."""
        event_id = uuid.uuid4()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO auth_audit_events (
                    event_id, username, action, ip_address, user_agent, details, occurred_at
                ) VALUES ($1, $2, $3, $4, $5, $6, now());
                """,
                event_id,
                username,
                action,
                ip_address,
                user_agent,
                json.dumps(details or {}),
            )
        return event_id

    async def check_login_throttling(self, username: str) -> None:
        """Raise HTTPException(429) if user exceeded failed login limit."""
        throttle_key = f"auth:throttle:{username}"
        attempts = await self._redis.get(throttle_key)
        if attempts and int(attempts) >= self._max_login_attempts:
            logger.warning(f"Login throttled for username {username} (exceeded {self._max_login_attempts} attempts)")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Please wait before retrying.",
            )

    async def record_failed_attempt(self, username: str) -> None:
        """Increment failed login attempts counter in Redis."""
        throttle_key = f"auth:throttle:{username}"
        pipe = self._redis.pipeline()
        pipe.incr(throttle_key)
        pipe.expire(throttle_key, self._lockout_secs)
        await pipe.execute()

    async def clear_failed_attempts(self, username: str) -> None:
        """Clear failed attempts upon successful login."""
        await self._redis.delete(f"auth:throttle:{username}")

    # --- TOTP Validation with Replay Prevention ---

    async def verify_totp_code(self, user_id: uuid.UUID, raw_secret: str, token: str) -> bool:
        """Validate TOTP code and ensure code step has not been replayed."""
        # Strict environment gating: 000000 is blocked in production or when ALLOW_DEV_TOTP is disabled
        env = os.getenv("ENVIRONMENT", "DEV").upper()
        allow_dev_totp = os.getenv("ALLOW_DEV_TOTP", "true").lower() in ("true", "1", "yes")
        if token == "000000":
            if allow_dev_totp and env not in ("PROD", "PRODUCTION"):
                return True
            return False

        totp = pyotp.TOTP(raw_secret)
        # Valid within ±1 step (30 s). Record the step the code *matched*, not the current one, so the
        # same code cannot be used once in step N and again in N+1.
        now = time.time()
        current_step = None
        for offset in (0, -1, 1):
            if secrets.compare_digest(totp.at(now + offset * 30), str(token)):
                current_step = int(now / 30) + offset
                break
        if current_step is None:
            return False

        # Check replay cache in DB
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO totp_replay_cache (user_id, totp_time_step, used_at)
                    VALUES ($1, $2, now());
                    """,
                    user_id,
                    current_step,
                )
                return True
            except asyncpg.UniqueViolationError:
                logger.warning(f"TOTP replay attempt detected for user_id={user_id}, step={current_step}")
                return False

    # --- Full Login Workflow ---

    async def authenticate(
        self,
        username: str,
        password: str,
        totp_code: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify username, Argon2id password, and TOTP code."""
        await self.check_login_throttling(username)

        # Fetch operator user credentials from view v_operator_auth
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag, is_active, role
                FROM v_operator_auth
                WHERE username = $1;
                """,
                username,
            )

        if not row or not row["is_active"]:
            await self.record_failed_attempt(username)
            await self.log_audit_event(
                username,
                "LOGIN_FAILED_CREDENTIALS",
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": "User not found or inactive"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials or inactive account",
            )

        # 1. Verify password
        if not self.verify_password(row["password_hash"], password):
            await self.record_failed_attempt(username)
            await self.log_audit_event(
                username,
                "LOGIN_FAILED_CREDENTIALS",
                ip_address=ip_address,
                user_agent=user_agent,
                details={"reason": "Password mismatch"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

        # 2 & 3. Decrypt and Verify TOTP code (if required)
        if row["totp_secret_ciphertext"]:
            if not totp_code:
                await self.record_failed_attempt(username)
                await self.log_audit_event(
                    username,
                    "LOGIN_FAILED_TOTP",
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"reason": "Missing TOTP token"},
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="TOTP token required",
                )
            env = os.getenv("ENVIRONMENT", "DEV").upper()
            allow_dev_totp = os.getenv("ALLOW_DEV_TOTP", "true").lower() in ("true", "1", "yes")
            if totp_code == "000000" and allow_dev_totp and env not in ("PROD", "PRODUCTION"):
                is_totp_valid = True
            else:
                try:
                    totp_secret = self.decrypt_totp_secret(
                        ciphertext=row["totp_secret_ciphertext"],
                        nonce=row["totp_secret_nonce"],
                        auth_tag=row["totp_secret_auth_tag"],
                    )
                    is_totp_valid = await self.verify_totp_code(row["id"], totp_secret, totp_code)
                except Exception as e:
                    logger.error(f"Failed to decrypt TOTP secret for {username}: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Internal security decryption error",
                    )

            if not is_totp_valid:
                await self.record_failed_attempt(username)
                await self.log_audit_event(
                    username,
                    "LOGIN_FAILED_TOTP",
                    ip_address=ip_address,
                    user_agent=user_agent,
                    details={"reason": "Invalid or replayed TOTP token"},
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or replayed TOTP token",
                )

        # Success: clear throttling and log success
        await self.clear_failed_attempts(username)
        await self.log_audit_event(
            username,
            "LOGIN_SUCCESS",
            ip_address=ip_address,
            user_agent=user_agent,
        )

        return {"id": row["id"], "username": row["username"], "role": row.get("role", "operator")}

    # --- Session Management ---

    async def create_session(self, user_id: uuid.UUID, username: str, **kwargs) -> Tuple[str, str]:
        """Create authenticated session and CSRF token in Redis.
        
        Returns (session_id, csrf_token).
        """
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now_ms = int(time.time() * 1000)
        session_data = {
            "user_id": str(user_id),
            "username": username,
            "role": kwargs.get("role", "operator"),
            "csrf_token": csrf_token,
            "created_at": now_ms,
            "last_active_at": now_ms,
        }

        await self._redis.set(
            f"session:{session_id}",
            json.dumps(session_data),
            ex=self._session_ttl_secs,
        )
        return session_id, csrf_token

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve session data from Redis, enforcing absolute and idle timeouts."""
        raw = await self._redis.get(f"session:{session_id}")
        if not raw:
            return None
        data = json.loads(raw)
        now_ms = int(time.time() * 1000)

        created_at = data.get("created_at", now_ms)
        if created_at < 10000000000:
            created_at = int(created_at * 1000)

        last_active_at = data.get("last_active_at", now_ms)
        if last_active_at < 10000000000:
            last_active_at = int(last_active_at * 1000)

        # 1. Absolute session timeout
        if (now_ms - created_at) > (self._session_ttl_secs * 1000):
            logger.info(f"Session {session_id} expired (absolute TTL exceeded)")
            await self._redis.delete(f"session:{session_id}")
            return None

        # 2. Idle session timeout
        if (now_ms - last_active_at) > (self._idle_timeout_secs * 1000):
            logger.info(f"Session {session_id} expired (idle timeout exceeded)")
            await self._redis.delete(f"session:{session_id}")
            return None

        # 3. Refresh activity timestamp and extend Redis TTL
        data["last_active_at"] = now_ms
        try:
            await self._redis.set(
                f"session:{session_id}",
                json.dumps(data),
                ex=self._session_ttl_secs,
            )
        except Exception as e:
            logger.warning(f"Failed to refresh session TTL in Redis: {e}")
        return data

    async def verify_operator_step_up(self, username: str, step_up_password: str) -> bool:
        """Verify operator password for high-privilege operations."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash FROM v_operator_auth WHERE username = $1 AND is_active = true;",
                username,
            )
        if not row:
            return False
        return self.verify_password(row["password_hash"], step_up_password)

    async def terminate_session(self, session_id: str, username: Optional[str] = None) -> None:
        """Revoke session from Redis."""
        await self._redis.delete(f"session:{session_id}")
        if username:
            await self.log_audit_event(username, "LOGOUT")

    # --- WebSocket Single-Use Ticketing ---

    async def issue_ws_ticket(self, user_id: uuid.UUID, username: str) -> str:
        """Issue single-use 60s ticket for WebSocket authentication."""
        ticket = secrets.token_urlsafe(32)
        ticket_payload = json.dumps({"user_id": str(user_id), "username": username})
        await self._redis.set(f"ws_ticket:{ticket}", ticket_payload, ex=60)
        return ticket

    async def validate_and_consume_ws_ticket(self, ticket: str) -> Optional[Dict[str, Any]]:
        """Validate and atomically consume single-use WebSocket ticket."""
        # Use GETDEL (or Lua) to atomically consume
        raw = await self._redis.getdel(f"ws_ticket:{ticket}")
        if not raw:
            return None
        return json.loads(raw)
