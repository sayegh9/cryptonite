"""Authentication API routes for operator users."""

import os
from typing import Optional
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

from cryptonite_api.auth import OperatorAuthManager

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

COOKIE_NAME = "cryptonite_session"
IS_SECURE_COOKIE = os.getenv("SECURE_COOKIE", "false").lower() in ("true", "1", "yes")


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


class LoginResponse(BaseModel):
    success: bool
    username: str
    csrf_token: str


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in_secs: int


async def get_auth_manager(request: Request) -> OperatorAuthManager:
    """Dependency injecting OperatorAuthManager from app state."""
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if not auth_mgr:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication manager not initialized",
        )
    return auth_mgr


async def get_current_session(
    request: Request,
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    session_id: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict:
    """Base dependency validating only session presence."""
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication session cookie required",
        )
    session = await auth_mgr.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )
    return session

async def require_auth(
    request: Request,
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    session_id: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token"),
) -> dict:
    """Unified auth dependency validating cookie or machine token without CSRF."""
    if x_api_token:
        # Machine token path
        import hashlib
        token_hash_hex = hashlib.sha256(x_api_token.encode('utf-8')).hexdigest()
        async with auth_mgr._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, scopes FROM machine_api_tokens WHERE token_hash = $1 AND is_active = true AND (expires_at IS NULL OR expires_at > now())",
                token_hash_hex
            )
            if not row:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired machine token")
            # Update last_used_at async without blocking
            await conn.execute("UPDATE machine_api_tokens SET last_used_at = now() WHERE id = $1", row['id'])
            return {"type": "machine_token", "scopes": row["scopes"], "user_id": None, "role": "machine"}
    
    if session_id:
        session = await auth_mgr.get_session(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
        session["type"] = "cookie"
        return session
    
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

async def require_operator(
    auth_data: dict = Depends(require_auth)
) -> dict:
    """Requires operator role (cookie) or operator scope (machine)."""
    if auth_data["type"] == "machine_token":
        if "operator" not in auth_data.get("scopes", []):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient machine token scopes")
    else:
        if auth_data.get("role") not in ("operator", "admin"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient privileges")
    return auth_data

async def require_csrf(
    request: Request,
    auth_data: dict = Depends(require_auth),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token")
) -> dict:
    """Requires CSRF only if authenticated via cookie."""
    if auth_data["type"] == "cookie":
        if not x_csrf_token or x_csrf_token != auth_data.get("csrf_token"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token")
    return auth_data


# Route-level dependency sets: `@router.post(..., dependencies=OPERATOR_WRITE)`.
OPERATOR_WRITE = [Depends(require_operator), Depends(require_csrf)]
AUTHED_READ = [Depends(require_auth)]



@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
):
    """Authenticate operator credentials and issue HttpOnly session cookie."""
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")

    user_info = await auth_mgr.authenticate(
        username=req.username,
        password=req.password,
        totp_code=req.totp_code,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    session_id, csrf_token = await auth_mgr.create_session(user_info["id"], user_info["username"], role=user_info.get("role", "operator"))

    response.set_cookie(
        key=COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="strict",
        secure=IS_SECURE_COOKIE,
        max_age=43200,
        path="/",
    )

    return LoginResponse(
        success=True,
        username=user_info["username"],
        csrf_token=csrf_token,
    )


@router.post("/logout")
async def logout(
    response: Response,
    session: dict = Depends(get_current_session),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    session_id: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
):
    """Revoke active session and clear cookie."""
    if session_id:
        await auth_mgr.terminate_session(session_id, username=session.get("username"))
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"success": True}


@router.get("/me")
async def get_me(session: dict = Depends(get_current_session)):
    """Return current authenticated session details."""
    return {
        "authenticated": True,
        "username": session["username"],
        "user_id": session["user_id"],
        "csrf_token": session.get("csrf_token"),
    }


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def issue_ws_ticket(
    session: dict = Depends(get_current_session),
    auth_mgr: OperatorAuthManager = Depends(get_auth_manager),
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
):
    """Issue a single-use 60s WebSocket ticket."""
    if not x_csrf_token or x_csrf_token != session.get("csrf_token"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing CSRF token",
        )

    ticket = await auth_mgr.issue_ws_ticket(
        user_id=session["user_id"],
        username=session["username"],
    )
    return WsTicketResponse(ticket=ticket, expires_in_secs=60)
