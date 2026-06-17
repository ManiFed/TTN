#!/usr/bin/env python3
"""
FCM push notification sender (HTTP v1 API).

Requires a Firebase service account JSON key.  Point config.yaml at it:

    firebase:
      service_account_key: /path/to/serviceAccountKey.json

If the key is absent or google-auth is not installed the module is a no-op —
the rest of the cloud continues normally without push notifications.

To obtain a service account key:
  Firebase Console → Project Settings → Service Accounts → Generate new private key

Install the runtime dependency:
  pip install google-auth
"""

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("cloud.push")

_FCM_SEND_URL = (
    "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
)
_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

# Module-level cache so we only load credentials once.
_credentials = None
_project_id: Optional[str] = None
_unavailable = False   # set True on first failed load so we don't retry


def _load(config: dict) -> bool:
    """Load service-account credentials from config.  Returns True on success."""
    global _credentials, _project_id, _unavailable
    if _unavailable:
        return False
    if _credentials is not None:
        return True

    try:
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        logger.info(
            "google-auth not installed — push notifications disabled. "
            "Install with: pip install google-auth"
        )
        _unavailable = True
        return False

    key_path = config.get("firebase", {}).get("service_account_key", "")
    if not key_path:
        _unavailable = True
        return False

    path = Path(key_path)
    if not path.exists():
        logger.warning("Firebase service account key not found: %s", key_path)
        _unavailable = True
        return False

    try:
        with open(path) as f:
            key_data = json.load(f)
        _project_id = key_data["project_id"]
        _credentials = service_account.Credentials.from_service_account_info(
            key_data, scopes=_SCOPES
        )
        logger.info("FCM credentials loaded for project: %s", _project_id)
        return True
    except Exception as exc:
        logger.warning("Failed to load Firebase credentials: %s", exc)
        _unavailable = True
        return False


def _access_token(config: dict) -> Optional[str]:
    """Return a valid OAuth2 bearer token, refreshing if needed."""
    if not _load(config):
        return None
    try:
        from google.auth.transport.requests import Request as GoogleRequest  # type: ignore
        if not _credentials.valid:
            _credentials.refresh(GoogleRequest())
        return _credentials.token
    except Exception as exc:
        logger.warning("FCM token refresh failed: %s", exc)
        return None


def send(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    config: Optional[dict] = None,
) -> bool:
    """
    Send one FCM push notification.

    Returns True on success, False on any failure (including not configured).
    Always safe to call — logs warnings rather than raising.
    """
    if not fcm_token:
        return False

    token = _access_token(config or {})
    if token is None or _project_id is None:
        return False

    message: dict = {
        "token": fcm_token,
        "notification": {"title": title, "body": body},
    }
    if data:
        # FCM data values must be strings.
        message["data"] = {k: str(v) for k, v in data.items()}

    payload = json.dumps({"message": message}).encode()
    url = _FCM_SEND_URL.format(project_id=_project_id)

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.debug("FCM sent to %s…: HTTP %s", fcm_token[:12], resp.status)
            return True
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        logger.warning(
            "FCM HTTP %s for token %s…: %s",
            exc.code, fcm_token[:12], body_bytes[:200],
        )
        return False
    except Exception as exc:
        logger.warning("FCM send failed for token %s…: %s", fcm_token[:12], exc)
        return False
