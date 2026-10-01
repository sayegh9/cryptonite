"""Notifier contract (AI-first blueprint §4.6, A5.4): request signing and Telegram button data.

Stdlib only. The notifier signs its calls to ``POST /api/v1/ai/notifier/approvals/{id}`` with a shared
secret (``NOTIFIER_HMAC_SECRET``); the API verifies the signature, the timestamp window and a
single-use nonce. Telegram button payloads carry a short HMAC tag, so a modified client cannot craft a
decision for an approval it was never shown.
"""

import hashlib
import hmac
import secrets
import time
import uuid

TS_HEADER = "X-Notifier-Timestamp"
NONCE_HEADER = "X-Notifier-Nonce"
SIG_HEADER = "X-Notifier-Signature"
MAX_SKEW_S = 60

CALLBACK_ACTIONS = {"a": "APPROVE", "v": "VETO", "d": "DETAILS"}


def _digest(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _message(method: str, path: str, body: bytes, ts: str, nonce: str) -> str:
    return f"{method.upper()}:{path}:{hashlib.sha256(body).hexdigest()}:{ts}:{nonce}"


def sign_request(secret: str, method: str, path: str, body: bytes, *, ts: int | None = None,
                 nonce: str | None = None) -> dict[str, str]:
    ts_s = str(int(ts if ts is not None else time.time()))
    nonce = nonce or secrets.token_hex(12)
    return {TS_HEADER: ts_s, NONCE_HEADER: nonce, SIG_HEADER: _digest(secret, _message(method, path, body, ts_s, nonce))}


def verify_request(secret: str, method: str, path: str, body: bytes, headers, *, now: float | None = None) -> str | None:
    """The nonce when the signature is valid and fresh (the caller must reject a reused nonce), else None."""
    if not secret:
        return None
    ts, nonce, sig = headers.get(TS_HEADER), headers.get(NONCE_HEADER), headers.get(SIG_HEADER)
    if not ts or not nonce or not sig or len(nonce) > 64:
        return None
    try:
        skew = abs((now if now is not None else time.time()) - int(ts))
    except ValueError:
        return None
    if skew > MAX_SKEW_S:
        return None
    if not hmac.compare_digest(sig, _digest(secret, _message(method, path, body, ts, nonce))):
        return None
    return nonce


def callback_data(secret: str, action: str, approval_id: uuid.UUID | str) -> str:
    """``a:<hex32>:<tag>`` (43 bytes; Telegram allows 64)."""
    hex_id = uuid.UUID(str(approval_id)).hex
    return f"{action}:{hex_id}:{_digest(secret, f'{action}:{hex_id}')[:8]}"


def parse_callback_data(secret: str, data: str) -> tuple[str, uuid.UUID] | None:
    """(APPROVE | VETO | DETAILS, approval id), or None for anything malformed or forged."""
    try:
        action, hex_id, tag = data.split(":")
        approval_id = uuid.UUID(hex=hex_id)
    except (ValueError, AttributeError):
        return None
    if action not in CALLBACK_ACTIONS or not hmac.compare_digest(tag, _digest(secret, f"{action}:{hex_id}")[:8]):
        return None
    return CALLBACK_ACTIONS[action], approval_id
