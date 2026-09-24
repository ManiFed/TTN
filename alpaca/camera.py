"""
ALPACA Camera device wrapper.

Covers the subset of the ICameraV3 interface needed to start and retrieve
a single exposure.
"""

import logging
import time
from typing import Callable, Optional

from .client import AlpacaClient

logger = logging.getLogger(__name__)


class ExposureCancelled(Exception):
    """Raised when an exposure is aborted via its cancel_check callback."""

# CameraState enum values from the ALPACA spec
_STATE_IDLE = 0
_STATE_WAITING = 1
_STATE_EXPOSING = 2
_STATE_READING = 3
_STATE_DOWNLOAD = 4
_STATE_ERROR = 5


class Camera:
    def __init__(self, host: str, port: int, device_number: int = 0, api_version: int = 1):
        self._c = AlpacaClient(host, port, "camera", device_number, api_version)

    # --- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        self._c.connect()
        name = self._c.name()
        logger.info("Camera connected: %s", name)

    def disconnect(self) -> None:
        self._c.disconnect()
        logger.info("Camera disconnected")

    # --- state queries -------------------------------------------------------

    def camera_state(self) -> int:
        return int(self._c._get("camerastate"))

    def image_ready(self) -> bool:
        return bool(self._c._get("imageready"))

    def sensor_name(self) -> str:
        return str(self._c._get("sensorname"))

    def full_well_capacity(self) -> float:
        return float(self._c._get("fullwellcapacity"))

    def pixel_size_x(self) -> float:
        return float(self._c._get("pixelsizex"))

    def pixel_size_y(self) -> float:
        return float(self._c._get("pixelsizey"))

    # --- commands ------------------------------------------------------------

    def gain(self) -> int:
        return int(self._c._get("gain"))

    def set_gain(self, gain: int) -> None:
        self._c._put("gain", Gain=gain)
        logger.info("Camera gain set to %d", gain)

    def offset(self) -> int:
        return int(self._c._get("offset"))

    def set_offset(self, offset: int) -> None:
        self._c._put("offset", Offset=offset)
        logger.info("Camera offset set to %d", offset)

    def ccd_temperature(self) -> float:
        return float(self._c._get("ccdtemperature"))

    def cooler_target(self) -> float:
        return float(self._c._get("setccdtemperature"))

    def set_cooler_target(self, temp: float) -> None:
        self._c._put("setccdtemperature", SetCCDTemperature=temp)
        logger.info("Camera cooler target set to %.1f °C", temp)

    def cooler_on(self) -> bool:
        return bool(self._c._get("cooleron"))

    def set_cooler(self, enabled: bool) -> None:
        self._c._put("cooleron", CoolerOn=enabled)
        logger.info("Camera cooler %s", "enabled" if enabled else "disabled")

    def set_binning(self, bin_x: int, bin_y: int | None = None) -> None:
        bin_y = bin_y if bin_y is not None else bin_x
        self._c._put("binx", BinX=bin_x)
        self._c._put("biny", BinY=bin_y)

    def set_roi(self, start_x: int, start_y: int, num_x: int, num_y: int) -> None:
        self._c._put("startx", StartX=start_x)
        self._c._put("starty", StartY=start_y)
        self._c._put("numx", NumX=num_x)
        self._c._put("numy", NumY=num_y)
        logger.info("Camera ROI set: origin=(%d,%d) size=%dx%d", start_x, start_y, num_x, num_y)

    def reset_roi(self) -> None:
        # CameraXSize/CameraYSize are always unbinned pixels (ASCOM/ALPACA
        # ICameraV3), but NumX/NumY must be given in binned pixels -- passing
        # the unbinned size straight through would request a subframe up to
        # bin_x*bin_y times larger than the binned frame actually has.
        w = int(self._c._get("cameraxsize"))
        h = int(self._c._get("cameraysize"))
        bin_x = int(self._c._get("binx"))
        bin_y = int(self._c._get("biny"))
        num_x, num_y = w // bin_x, h // bin_y
        self.set_roi(0, 0, num_x, num_y)
        logger.info("Camera ROI reset to full frame %dx%d (binned %dx%d)",
                    w, h, num_x, num_y)

    def expose(
        self,
        duration: float,
        light: bool = True,
        readout_timeout: float = 120.0,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Start an exposure and wait until the image is ready in the download buffer.

        duration        – exposure length in seconds
        light           – True for a light frame, False for a dark/bias
        readout_timeout – extra seconds beyond *duration* to allow for sensor
                          readout and ALPACA transfer (default 120 s; raise for
                          large/slow sensors or a slow network link)
        cancel_check    – optional zero-arg callable polled during the wait; if
                          it returns True the exposure is aborted on the camera
                          and ExposureCancelled is raised.
        """
        logger.info("Starting %.2f s %s exposure", duration, "light" if light else "dark")
        # Pre-flight: reclaim a leftover firmware capture-active latch so the
        # immediate next StartExposure after a finished frame does not 1279
        # (issues #133/#127; mirrors post-expose clear_capture_latch).
        try:
            pre = self.camera_state()
            if pre not in (_STATE_IDLE, _STATE_ERROR):
                logger.warning(
                    "StartExposure preflight: camera state=%d — clearing "
                    "stale capture latch (issue #133)", pre,
                )
                self.clear_capture_latch(settle_s=5.0)
        except Exception as exc:
            logger.debug("StartExposure preflight latch clear skipped: %s", exc)
        self._c._put("startexposure", Duration=duration, Light=light)

        # Poll imageready — the authoritative ALPACA flag that the image has
        # landed in the download buffer.  Do NOT gate on CameraState == IDLE:
        # some drivers set imageready while still in STATE_DOWNLOAD (4), and
        # requiring IDLE would miss that window entirely.
        deadline = time.monotonic() + duration + readout_timeout
        # If the camera returns to IDLE without ImageReady after the exposure
        # window, fail fast — waiting out a long readout_timeout with an empty
        # buffer is what left node_expose hanging after a failed center (#52).
        idle_grace = max(5.0, min(15.0, duration + 5.0))
        idle_since: Optional[float] = None
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                self.abort_exposure()
                raise ExposureCancelled("Exposure cancelled")
            state = self.camera_state()
            if state == _STATE_ERROR:
                raise RuntimeError("Camera entered error state during exposure")
            if self.image_ready():
                # Seestar can flip ImageReady while still reporting EXPOSING (2);
                # imagearray then returns "no image available" (Starfront 2026-09-16).
                # Accept ImageReady in IDLE/READING/DOWNLOAD; keep waiting if still
                # EXPOSING/WAITING so the buffer can finish filling.
                if state in (_STATE_EXPOSING, _STATE_WAITING):
                    logger.info(
                        "ImageReady with camera still state=%d — waiting for readout",
                        state,
                    )
                else:
                    logger.info(
                        "Exposure complete — image ready for download (camera state=%d)",
                        state,
                    )
                    return
            now = time.monotonic()
            if state == _STATE_IDLE and now >= (deadline - readout_timeout) + idle_grace:
                if idle_since is None:
                    idle_since = now
                elif now - idle_since >= 2.0:
                    raise TimeoutError(
                        f"Camera returned to Idle without ImageReady after "
                        f"{duration:.1f}s exposure (fail-fast; not waiting full "
                        f"{readout_timeout:.0f}s readout budget)"
                    )
            else:
                idle_since = None
            time.sleep(0.5)

        raise TimeoutError(
            f"Camera exposure did not complete within {duration + readout_timeout:.0f} s "
            f"({duration:.1f} s exposure + {readout_timeout:.0f} s readout budget)"
        )

    def abort_exposure(self, settle_s: float = 15.0) -> None:
        self._c._put("abortexposure")
        logger.warning("Exposure aborted")
        # Drain a stuck EXPOSING/DOWNLOAD state so the next StartExposure is not
        # rejected with SET_PREVIEW_PAGE / capture-is-active (Starfront nights).
        self._wait_capture_idle(settle_s=settle_s)

    def _wait_capture_idle(self, settle_s: float = 15.0) -> int:
        """Poll CameraState until IDLE/ERROR or settle budget exhausted.

        Returns the last observed state (or -1 if the query failed).
        """
        deadline = time.monotonic() + max(0.0, settle_s)
        last_state = -1
        while time.monotonic() < deadline:
            try:
                last_state = self.camera_state()
            except Exception:
                return -1
            if last_state in (_STATE_IDLE, _STATE_ERROR):
                return last_state
            time.sleep(0.25)
        return last_state

    def clear_capture_latch(self, settle_s: float = 8.0) -> bool:
        """Clear Seestar firmware capture-active after a finished expose.

        Issue #133 / #127: after a successful or cancelled expose the driver
        can still reject the next StartExposure with Error 1279 / "capture is
        active" until an abort settles. Soft-abort when not already IDLE so
        an immediate re-expose works without a manual node_abort_exposure.
        Returns True when the camera reports IDLE afterward.
        """
        try:
            state = self.camera_state()
        except Exception as exc:
            logger.warning("clear_capture_latch: cannot read camerastate: %s", exc)
            return False
        if state == _STATE_IDLE:
            return True
        logger.info(
            "clear_capture_latch: camera state=%d after expose — soft-abort "
            "to clear capture-active (issues #133/#127)",
            state,
        )
        try:
            # Prefer abort PUT even when ImageReady already fired; Seestar
            # treats this as releasing the capture latch rather than losing
            # the already-downloaded frame.
            self._c._put("abortexposure")
        except Exception as exc:
            logger.warning("clear_capture_latch: abortexposure failed: %s", exc)
        final = self._wait_capture_idle(settle_s=settle_s)
        ok = final == _STATE_IDLE
        if not ok:
            logger.warning(
                "clear_capture_latch: still state=%s after %.1fs settle",
                final, settle_s,
            )
        return ok

    def image_array(self, timeout: float = 300.0) -> list:
        """Return the last image as a nested list (row-major). Large frames will be slow over HTTP.

        Seestar ALPACA sometimes reports ImageReady before the download buffer
        is actually readable; the first imagearray call then fails with
        "no image available" while CameraState is still EXPOSING. Retry briefly
        instead of failing the whole science frame (Starfront 2026-09-16).
        """
        from .client import AlpacaError

        logger.info("Downloading image array…")
        # Bound the settle window separately from the (large) transfer timeout.
        settle_deadline = time.monotonic() + min(20.0, max(5.0, timeout))
        last_exc: Optional[Exception] = None
        while True:
            try:
                data = self._c._get("imagearray", timeout=timeout)
                logger.info("Image array received")
                return data
            except AlpacaError as exc:
                msg = str(exc).lower()
                if "no image" not in msg:
                    raise
                last_exc = exc
                if time.monotonic() >= settle_deadline:
                    raise
                logger.warning(
                    "imagearray not ready yet (%s) — retrying (Seestar ImageReady race)",
                    exc,
                )
                time.sleep(0.5)
