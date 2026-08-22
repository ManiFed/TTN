"""Sign in by opening a browser, so no password ever passes through a tool call.

A chat interface must not take a password as an argument: it would be written
into the transcript, the model's context, and any logging in between, and no
later deletion recalls it. But the app is going away, so "create your account
in the app" is not an answer either.

This is the device-authorization pattern, simplified by the fact that the
member is sitting at the machine. The agent asks for a link, the member opens
it, signs in or signs up in a real browser against the real cloud, and the
agent polls until a session appears. The password only ever exists between the
browser and the server.

The code in the link is the secret, so it is long, single-use, and short-lived:
a link that stays valid is a password that never expires.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db

logger = logging.getLogger("cloud.browser_auth")

#: Long enough that guessing is hopeless; this is the whole credential.
_CODE_BYTES = 32

#: A link nobody used within this is dead. Ten minutes is long enough to find
#: your password manager and short enough that a leaked URL is worthless.
LINK_TTL_MINUTES = 10

#: What the agent should wait between polls.
POLL_INTERVAL_S = 2

PENDING, APPROVED, CONSUMED, EXPIRED = "pending", "approved", "consumed", "expired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def start(origin: str) -> dict:
    """Begin a browser sign-in. Returns the link for the member to open."""
    code = secrets.token_urlsafe(_CODE_BYTES)
    now = _now()
    expires = now + timedelta(minutes=LINK_TTL_MINUTES)
    db.execute(
        """INSERT INTO auth_browser_sessions
               (code, status, created_at, expires_at)
           VALUES (%s,%s,%s,%s)""",
        (code, PENDING, _iso(now), _iso(expires)),
    )
    return {
        "code": code,
        "url": f"{origin.rstrip('/')}/auth/link?code={code}",
        "expires_at": _iso(expires),
        "poll_interval_s": POLL_INTERVAL_S,
    }


def _row(code: str) -> Optional[dict]:
    return db.query_one(
        "SELECT * FROM auth_browser_sessions WHERE code = %s", (code,))


def _expired(row: dict) -> bool:
    try:
        return _now() >= datetime.fromisoformat(str(row.get("expires_at")))
    except (TypeError, ValueError):
        return True


def approve(code: str, user_id: str, token: str) -> bool:
    """Attach a freshly-issued session to a pending link. Called by the page."""
    row = _row(code)
    if row is None or row.get("status") != PENDING or _expired(row):
        return False
    db.execute(
        """UPDATE auth_browser_sessions
              SET status = %s, user_id = %s, token = %s, approved_at = %s
            WHERE code = %s AND status = %s""",
        (APPROVED, user_id, token, _iso(_now()), code, PENDING),
    )
    return True


def poll(code: str) -> dict:
    """What the agent sees while it waits.

    The token is handed over exactly once. A link that could be replayed would
    be a password sitting in whatever recorded the URL.
    """
    row = _row(code)
    if row is None:
        return {"status": EXPIRED, "detail": "That sign-in link is not valid."}

    status = row.get("status")
    if status == CONSUMED:
        return {"status": CONSUMED,
                "detail": "That link has already been used. Start a new one."}
    if _expired(row) and status != APPROVED:
        return {"status": EXPIRED,
                "detail": f"That link expired after {LINK_TTL_MINUTES} minutes. "
                          f"Start a new one."}
    if status != APPROVED:
        return {"status": PENDING,
                "detail": "Waiting for you to finish signing in."}

    # Read the session out before clearing it. Depending on the row still
    # holding its token after the UPDATE means depending on the driver handing
    # back a snapshot rather than a live cursor -- true of psycopg, but a
    # silent empty token is a miserable thing to debug if it ever stops being.
    token = row.get("token")
    user_id = row.get("user_id")
    db.execute(
        "UPDATE auth_browser_sessions SET status = %s, token = '' WHERE code = %s",
        (CONSUMED, code))
    return {"status": APPROVED, "token": token, "user_id": user_id}


def purge(older_than_hours: int = 24) -> int:
    """Drop spent and expired links. Called by nightly maintenance."""
    cutoff = _iso(_now() - timedelta(hours=older_than_hours))
    try:
        return db.execute(
            "DELETE FROM auth_browser_sessions WHERE created_at < %s", (cutoff,))
    except Exception as exc:
        logger.debug("Could not purge browser auth sessions: %s", exc)
        return 0
