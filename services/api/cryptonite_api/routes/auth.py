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
    """Dependency verifying active session cookie."""
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

    session_id, csrf_token = await auth_mgr.create_session(user_info["id"], user_info["username"])

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
