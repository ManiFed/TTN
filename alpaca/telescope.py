"""
ALPACA Telescope device wrapper.

Covers the subset of the ITelescopeV3 interface needed for basic slew,
park, and tracking operations.
"""

import logging

from .client import AlpacaClient

logger = logging.getLogger(__name__)


class Telescope:
    def __init__(self, host: str, port: int, device_number: int = 0, api_version: int = 1):
        self._c = AlpacaClient(host, port, "telescope", device_number, api_version)

    # --- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        self._c.connect()
        name = self._c.name()
        logger.info("Telescope connected: %s", name)

    def disconnect(self) -> None:
        self._c.disconnect()
        logger.info("Telescope disconnected")

    # --- state queries -------------------------------------------------------

    def is_slewing(self) -> bool:
        return bool(self._c._get("slewing"))

    def is_parked(self) -> bool:
        return bool(self._c._get("atpark"))

    def is_tracking(self) -> bool:
        return bool(self._c._get("tracking"))

    def ra(self) -> float:
        return float(self._c._get("rightascension"))

    def dec(self) -> float:
        return float(self._c._get("declination"))

    # --- commands ------------------------------------------------------------

    def set_tracking(self, enabled: bool) -> None:
        self._c._put("tracking", Tracking=enabled)
        logger.info("Tracking set to %s", enabled)

    def slew_to_coordinates(self, ra: float, dec: float) -> None:
        """
        Slew to equatorial coordinates and wait until the mount stops moving.

        ra  – Right ascension in decimal hours (0–24)
        dec – Declination in decimal degrees (-90 to +90)
        """
        logger.info("Slewing to RA=%.4f h  Dec=%.4f °", ra, dec)
        self._c._put("slewtocoordinatesasync", RightAscension=ra, Declination=dec)
        self._c.wait_for(lambda: not self.is_slewing(), timeout=120, label="slew complete")
        logger.info("Slew complete — RA=%.4f h  Dec=%.4f °", self.ra(), self.dec())

    def park(self) -> None:
        logger.info("Parking telescope…")
        self._c._put("park")
        self._c.wait_for(self.is_parked, timeout=180, label="park complete")
        logger.info("Telescope parked")

    def unpark(self) -> None:
        self._c._put("unpark")
        logger.info("Telescope unparked")
