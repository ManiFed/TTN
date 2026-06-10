"""
ALPACA CoverCalibrator device wrapper.

Controls the optical arm (cover) on the Seestar S50 and compatible devices.
Cover state values follow the ALPACA ITelescopeV3 CoverStatus enum.
"""

import logging

from .client import AlpacaClient

logger = logging.getLogger(__name__)

COVER_NOT_PRESENT = 0
COVER_CLOSED      = 1
COVER_MOVING      = 2
COVER_OPEN        = 3
COVER_UNKNOWN     = 4
COVER_ERROR       = 5

COVER_STATE_NAMES = {
    COVER_NOT_PRESENT: "Not Present",
    COVER_CLOSED:      "Closed",
    COVER_MOVING:      "Moving",
    COVER_OPEN:        "Open",
    COVER_UNKNOWN:     "Unknown",
    COVER_ERROR:       "Error",
}


class CoverCalibrator:
    def __init__(self, host: str, port: int, device_number: int = 0, api_version: int = 1):
        self._c = AlpacaClient(host, port, "covercalibrator", device_number, api_version)

    def connect(self) -> None:
        self._c.connect()
        try:
            name = self._c.name()
        except Exception:
            name = "unknown"
        logger.info("CoverCalibrator connected: %s", name)

    def disconnect(self) -> None:
        self._c.disconnect()
        logger.info("CoverCalibrator disconnected")

    def cover_state(self) -> int:
        """Return current cover state (COVER_* constants)."""
        return int(self._c._get("coverstate"))

    def open_cover(self) -> None:
        self._c._put("opencover")
        logger.info("Cover open commanded")

    def close_cover(self) -> None:
        self._c._put("closecover")
        logger.info("Cover close commanded")

    def halt_cover(self) -> None:
        self._c._put("haltcover")
        logger.info("Cover halt commanded")
