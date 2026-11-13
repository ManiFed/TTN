"""
ALPACA Telescope device wrapper.

Covers the subset of the ITelescopeV3 interface needed for basic slew,
park, and tracking operations.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .client import AlpacaClient, AlpacaError, NOT_IMPLEMENTED

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


# ASCOM error numbers a driver uses when a site property is read-only or the
# value is refused. SiteElevation in particular is not settable on every
# Seestar firmware; that must never fail the sync (issue #149).
_NOT_SETTABLE_CODES = (NOT_IMPLEMENTED, 0x401, 0x402, 0x40B)


def format_utc_iso(dt: Optional[datetime] = None) -> str:
    """ISO 8601 UTC with millisecond precision and a ``Z`` suffix.

    This is the form the ALPACA spec gives for ``UTCDate``
    (e.g. ``2026-11-13T03:04:05.678Z``).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_utc_iso(value: Any) -> Optional[datetime]:
    """Parse a device ``UTCDate`` string into an aware UTC datetime.

    Tolerates a ``Z`` or explicit offset, no zone at all (treated as UTC),
    and 0-7 fractional-second digits (.NET drivers emit 7). None if unparsable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Normalise the fractional part to 6 digits for fromisoformat on 3.10.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            digits += rest[i]
            i += 1
        text = head + "." + (digits[:6].ljust(6, "0")) + rest[i:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def clock_offset_seconds(device_utc: Any, node_now: Optional[datetime] = None) -> Optional[float]:
    """Device clock minus node clock, in seconds (positive = device ahead).

    None when the device value cannot be parsed.
    """
    dev = parse_utc_iso(device_utc)
    if dev is None:
        return None
    if node_now is None:
        node_now = datetime.now(timezone.utc)
    elif node_now.tzinfo is None:
        node_now = node_now.replace(tzinfo=timezone.utc)
    return round((dev - node_now).total_seconds(), 3)


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

    def read_site_and_time(self) -> dict:
        """Read-only: the mount's own idea of where and when it is.

        Returns ``utcdate``, ``sitelatitude``, ``sitelongitude`` and
        ``siteelevation`` straight from the device (None for any that fail
        to read, with the reason under ``errors``), plus ``clock_offset_s``
        = device UTC minus this node's UTC. A Seestar that lost its clock on
        a power cycle tracks at the wrong rate while still reporting
        ``tracking: true`` (issue #149) -- this is how to see that.
        """
        out: dict = {"utcdate": None, "sitelatitude": None,
                     "sitelongitude": None, "siteelevation": None,
                     "errors": {}}
        # Sample the node clock around the utcdate GET itself (request/response
        # midpoint), so slow or timed-out site reads never inflate the offset.
        before = datetime.now(timezone.utc)
        try:
            out["utcdate"] = self._c._get("utcdate")
        except Exception as exc:  # noqa: BLE001 - read is diagnostic only
            out["errors"]["utcdate"] = str(exc)
        after = datetime.now(timezone.utc)
        node_now = before + (after - before) / 2
        for attr in ("sitelatitude", "sitelongitude", "siteelevation"):
            try:
                out[attr] = float(self._c._get(attr))
            except Exception as exc:  # noqa: BLE001 - read is diagnostic only
                out["errors"][attr] = str(exc)
        out["node_utc"] = format_utc_iso(node_now)
        out["clock_offset_s"] = clock_offset_seconds(out["utcdate"], node_now)
        return out

    def sync_site_and_time(self, lat: float, lon: float,
                           elev_m: Optional[float] = None,
                           now: Optional[datetime] = None) -> dict:
        """Push site latitude/longitude/elevation and UTC time to the mount.

        ``lon`` is east-positive (ASCOM convention; Texas is negative).
        Every PUT is fail-soft: a refusal is logged and recorded under
        ``errors`` but never raised, so a driver that does not implement a
        setter cannot block connect/slew/tracking. ``siteelevation`` refusals
        with a not-settable/not-implemented code are expected and logged at
        INFO. The values are read back afterwards and returned
        (see :meth:`read_site_and_time`), along with ``sent`` and ``ok``.
        """
        sent: dict = {}
        errors: dict = {}

        def _put(action: str, key: str, value: Any, optional: bool = False) -> None:
            sent[action] = value
            try:
                self._c._put(action, **{key: value})
            except AlpacaError as exc:
                errors[action] = str(exc)
                if optional and getattr(exc, "code", 0) in _NOT_SETTABLE_CODES:
                    logger.info("Mount %s not settable (%s) - skipped", action, exc)
                else:
                    logger.warning("Mount %s sync failed: %s", action, exc)
            except Exception as exc:  # noqa: BLE001 - transport errors fail soft too
                errors[action] = str(exc)
                logger.warning("Mount %s sync failed: %s", action, exc)

        _put("sitelatitude", "SiteLatitude", float(lat))
        _put("sitelongitude", "SiteLongitude", float(lon))
        if elev_m is not None:
            _put("siteelevation", "SiteElevation", float(elev_m), optional=True)
        # Time last, so it is as fresh as possible when the mount applies it.
        _put("utcdate", "UTCDate", format_utc_iso(now))

        readback = self.read_site_and_time()
        readback["sent"] = sent
        readback["put_errors"] = errors
        critical = [a for a in ("sitelatitude", "sitelongitude", "utcdate") if a in errors]
        readback["ok"] = not critical
        logger.info(
            "Mount site/time sync: lat=%.4f lon=%.4f elev=%s utc=%s -> device utc=%s "
            "offset=%ss%s",
            float(lat), float(lon), elev_m, sent.get("utcdate"),
            readback.get("utcdate"), readback.get("clock_offset_s"),
            f" (failed: {', '.join(sorted(errors))})" if errors else "",
        )
        return readback

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
