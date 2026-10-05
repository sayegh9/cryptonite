"""Tests for Operator Authentication, Argon2id, AES-256-GCM TOTP, replay prevention, and sessions."""

import os
import uuid
import asyncpg
import pyotp
import pytest
import redis.asyncio as aioredis
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cryptonite_api.auth import OperatorAuthManager
from cryptonite_api.main import create_app
from connections import DB_URL, REDIS_URL



@pytest.mark.asyncio
async def test_argon2id_and_aes_gcm_totp_cryptography():
    """Verify Argon2id password hashing and AES-256-GCM TOTP encryption/decryption."""
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=redis)

        # 1. Argon2id password hashing
        password = "ExtremelySecurePassword!2026"
        pwd_hash = auth_mgr.hash_password(password)
        assert pwd_hash.startswith("$argon2id$")
        assert auth_mgr.verify_password(pwd_hash, password) is True
        assert auth_mgr.verify_password(pwd_hash, "WrongPassword!") is False

        # 2. AES-256-GCM TOTP encryption
        raw_secret = pyotp.random_base32()
        ciphertext, nonce, auth_tag = auth_mgr.encrypt_totp_secret(raw_secret)
        assert len(nonce) == 12
        assert len(auth_tag) == 16

        decrypted = auth_mgr.decrypt_totp_secret(ciphertext, nonce, auth_tag)
        assert decrypted == raw_secret

        # 3. Tampering detection
        tampered_tag = bytearray(auth_tag)
        tampered_tag[0] ^= 0xFF
        with pytest.raises(InvalidTag):
            auth_mgr.decrypt_totp_secret(ciphertext, nonce, bytes(tampered_tag))
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_totp_validation_and_replay_prevention():
    """Verify TOTP validation and strict database-backed replay prevention."""
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=redis)
        user_id = uuid.uuid4()
        username = f"op_{uuid.uuid4().hex[:8]}"

        raw_secret = pyotp.random_base32()
        cipher, nonce, tag = auth_mgr.encrypt_totp_secret(raw_secret)
        pwd_hash = auth_mgr.hash_password("Pass12345!")

        # Create operator user in DB
        await pool.execute(
            """
            INSERT INTO operator_users (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag)
            VALUES ($1, $2, $3, $4, $5, $6);
            """,
            user_id,
            username,
            pwd_hash,
            cipher,
            nonce,
            tag,
        )

        totp = pyotp.TOTP(raw_secret)
        code = totp.now()

        # 1. First verification must succeed
        valid = await auth_mgr.verify_totp_code(user_id, raw_secret, code)
        assert valid is True

        # 2. Replay attempt with same code within same window must be rejected
        replay_valid = await auth_mgr.verify_totp_code(user_id, raw_secret, code)
        assert replay_valid is False
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_auth_throttling_and_lockout():
    """Verify failed login attempts trigger rate limiting (HTTP 429) and record audit events."""
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        auth_mgr = OperatorAuthManager(
            db_pool=pool,
            redis_client=redis,
            max_login_attempts=3,
            lockout_secs=60,
        )
        username = f"throttle_{uuid.uuid4().hex[:8]}"

        # Simulate 3 failed attempts
        for _ in range(3):
            await auth_mgr.record_failed_attempt(username)

        # 4th attempt must trigger 429 Too Many Requests
        with pytest.raises(HTTPException) as exc_info:
            await auth_mgr.check_login_throttling(username)
        assert exc_info.value.status_code == 429

        # Clear failed attempts
        await auth_mgr.clear_failed_attempts(username)
        # Should not raise now
        await auth_mgr.check_login_throttling(username)
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_session_and_ws_ticket_single_use():
    """Verify session creation in Redis and single-use WebSocket ticketing."""
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=redis)
        user_id = uuid.uuid4()
        username = "operator_trader"

        # 1. Create session
        session_id, csrf_token = await auth_mgr.create_session(user_id, username)
        session_data = await auth_mgr.get_session(session_id)
        assert session_data is not None
        assert session_data["username"] == username
        assert session_data["csrf_token"] == csrf_token

        # 2. Issue single-use WS ticket
        ticket = await auth_mgr.issue_ws_ticket(user_id, username)
        assert isinstance(ticket, str)

        # 3. First consumption must succeed
        consumed = await auth_mgr.validate_and_consume_ws_ticket(ticket)
        assert consumed is not None
        assert consumed["username"] == username

        # 4. Second consumption must fail (single-use)
        replayed = await auth_mgr.validate_and_consume_ws_ticket(ticket)
        assert replayed is None

        # 5. Terminate session
        await auth_mgr.terminate_session(session_id, username)
        assert await auth_mgr.get_session(session_id) is None
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_auth_routes_e2e():
    """Verify full FastAPI auth routes: login, me, ws-ticket, and logout."""
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)

    try:
        user_id = uuid.uuid4()
        username = f"admin_{uuid.uuid4().hex[:8]}"
        password = "SecretPassword!2026"

        auth_mgr = OperatorAuthManager(db_pool=pool, redis_client=redis)
        raw_secret = pyotp.random_base32()
        cipher, nonce, tag = auth_mgr.encrypt_totp_secret(raw_secret)
        pwd_hash = auth_mgr.hash_password(password)

        await pool.execute(
            """
            INSERT INTO operator_users (id, username, password_hash, totp_secret_ciphertext, totp_secret_nonce, totp_secret_auth_tag)
            VALUES ($1, $2, $3, $4, $5, $6);
            """,
            user_id,
            username,
            pwd_hash,
            cipher,
            nonce,
            tag,
        )

        app = create_app()
        app.state.pool = pool
        app.state.stream_hub = type("MockHub", (), {"redis": redis, "clients": set()})()
        app.state.auth_manager = auth_mgr

        with TestClient(app) as client:
            totp = pyotp.TOTP(raw_secret)
            totp_code = totp.now()

            # 1. Login
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": password, "totp_code": totp_code},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["success"] is True
            csrf_token = data["csrf_token"]
            assert "cryptonite_session" in resp.cookies

            # 2. Get /me
            me_resp = client.get("/api/v1/auth/me")
            assert me_resp.status_code == 200
            assert me_resp.json()["username"] == username

            # 3. Issue WS ticket (requires CSRF header)
            ticket_resp = client.post(
                "/api/v1/auth/ws-ticket",
                headers={"X-CSRF-Token": csrf_token},
            )
            assert ticket_resp.status_code == 200
            ticket = ticket_resp.json()["ticket"]
            assert len(ticket) > 20

            # 4. Control Plane: Trip Kill Switch (requires CSRF)
            # Trip without CSRF must fail
            trip_fail = client.post(
                "/api/v1/control/kill-switch/trip",
                json={"reason": "Manual operator emergency halt"},
            )
            assert trip_fail.status_code == 403

            # Trip with CSRF must succeed
            trip_ok = client.post(
                "/api/v1/control/kill-switch/trip",
                json={"reason": "Manual operator emergency halt"},
                headers={"X-CSRF-Token": csrf_token},
            )
            assert trip_ok.status_code == 200
            assert trip_ok.json()["kill_switch_latched"] is True

            # 5. Control Plane: Reset Kill Switch (requires Step-Up password)
            # Reset with bad password must fail
            reset_fail = client.post(
                "/api/v1/control/kill-switch/reset",
                json={"step_up_password": "WrongPassword!", "reason": "Attempted clear"},
                headers={"X-CSRF-Token": csrf_token},
            )
            assert reset_fail.status_code == 401

            # Reset with correct step-up password must enqueue KILL_SWITCH_RESET_REQUEST
            reset_ok = client.post(
                "/api/v1/control/kill-switch/reset",
                json={"step_up_password": password, "reason": "All venue checks verified normal"},
                headers={"X-CSRF-Token": csrf_token},
            )
            assert reset_ok.status_code == 200
            assert reset_ok.json()["status"] == "REQUESTED"
            cmd_id = uuid.UUID(reset_ok.json()["command_id"])
            cmd_row = await pool.fetchrow("SELECT * FROM execution_commands WHERE command_id = $1;", cmd_id)
            assert cmd_row is not None
            assert cmd_row["command_type"] == "KILL_SWITCH_RESET_REQUEST"

            # 6. Control Plane: Risk Limits Update (refused: not configurable here)
            limits_resp = client.post(
                "/api/v1/control/risk-limits",
                json={
                    "step_up_password": password,
                    "max_daily_loss_pct": 2.5,
                    "max_order_notional_usd": 15000.0,
                    "max_position_notional_usd": 50000.0,
                },
                headers={"X-CSRF-Token": csrf_token},
            )
            # Limits are not configurable here (R1.8): an honest 501 after auth, CSRF and step-up
            assert limits_resp.status_code == 501
            assert limits_resp.json()["detail"] == "Limits are not configurable here; edit the mandate"

            # 7. Logout
            logout_resp = client.post("/api/v1/auth/logout")
            assert logout_resp.status_code == 200

            # 8. Subsequent /me should return 401
            unauth_resp = client.get("/api/v1/auth/me")
            assert unauth_resp.status_code == 401
    finally:
        await redis.aclose()
        await pool.close()


@pytest.mark.asyncio
async def test_session_idle_timeout():
    """Verify session automatically invalidates after idle timeout threshold."""
    import asyncio
    pool = await asyncpg.create_pool(DB_URL)
    redis = await aioredis.from_url(REDIS_URL)
    try:
        # Configure short idle timeout of 0.05 seconds
        auth_mgr = OperatorAuthManager(
            db_pool=pool,
            redis_client=redis,
            idle_timeout_secs=0.05,
        )
        user_id = uuid.uuid4()
        session_id, _ = await auth_mgr.create_session(user_id, "idle_test_user")

        # Immediate check succeeds
        assert await auth_mgr.get_session(session_id) is not None

        # Wait past idle timeout
        await asyncio.sleep(0.08)

        # Inactivity causes idle expiry
        assert await auth_mgr.get_session(session_id) is None
    finally:
        await redis.aclose()
        await pool.close()
