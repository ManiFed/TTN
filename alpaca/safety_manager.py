"""
SafetyManager — protective watchdog for the Seestar telescope.

Responsibilities
----------------
* Periodic heartbeat to verify the telescope is still reachable.
* Retry / reconnect logic for transient connection dropouts.
* Auto-park when the telescope is unreachable beyond a configurable timeout.
* Auto-park at astronomical (or nautical / civil) dawn.
* Graceful shutdown on SIGTERM and SIGINT — parks before exiting.
* is_safe() / status() API so other modules can gate operations on safety.

Usage (from main / dashboard)
------------------------------
    mgr = SafetyManager(config=cfg, on_unsafe=abort_callback)
    mgr.start()                          # call from main thread
    ...
    mgr.attach_telescope(tel)            # after the device connects
    ...
    mgr.stop()                           # on application exit
"""

import logging
import math
import signal
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DAWN_ELEVATIONS = {
    "astronomical": -18.0,
    "nautical":     -12.0,
    "civil":         -6.0,
}


# ── Solar-position math ────────────────────────────────────────────────────────

def _solar_elevation(lat_deg: float, lon_deg: float, utc_unix: float) -> float:
    """
    Solar elevation angle in degrees at the given WGS-84 position and UTC time.
    Uses the NOAA algorithm; accuracy ≈ ±0.3° for dates 2000–2050.
    """
    JD = utc_unix / 86400.0 + 2440587.5          # Julian Day Number
    T  = (JD - 2451545.0) / 36525.0              # Julian centuries from J2000.0

    L0    = (280.46646 + 36000.76983 * T) % 360.0
    M     = math.radians((357.52911 + 35999.05029 * T - 0.0001537 * T * T) % 360.0)
    C     = ((1.914602 - 0.004817 * T - 0.000014 * T * T) * math.sin(M)
             + (0.019993 - 0.000101 * T) * math.sin(2 * M)
             + 0.000289 * math.sin(3 * M))
    omega = math.radians(125.04 - 1934.136 * T)
    lam   = math.radians(L0 + C - 0.00569 - 0.00478 * math.sin(omega))

    eps_deg = (23.0 + 26.0 / 60.0 + 21.448 / 3600.0
               - (46.8150 * T + 0.00059 * T * T - 0.001813 * T * T * T) / 3600.0)
    eps   = math.radians(eps_deg + 0.00256 * math.cos(omega))

    dec   = math.asin(math.sin(eps) * math.sin(lam))
    ra    = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))

    GMST  = (280.46061837 + 360.98564736629 * (JD - 2451545.0)
             + 0.000387933 * T * T - T * T * T / 38710000.0) % 360.0
    HA    = math.radians(GMST + lon_deg) - ra

    lat   = math.radians(lat_deg)
    sin_alt = (math.sin(lat) * math.sin(dec)
               + math.cos(lat) * math.cos(dec) * math.cos(HA))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


# ── SafetyManager ─────────────────────────────────────────────────────────────

class SafetyManager:
    """
    Watchdog that monitors telescope connectivity and environmental conditions,
    parking the mount whenever the system should be stowed.

    Parameters
    ----------
    telescope : Telescope | None
        Device object.  Pass None here and call attach_telescope() later once
        the connection is established.
    config : dict
        Top-level application config dict (loaded from config.yaml).
        Reads the ``safety`` sub-key; all sub-keys have safe defaults.
    on_unsafe : callable | None
        Zero-argument callback invoked the first time the system becomes unsafe
        (e.g. to abort a running sequence).  Called before the park command.
    """

    def __init__(
        self,
        telescope=None,
        config: Optional[dict] = None,
        on_unsafe: Optional[Callable[[], None]] = None,
    ):
        cfg = (config or {}).get("safety", {})
        obs = cfg.get("observer", {})

        self._tel       = telescope
        self._on_unsafe = on_unsafe

        self._enabled              : bool  = cfg.get("enabled", True)
        self._disconnect_timeout   : float = float(cfg.get("disconnect_timeout", 600))
        self._heartbeat_interval   : float = float(cfg.get("heartbeat_interval", 30))
        self._reconnect_attempts   : int   = int(cfg.get("reconnect_attempts", 3))
        self._reconnect_delay      : float = float(cfg.get("reconnect_delay", 10))
        self._park_at_dawn         : bool  = cfg.get("park_at_dawn", True)
        self._dawn_elevation       : float = _DAWN_ELEVATIONS.get(
            cfg.get("dawn_type", "astronomical"), -18.0
        )
        self._lat                  : float = float(obs.get("latitude", 0.0))
        self._lon                  : float = float(obs.get("longitude", 0.0))

        self._lock           = threading.Lock()
        self._stop_event     = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Signal-handler bookkeeping (main-thread only)
        self._orig_sigint  = None
        self._orig_sigterm = None

        # Mutable safety state — always access under self._lock
        self._safe             : bool            = True
        self._parked           : bool            = False
        self._reason           : str             = ""
        self._heartbeat_ok     : bool            = True
        self._last_heartbeat   : Optional[float] = None   # UTC epoch
        self._disconnect_since : Optional[float] = None   # monotonic
        self._telescope_attached_at : Optional[float] = None  # monotonic

    # ── Public API ─────────────────────────────────────────────────────────────

    def attach_telescope(self, telescope) -> None:
        """Swap in a (re)connected telescope object and reset the disconnect timer."""
        with self._lock:
            self._tel              = telescope
            self._disconnect_since = None
            self._telescope_attached_at = time.monotonic() if telescope is not None else None
        if telescope is not None:
            logger.info("SafetyManager: telescope attached")

    def start(self) -> None:
        """Install OS signal handlers and start the background monitor thread.

        Must be called from the main thread so signal handlers are valid.
        """
        if not self._enabled:
            logger.info("SafetyManager: disabled — skipping startup")
            return
        self._install_signal_handlers()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="safety-monitor"
        )
        self._thread.start()
        logger.info(
            "SafetyManager started  "
            "(disconnect_timeout=%ds  heartbeat=%ds  dawn=%.1f°  lat=%.4f  lon=%.4f)",
            int(self._disconnect_timeout),
            int(self._heartbeat_interval),
            self._dawn_elevation,
            self._lat,
            self._lon,
        )

    def stop(self) -> None:
        """Signal the monitor thread to exit and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("SafetyManager stopped")

    def is_safe(self) -> bool:
        """Return True if it is safe to continue operations."""
        with self._lock:
            return self._safe

    def status(self) -> dict:
        """Return a snapshot of the current safety state."""
        with self._lock:
            ds = self._disconnect_since
            elapsed = (time.monotonic() - ds) if ds is not None else None
            tel = self._tel

        sun_el: Optional[float] = None
        if self._lat != 0.0 or self._lon != 0.0:
            try:
                sun_el = round(_solar_elevation(self._lat, self._lon, time.time()), 2)
            except Exception:
                pass

        with self._lock:
            return {
                "safe":              self._safe,
                "parked":            self._parked,
                "reason":            self._reason,
                "heartbeat_ok":      self._heartbeat_ok,
                "last_heartbeat":    self._last_heartbeat,
                "disconnected_secs": round(elapsed, 1) if elapsed is not None else None,
                "sun_elevation":     sun_el,
                "dawn_threshold":    self._dawn_elevation,
            }

    # ── Signal handling ────────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        try:
            self._orig_sigint  = signal.signal(signal.SIGINT,  self._handle_sigint)
            self._orig_sigterm = signal.signal(signal.SIGTERM, self._handle_sigterm)
        except (OSError, ValueError):
            logger.warning(
                "SafetyManager: cannot install signal handlers (not main thread)"
            )

    def _handle_sigint(self, signum, frame) -> None:
        logger.warning("SafetyManager: SIGINT received — parking before exit")
        self._emergency_park("SIGINT (Ctrl+C)")
        orig = self._orig_sigint
        if callable(orig):
            orig(signum, frame)
        else:
            signal.default_int_handler(signum, frame)

    def _handle_sigterm(self, signum, frame) -> None:
        logger.warning("SafetyManager: SIGTERM received — parking before exit")
        self._emergency_park("SIGTERM")
        sys.exit(0)

    # ── Monitor loop ───────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        next_check = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            if now >= next_check:
                self._run_connection_check()
                next_check = now + self._heartbeat_interval

            with self._lock:
                safe = self._safe

            if safe and self._park_at_dawn:
                self._run_dawn_check()

            self._stop_event.wait(timeout=5.0)

    def _run_connection_check(self) -> None:
        with self._lock:
            tel = self._tel
            attached_at = self._telescope_attached_at
        if tel is None:
            return  # Skip heartbeat until telescope is attached

        # Skip disconnect logic for 10s after attachment to allow scope discovery
        if attached_at is not None:
            age = time.monotonic() - attached_at
            if age < 10:
                alive = self._heartbeat()
                with self._lock:
                    self._heartbeat_ok = alive
                    self._last_heartbeat = time.time()
                return

        alive = self._heartbeat()
        with self._lock:
            self._heartbeat_ok   = alive
            self._last_heartbeat = time.time()

        if alive:
            with self._lock:
                was_lost           = self._disconnect_since is not None
                self._disconnect_since = None
            if was_lost:
                logger.info("SafetyManager: telescope connection restored")
            return

        # Heartbeat failed — attempt reconnect immediately
        logger.warning("SafetyManager: heartbeat failed — attempting reconnect…")
        if self._try_reconnect():
            with self._lock:
                self._disconnect_since = None
            return

        # All reconnect attempts exhausted
        with self._lock:
            if self._disconnect_since is None:
                self._disconnect_since = time.monotonic()
                logger.error(
                    "SafetyManager: telescope connection lost — "
                    "auto-park in %.0fs if not restored",
                    self._disconnect_timeout,
                )
            elapsed   = time.monotonic() - self._disconnect_since
            remaining = self._disconnect_timeout - elapsed
            safe      = self._safe

        if not safe:
            return

        if remaining <= 0:
            self._emergency_park(
                f"telescope unreachable for {int(elapsed)}s "
                f"(timeout={int(self._disconnect_timeout)}s)"
            )
        else:
            logger.warning(
                "SafetyManager: telescope still unreachable — "
                "auto-park in %.0fs", max(0.0, remaining)
            )

    def _run_dawn_check(self) -> None:
        if self._lat == 0.0 and self._lon == 0.0:
            return
        try:
            elev = _solar_elevation(self._lat, self._lon, time.time())
            logger.debug("SafetyManager: sun elevation %.2f°", elev)
            if elev > self._dawn_elevation:
                self._emergency_park(
                    f"dawn — sun {elev:.1f}° > threshold {self._dawn_elevation:.1f}°"
                )
        except Exception as exc:
            logger.debug("SafetyManager: dawn check error: %s", exc)

    # ── Heartbeat & reconnect ──────────────────────────────────────────────────

    def _heartbeat(self) -> bool:
        """Ping the telescope with a lightweight GET /connected call."""
        with self._lock:
            tel = self._tel
        if tel is None:
            return False
        try:
            tel._c.connected()
            return True
        except Exception as exc:
            logger.debug("SafetyManager: heartbeat exception: %s", exc)
            return False

    def _try_reconnect(self) -> bool:
        """
        Attempt to re-establish the ALPACA connection.
        Returns True if any attempt succeeds.
        """
        with self._lock:
            tel = self._tel
        if tel is None:
            return False

        for attempt in range(1, self._reconnect_attempts + 1):
            logger.info(
                "SafetyManager: reconnect attempt %d/%d…",
                attempt, self._reconnect_attempts,
            )
            try:
                tel.connect()
                logger.info(
                    "SafetyManager: reconnected on attempt %d", attempt
                )
                return True
            except Exception as exc:
                logger.warning(
                    "SafetyManager: reconnect attempt %d failed: %s", attempt, exc
                )
                if attempt < self._reconnect_attempts:
                    self._stop_event.wait(timeout=self._reconnect_delay)

        logger.error("SafetyManager: all %d reconnect attempts failed", self._reconnect_attempts)
        return False

    # ── Emergency park ─────────────────────────────────────────────────────────

    def _emergency_park(self, reason: str) -> None:
        """
        Mark the system unsafe, invoke the on_unsafe callback, park the mount,
        and disconnect.  Idempotent — subsequent calls are ignored once parked.
        """
        with self._lock:
            if self._parked:
                return
            self._safe   = False
            self._reason = reason
            tel          = self._tel

        logger.critical("SafetyManager: EMERGENCY PARK — %s", reason)

        if self._on_unsafe:
            try:
                self._on_unsafe()
            except Exception as exc:
                logger.warning("SafetyManager: on_unsafe callback raised: %s", exc)

        if tel is not None:
            try:
                logger.info("SafetyManager: sending park command to telescope…")
                tel.park()
                logger.info("SafetyManager: telescope parked successfully")
            except Exception as exc:
                logger.error("SafetyManager: park command failed: %s", exc)
            try:
                tel.disconnect()
            except Exception:
                pass

        with self._lock:
            self._parked = True
