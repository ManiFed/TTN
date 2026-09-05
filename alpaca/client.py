"""
Low-level ALPACA HTTP client.

Wraps the REST calls defined in the ALPACA API spec so that device modules
never deal with raw HTTP or JSON parsing.
"""

import itertools
import json
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_client_transaction_id = itertools.count(1)


def _next_transaction_id() -> int:
    return next(_client_transaction_id)


class AlpacaError(Exception):
    """Raised when the ALPACA server returns a non-zero ErrorNumber."""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code

# ASCOM/ALPACA standard error number for "this member is not implemented in
# this driver" -- e.g. Seestar's Park, which several callers need to treat as
# "no such capability" rather than a transient fault.
NOT_IMPLEMENTED = 1024


def _parse_json_body(response: requests.Response, endpoint: str) -> dict:
    """Parse an ALPACA JSON body, or raise AlpacaError for empty/non-JSON.

    ``requests.Response.json()`` surfaces empty bodies as the raw
    ``json.loads`` message ``Expecting value: line 1 column 1 (char 0)``.
    Auto-centering and MCP callers need a structured device/endpoint error
    instead (issue #59).
    """
    raw = response.content or b""
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if not text.strip():
        raise AlpacaError(
            f"{endpoint}: empty ALPACA HTTP body "
            f"(HTTP {response.status_code}, Content-Length="
            f"{response.headers.get('Content-Length', 'missing')})",
            code=0,
        )
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:120].replace("\n", "\\n")
        raise AlpacaError(
            f"{endpoint}: non-JSON ALPACA HTTP body "
            f"(HTTP {response.status_code}, {len(text)} bytes, "
            f"json error: {exc}; preview={preview!r})",
            code=0,
        ) from exc
    if not isinstance(body, dict):
        raise AlpacaError(
            f"{endpoint}: ALPACA JSON body was {type(body).__name__}, expected object",
            code=0,
        )
    return body


class AlpacaClient:
    """
    Thin HTTP wrapper around a single ALPACA device endpoint.

    Base URL pattern: http://<host>:<port>/api/v<version>/<device_type>/<device_number>/
    """

    def __init__(self, host: str, port: int, device_type: str, device_number: int, api_version: int = 1):
        self.base_url = (
            f"http://{host}:{port}/api/v{api_version}/{device_type}/{device_number}"
        )
        self.session = requests.Session()
        # Separate session reserved for the safety watchdog's heartbeat so its
        # polling never contends with in-flight device traffic on the main
        # session's connection pool (requests.Session is not thread-safe).
        self._ping_session: Any = None
        self._client_id = id(self) & 0xFFFF

    def _get(self, attribute: str, timeout: float = 10, **params) -> Any:
        url = f"{self.base_url}/{attribute}"
        params["ClientID"] = self._client_id
        params["ClientTransactionID"] = _next_transaction_id()
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        body = _parse_json_body(response, attribute)
        self._check_error(attribute, body)
        return body["Value"]

    def _put(self, action: str, timeout: float = 10, **data) -> None:
        url = f"{self.base_url}/{action}"
        data["ClientID"] = self._client_id
        data["ClientTransactionID"] = _next_transaction_id()
        response = self.session.put(url, data=data, timeout=timeout)
        response.raise_for_status()
        body = _parse_json_body(response, action)
        self._check_error(action, body)

    @staticmethod
    def _check_error(endpoint: str, body: dict) -> None:
        code = body.get("ErrorNumber", 0)
        if code != 0:
            raise AlpacaError(
                f"{endpoint} → ErrorNumber {code}: {body.get('ErrorMessage', '')}", code=code
            )

    def connected(self) -> bool:
        return self._get("connected")

    def ping(self, timeout: float = 5.0) -> bool:
        """Lightweight liveness check on a dedicated session.

        Mirrors GET /connected but uses a separate requests.Session so a
        background watchdog can poll the device without colliding with the
        main session's in-flight requests from another thread.
        """
        sess = self._ping_session
        if sess is None:
            sess = self._ping_session = requests.Session()
        params = {
            "ClientID": self._client_id,
            "ClientTransactionID": _next_transaction_id(),
        }
        response = sess.get(f"{self.base_url}/connected", params=params, timeout=timeout)
        response.raise_for_status()
        body = _parse_json_body(response, "connected")
        self._check_error("connected", body)
        return bool(body["Value"])

    def connect(self) -> None:
        logger.debug("%s: connecting", self.base_url)
        self._put("connected", Connected=True)

    def disconnect(self) -> None:
        logger.debug("%s: disconnecting", self.base_url)
        self._put("connected", Connected=False)

    def name(self) -> str:
        return self._get("name")

    def description(self) -> str:
        return self._get("description")

    def wait_for(self, predicate, poll_interval: float = 0.5, timeout: float = 120.0, label: str = "") -> None:
        """Poll *predicate* (a zero-arg callable) until it returns True or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(poll_interval)
        raise TimeoutError(f"Timed out waiting for: {label or predicate}")

    def wait_for_either(self, predicate, poll_interval: float = 0.5, timeout: float = 5.0, label: str = "") -> bool:
        """Like wait_for but returns True if predicate succeeds, False on timeout (no exception)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll_interval)
        logger.debug("wait_for_either: timed out waiting for %s", label or predicate)
        return False
