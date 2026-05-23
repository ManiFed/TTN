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
        start_ra, start_dec = self.ra(), self.dec()
        logger.info("Current position  RA=%.4f h  Dec=%.4f °", start_ra, start_dec)
        logger.info("Slewing to        RA=%.4f h  Dec=%.4f °", ra, dec)

        self._c._put("slewtocoordinatesasync", timeout=120, RightAscension=ra, Declination=dec)

        # Two driver behaviours exist:
        #   Blocking PUT  — server holds the connection open until the mount
        #                   stops, then responds.  is_slewing() is already False
        #                   when we reach here; nothing more to wait for.
        #   Truly-async   — server responds immediately and the mount starts
        #                   moving.  is_slewing() is True; wait for it to clear.
        if self.is_slewing():
            self._c.wait_for(lambda: not self.is_slewing(), timeout=120, label="slew complete")

        end_ra, end_dec = self.ra(), self.dec()
        delta_ra, delta_dec = end_ra - start_ra, end_dec - start_dec

        # Warn if position barely changed (threshold: ~4 arcsec in RA, 0.01° in Dec)
        if abs(delta_ra) < 0.001 and abs(delta_dec) < 0.01:
            logger.warning(
                "Slew reported complete but position barely changed "
                "(ΔRA=%+.4f h  ΔDec=%+.4f °) — target may already have been reached.",
                delta_ra, delta_dec,
            )
        else:
            logger.info(
                "Slew complete — RA=%.4f h  Dec=%.4f °  (ΔRA=%+.4f h  ΔDec=%+.4f °)",
                end_ra, end_dec, delta_ra, delta_dec,
            )

    def park(self) -> None:
        logger.info("Parking telescope…")
        self._c._put("park", timeout=300)
        self._c.wait_for(self.is_parked, timeout=300, label="park complete")
        logger.info("Telescope parked")

    def unpark(self) -> None:
        self._c._put("unpark")
        logger.info("Telescope unparked")
