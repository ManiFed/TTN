"""
Low-level ALPACA HTTP client.

Wraps the REST calls defined in the ALPACA API spec so that device modules
never deal with raw HTTP or JSON parsing.
"""

import itertools
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
        self._client_id = id(self) & 0xFFFF

    def _get(self, attribute: str, timeout: float = 10, **params) -> Any:
        url = f"{self.base_url}/{attribute}"
        params["ClientID"] = self._client_id
        params["ClientTransactionID"] = _next_transaction_id()
        response = self.session.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        self._check_error(attribute, body)
        return body["Value"]

    def _put(self, action: str, timeout: float = 10, **data) -> None:
        url = f"{self.base_url}/{action}"
        data["ClientID"] = self._client_id
        data["ClientTransactionID"] = _next_transaction_id()
        response = self.session.put(url, data=data, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        self._check_error(action, body)

    @staticmethod
    def _check_error(endpoint: str, body: dict) -> None:
        code = body.get("ErrorNumber", 0)
        if code != 0:
            raise AlpacaError(f"{endpoint} → ErrorNumber {code}: {body.get('ErrorMessage', '')}")

    def connected(self) -> bool:
        return self._get("connected")

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
