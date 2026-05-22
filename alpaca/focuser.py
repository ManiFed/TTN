"""
ALPACA Focuser device wrapper.

Covers the IFocuserV3 interface subset needed for absolute and relative moves.
"""

import logging

from .client import AlpacaClient

logger = logging.getLogger(__name__)


class Focuser:
    def __init__(self, host: str, port: int, device_number: int = 0, api_version: int = 1):
        self._c = AlpacaClient(host, port, "focuser", device_number, api_version)

    def connect(self) -> None:
        self._c.connect()
        logger.info("Focuser connected: %s", self._c.name())

    def disconnect(self) -> None:
        self._c.disconnect()
        logger.info("Focuser disconnected")

    def position(self) -> int:
        return int(self._c._get("position"))

    def is_moving(self) -> bool:
        return bool(self._c._get("ismoving"))

    def move(self, position: int) -> None:
        """Move to an absolute step position and wait for completion."""
        logger.info("Focuser moving to position %d", position)
        self._c._put("move", Position=position)
        self._c.wait_for(lambda: not self.is_moving(), timeout=60, label="focuser move")
        logger.info("Focuser at position %d", self.position())

    def halt(self) -> None:
        self._c._put("halt")
        logger.warning("Focuser halted")
