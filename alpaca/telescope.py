"""
ALPACA Telescope device wrapper.

Covers the subset of the ITelescopeV3 interface needed for basic slew,
park, and tracking operations.
"""

import logging
import time

from .client import AlpacaClient, AlpacaError

logger = logging.getLogger(__name__)

# Seestar firmware sometimes answers GET rightascension/declination with
# ErrorNumber 1279 "The given key 'RA' was not present in the dictionary"
# (its position cache is mid-refresh). That is a transient read, not a lost
# connection (Starfront 2026-09-26, related #133): retry after a short backoff.
_POSITION_RETRY_DELAYS_S = (0.25, 0.75)


def is_transient_position_error(exc: BaseException) -> bool:
    """True for the Seestar 1279 missing-key position read (see above)."""
    if not isinstance(exc, AlpacaError):
        return False
    return (getattr(exc, "code", 0) == 1279
            and "not present in the dictionary" in str(exc).lower())


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

    def _get_position(self, attribute: str) -> float:
        for delay in _POSITION_RETRY_DELAYS_S:
            try:
                return float(self._c._get(attribute))
            except AlpacaError as exc:
                if not is_transient_position_error(exc):
                    raise
                logger.info("%s read hit transient Seestar 1279 (%s) — retrying in %.2fs",
                            attribute, exc, delay)
                time.sleep(delay)
        return float(self._c._get(attribute))

    def ra(self) -> float:
        return self._get_position("rightascension")

    def dec(self) -> float:
        return self._get_position("declination")

    # --- commands ------------------------------------------------------------

    def set_tracking(self, enabled: bool) -> None:
        self._c._put("tracking", Tracking=enabled)
        logger.info("Tracking set to %s", enabled)

    def begin_slew(self, ra: float, dec: float) -> None:
        """Issue the async slew command and return immediately without polling."""
        self._c._put("slewtocoordinatesasync", timeout=30, RightAscension=ra, Declination=dec)
        logger.info("Slew commanded: RA=%.4f h  Dec=%.4f °", ra, dec)

    def begin_slew_altaz(self, alt: float, az: float) -> None:
        """Slew to alt-az coordinates asynchronously (alt-az mounts or fixed-point goto)."""
        self._c._put("slewtoaltazasync", timeout=30, Altitude=alt, Azimuth=az)
        logger.info("Alt-Az slew commanded: Alt=%.4f °  Az=%.4f °", alt, az)

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

        # Some drivers return from the async PUT before the mount has begun
        # moving, so is_slewing() may read False for a brief window right after
        # the command is accepted.  Wait up to 5 s for slewing to go True first;
        # if it never does the mount either didn't move or was already there.
        slew_started = self._c.wait_for_either(
            lambda: self.is_slewing(), timeout=5, label="slew start"
        )
        if not slew_started:
            end_ra, end_dec = self.ra(), self.dec()
            logger.warning(
                "Mount never reported Slewing=True — it may not have moved. "
                "Position after command: RA=%.4f h  Dec=%.4f °", end_ra, end_dec
            )
            return

        self._c.wait_for(lambda: not self.is_slewing(), timeout=120, label="slew complete")
        end_ra, end_dec = self.ra(), self.dec()
        logger.info(
            "Slew complete — RA=%.4f h  Dec=%.4f °  (ΔRA=%.4f h  ΔDec=%.4f °)",
            end_ra, end_dec, end_ra - start_ra, end_dec - start_dec,
        )

    def verify_movement(self, ra_delta: float = 0.1, dec_delta: float = 0.0) -> bool:
        """
        Slew by a small delta, confirm the mount actually moved, then return to
        the start position.  Intended for daytime / remote sanity checks.
        Returns True if movement was confirmed.
        """
        start_ra, start_dec = self.ra(), self.dec()
        print("\n=== Movement Verification ===")
        print(f"Start position:   RA={start_ra:.4f} h   Dec={start_dec:.4f} °")
        print(f"Commanding slew:  ΔRA={ra_delta:+.4f} h  ΔDec={dec_delta:+.4f} °")

        self.slew_to_coordinates(start_ra + ra_delta, start_dec + dec_delta)

        end_ra, end_dec = self.ra(), self.dec()
        actual_ra_delta = abs(end_ra - start_ra)
        actual_dec_delta = abs(end_dec - start_dec)
        moved = actual_ra_delta > 0.0001 or actual_dec_delta > 0.0001

        if moved:
            print(
                f"Result:           PASSED — "
                f"ΔRA={actual_ra_delta:.4f} h  ΔDec={actual_dec_delta:.4f} °"
            )
        else:
            print(
                f"Result:           FAILED — position unchanged after slew "
                f"(ΔRA={actual_ra_delta:.6f} h  ΔDec={actual_dec_delta:.6f} °)"
            )

        print("Returning to start position…")
        self.slew_to_coordinates(start_ra, start_dec)
        print("=== Verification complete ===\n")
        return moved

    def move_axis(self, axis: int, rate: float) -> None:
        """Move the specified axis at rate (deg/s). rate=0 stops movement."""
        self._c._put("moveaxis", Axis=axis, Rate=rate)
        logger.debug("MoveAxis axis=%d rate=%.4f deg/s", axis, rate)

    def park(self) -> None:
        logger.info("Parking telescope…")
        self._c._put("park", timeout=180)
        self._c.wait_for(self.is_parked, timeout=180, label="park complete")
        logger.info("Telescope parked")

    def unpark(self) -> None:
        self._c._put("unpark")
        logger.info("Telescope unparked")
