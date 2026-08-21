#!/usr/bin/env python3
"""
Member authentication for the The Telescope Net cloud.

Token-based, multi-session: each login/register issues a bearer token stored
as a SHA-256 hash in the sessions table (one row per signed-in device), so
signing in on the phone doesn't kick the desktop app back to the login
screen and vice versa. Token is returned on register/login and passed as
"Authorization: Bearer <token>" or the "X-Auth-Token" header.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user salt (260 000 rounds).

Public API
----------
    from cloud import auth

    # Flask decorator — passes the user row as the first positional argument
    @auth.require_member
    def my_endpoint(user): ...

    # Direct calls
    auth.register(email, password, display_name)  → {"user_id", "token"}
    auth.login(email, password)                   → {"user_id", "token"}
    auth.verify_token(token)                      → user row dict | None
    auth.logout(token)                             → revokes just this session
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

from flask import jsonify, request

from cloud import db

logger = logging.getLogger("cloud.auth")

_PBKDF2_ITERATIONS = 260_000
_SESSION_TTL_DAYS = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _expiry_from(now: datetime) -> str:
    return (now + timedelta(days=_SESSION_TTL_DAYS)).isoformat()


def _issue_session(user_id: str) -> str:
    """Create a new session row (does not touch any other device's session)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO sessions (token_hash, user_id, created_at, last_used_at, expires_at)
           VALUES (%s,%s,%s,%s,%s)""",
        (_hash_token(token), user_id, now.isoformat(), now.isoformat(), _expiry_from(now)),
    )
    # Opportunistic cleanup — no cron needed, this runs on every sign-in.
    db.execute("DELETE FROM sessions WHERE user_id = %s AND expires_at < %s",
               (user_id, now.isoformat()))
    return token


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()


# ── Registration / login ───────────────────────────────────────────────────────

def register(email: str, password: str, display_name: str = "") -> dict:
    """
    Create a new member account.
    Returns {"user_id", "token"} or raises ValueError.
    """
    if not isinstance(email, str) or not isinstance(password, str) \
            or not isinstance(display_name, str):
        raise ValueError("email, password, and display_name must be strings")
    email = email.strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("invalid email address")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    if db.query_one("SELECT user_id FROM users WHERE email = %s", (email,)):
        raise ValueError("email already registered")

    user_id = f"u_{secrets.token_hex(8)}"
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)

    db.execute(
        """INSERT INTO users
               (user_id, email, password_hash, salt, created_at, last_login)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (user_id, email, pw_hash, salt, _now(), _now()),
    )
    token = _issue_session(user_id)
    db.execute(
        "INSERT INTO members (user_id, display_name, created_at) VALUES (%s,%s,%s)",
        (user_id, display_name.strip() or email.split("@")[0], _now()),
    )
    logger.info("New member registered: %s (%s)", user_id, email)
    return {"user_id": user_id, "token": token}


def login(email: str, password: str) -> dict:
    """
    Verify credentials and issue a fresh bearer token.
    Raises ValueError on bad credentials (deliberately vague error to prevent enumeration).
    """
    if not isinstance(email, str) or not isinstance(password, str):
        raise ValueError("invalid email or password")
    email = email.strip().lower()
    row = db.query_one("SELECT * FROM users WHERE email = %s", (email,))
    if row is None:
        # Constant-time dummy check to prevent timing enumeration
        _hash_password("dummy", "dummy")
        raise ValueError("invalid email or password")

    pw_hash = _hash_password(password, row["salt"])
    if not secrets.compare_digest(pw_hash, row["password_hash"]):
        raise ValueError("invalid email or password")

    db.execute(
        "UPDATE users SET last_login = %s WHERE user_id = %s",
        (_now(), row["user_id"]),
    )
    token = _issue_session(row["user_id"])
    logger.info("Member login: %s", row["user_id"])
    return {"user_id": row["user_id"], "token": token}


def verify_token(token: str) -> Optional[dict]:
    """Return the user row if the bearer token is valid, else None.

    Sliding expiry: every verified request pushes this session's expiry
    another _SESSION_TTL_DAYS out, so an actively-used app is never
    signed out; only one left untouched that long is.
    """
    if not token:
        return None
    token_hash = _hash_token(token)
    row = db.query_one(
        """SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id
           WHERE s.token_hash = %s AND s.expires_at > %s""",
        (token_hash, _now()),
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    db.execute(
        "UPDATE sessions SET last_used_at = %s, expires_at = %s WHERE token_hash = %s",
        (now.isoformat(), _expiry_from(now), token_hash),
    )
    return row


def logout(token: str) -> None:
    """Revoke just this device's session -- other signed-in devices are unaffected."""
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))


# ── Flask decorator ────────────────────────────────────────────────────────────

def require_member(fn):
    """
    Authenticate via "Authorization: Bearer <token>" or "X-Auth-Token" header.
    Injects the user row dict as the first positional argument.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        user = verify_token(token) if token else None
        if user is None:
            return jsonify({"error": "authentication required"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def require_admin_member(fn):
    """Like require_member, but the authenticated user must have role='admin'.

    Distinct from server.py's require_admin (a static X-Admin-Key shared
    secret with no user identity) — this ties admin actions to a specific
    signed-in account, e.g. so dry-run testing mode can be scoped to named
    admins rather than anyone holding the ops key.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        user = verify_token(token) if token else None
        if user is None:
            return jsonify({"error": "authentication required"}), 401
        if user.get("role") != "admin":
            return jsonify({"error": "admin role required"}), 403
        return fn(user, *args, **kwargs)
    return wrapper


def _extract_token() -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Auth-Token") or None
