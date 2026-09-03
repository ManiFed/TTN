"""HTTP clients for the two backends the MCP tools sit on top of.

Mirrors app/lib/api/api_client.dart and app/lib/api/node_agent_client.dart so
that a tool and the equivalent screen in the Flutter app hit the same endpoint
with the same shape. Error text is normalised into ApiError, whose message is
what the model (and therefore the member) ends up reading — so it is written
for a person, not a stack trace.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

from mcp.server.mcpserver.exceptions import ToolError

from . import session_store

#: Matches AppConfig._productionBase / apiPrefix in app/lib/config.dart.
DEFAULT_CLOUD_BASE = "https://api.thetelescope.net"
API_PREFIX = "/api/v1"

#: The node agent's local JSON API (src/dashboard.py binds 127.0.0.1:5173).
DEFAULT_AGENT_BASE = "http://127.0.0.1:5173"

#: Same 10s cap the app uses, so a dead API fails fast instead of hanging.
CLOUD_TIMEOUT = 10.0


class ApiError(ToolError):
    """A non-2xx response, carrying the server's own message where it gave one.

    Subclassing ToolError (rather than a plain RuntimeError) is what makes that
    message — rather than a generic "Error executing tool ..." — reach the model."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def unauthorized(self) -> bool:
        return self.status == 401


class CloudClient:
    """Member/admin client for cloud/server.py.

    The token is never returned by a tool: a bearer token in a chat
    transcript is a credential leak, so `auth_login` reports success and
    nothing else. After sign-in it is also written to a user-only file
    bound to this client's cloud origin (see session_store) so an MCP
    process restart does not silently log the member out.
    `TELESCOPE_MCP_TOKEN` still wins, and is not copied to disk.
    Shared HTTP mode must pass persist=False: one process, many clients.
    """

    def __init__(self, base: str | None = None, token: str | None = None, *,
                 persist: bool = True, session_path=None):
        self.base = (base or os.environ.get("TELESCOPE_MCP_CLOUD_BASE")
                     or DEFAULT_CLOUD_BASE).rstrip("/")
        self._persist = persist
        self._session_path = Path(session_path) if session_path else None
        env_token = os.environ.get("TELESCOPE_MCP_TOKEN") or None
        stored = None
        if persist and not token and not env_token:
            stored = session_store.load(self._session_path, expected_base=self.base)
        self._token = token or env_token or stored
        self._admin_key = os.environ.get("TELESCOPE_MCP_ADMIN_KEY") or None
        self._lock = threading.Lock()
        self._session = requests.Session()

    # ── auth state ────────────────────────────────────────────
    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def set_token(self, token: str | None) -> None:
        with self._lock:
            self._token = token
            if not self._persist:
                return
            if token:
                session_store.save(token, self._session_path, cloud_base=self.base)
            else:
                session_store.clear(self._session_path)

    def _headers(self, admin: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if admin and self._admin_key:
            headers["X-Admin-Key"] = self._admin_key
        return headers

    # ── transport ─────────────────────────────────────────────
    def _request(self, method: str, path: str, *,
                 params: Optional[dict] = None,
                 body: Optional[dict] = None,
                 admin: bool = False) -> dict:
        url = f"{self.base}{API_PREFIX}{path}"
        try:
            resp = self._session.request(
                method, url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                data=json.dumps(body) if body is not None else None,
                headers=self._headers(admin=admin),
                timeout=CLOUD_TIMEOUT,
            )
        except requests.Timeout:
            raise ApiError(504, f"The cloud did not respond within {CLOUD_TIMEOUT:.0f}s.")
        except requests.RequestException as exc:
            raise ApiError(0, f"Could not reach the cloud API: {exc}")
        try:
            return _decode(resp)
        except ApiError as exc:
            # Admin endpoints 401 for a missing/wrong X-Admin-Key even when
            # the member bearer is still valid. Only a non-admin 401 means
            # the session itself died.
            if exc.unauthorized and self._token and not admin:
                self.set_token(None)
                detail = (exc.message.rstrip(".") + ". "
                          if exc.message else "")
                raise ApiError(401, (
                    f"{detail}This chat lost its session. "
                    "Sign in again with sign_in."
                )) from exc
            raise

    def get(self, path: str, params: dict | None = None, admin: bool = False) -> dict:
        return self._request("GET", path, params=params, admin=admin)

    def post(self, path: str, body: dict | None = None, admin: bool = False) -> dict:
        return self._request("POST", path, body=body or {}, admin=admin)

    def put(self, path: str, body: dict | None = None, admin: bool = False) -> dict:
        return self._request("PUT", path, body=body or {}, admin=admin)

    def patch(self, path: str, body: dict | None = None, admin: bool = False) -> dict:
        return self._request("PATCH", path, body=body or {}, admin=admin)

    def delete(self, path: str, body: dict | None = None, admin: bool = False) -> dict:
        return self._request("DELETE", path, body=body, admin=admin)


class AgentClient:
    """Client for the node agent running on this computer.

    Unreachability is the common case, not an exception: the agent may still be
    starting, or busy on a slow endpoint. Those are reported as plain sentences
    a member can act on, the same way NodeAgentClient._guard does in the app.
    """

    def __init__(self, base: str | None = None):
        self.base = (base or os.environ.get("TELESCOPE_MCP_AGENT_BASE")
                     or DEFAULT_AGENT_BASE).rstrip("/")
        self._session = requests.Session()

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict] = None, timeout: float = 8.0) -> Any:
        url = f"{self.base}{path}"
        try:
            resp = self._session.request(
                method, url,
                params=params,
                data=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except requests.Timeout:
            raise ApiError(504, (
                f"The node software on this computer did not respond in time "
                f"({path}). It may still be starting up — try again in a moment."
            ))
        except requests.RequestException:
            raise ApiError(0, (
                f"Could not reach the node software on this computer ({path}). "
                f"Make sure it is running, then try again."
            ))
        return _decode(resp)

    def get(self, path: str, params: dict | None = None, timeout: float = 8.0) -> Any:
        return self._request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, body: dict | None = None, timeout: float = 15.0) -> Any:
        return self._request("POST", path, body=body or {}, timeout=timeout)

    def delete(self, path: str, body: dict | None = None, timeout: float = 15.0) -> Any:
        return self._request("DELETE", path, body=body, timeout=timeout)

    def get_bytes(self, path: str, timeout: float = 20.0) -> tuple[bytes, str]:
        """Fetch a binary body (an image) rather than JSON.

        The image endpoints return raw PNG, and a 404 carries a JSON reason --
        "no stacked preview available yet" is a normal state early in a night,
        not a fault, so it is surfaced as a readable message.
        """
        url = f"{self.base}{path}"
        try:
            resp = self._session.get(url, timeout=timeout)
        except requests.Timeout:
            raise ApiError(504, f"The node did not send the image in time ({path}).")
        except requests.RequestException:
            raise ApiError(0, f"Could not reach the node software ({path}).")
        if resp.status_code == 404:
            reason = "Not available yet."
            try:
                reason = json.loads(resp.text).get("error") or reason
            except ValueError:
                pass
            raise ApiError(404, reason)
        if not (200 <= resp.status_code < 300):
            raise ApiError(resp.status_code, f"Request failed ({resp.status_code}).")
        return resp.content, resp.headers.get("Content-Type", "image/png")


def _decode(resp) -> Any:
    """Turn a response into JSON, or into an ApiError carrying a readable reason."""
    text = resp.text or ""
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        if 200 <= resp.status_code < 300:
            raise ApiError(resp.status_code, "Unexpected response from server.")
        payload = {}
    if not (200 <= resp.status_code < 300):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("error") or "")
        raise ApiError(resp.status_code, message or f"Request failed ({resp.status_code}).")
    return payload


def encode_path(value: str) -> str:
    """Percent-encode one path segment, matching Uri.encodeComponent in the app."""
    return quote(str(value), safe="")
