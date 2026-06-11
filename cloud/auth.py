#!/usr/bin/env python3
"""
Member authentication for the Boundless Skies cloud.

Token-based: each user gets a bearer token (secrets.token_urlsafe(32)) stored
as a SHA-256 hash in the users table.  Token is returned on register/login and
passed as "Authorization: Bearer <token>" or the "X-Auth-Token" header.

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
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from flask import jsonify, request

from cloud import db

logger = logging.getLogger("cloud.auth")

_PBKDF2_ITERATIONS = 260_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
    email = email.strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("invalid email address")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    if db.query_one("SELECT user_id FROM users WHERE email = ?", (email,)):
        raise ValueError("email already registered")

    user_id = f"u_{secrets.token_hex(8)}"
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    db.execute(
        """INSERT INTO users
               (user_id, email, password_hash, salt, auth_token_hash, created_at, last_login)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, email, pw_hash, salt, token_hash, _now(), _now()),
    )
    db.execute(
        "INSERT INTO members (user_id, display_name, created_at) VALUES (?,?,?)",
        (user_id, display_name.strip() or email.split("@")[0], _now()),
    )
    logger.info("New member registered: %s (%s)", user_id, email)
    return {"user_id": user_id, "token": token}


def login(email: str, password: str) -> dict:
    """
    Verify credentials and issue a fresh bearer token.
    Raises ValueError on bad credentials (deliberately vague error to prevent enumeration).
    """
    email = email.strip().lower()
    row = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    if row is None:
        # Constant-time dummy check to prevent timing enumeration
        _hash_password("dummy", "dummy")
        raise ValueError("invalid email or password")

    pw_hash = _hash_password(password, row["salt"])
    if not secrets.compare_digest(pw_hash, row["password_hash"]):
        raise ValueError("invalid email or password")

    token = secrets.token_urlsafe(32)
    db.execute(
        "UPDATE users SET auth_token_hash = ?, last_login = ? WHERE user_id = ?",
        (_hash_token(token), _now(), row["user_id"]),
    )
    logger.info("Member login: %s", row["user_id"])
    return {"user_id": row["user_id"], "token": token}


def verify_token(token: str) -> Optional[dict]:
    """Return the user row if the bearer token is valid, else None."""
    if not token:
        return None
    return db.query_one(
        "SELECT * FROM users WHERE auth_token_hash = ?", (_hash_token(token),)
    )


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


def _extract_token() -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Auth-Token") or None
