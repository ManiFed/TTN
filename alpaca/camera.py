"""
ALPACA Camera device wrapper.

Covers the subset of the ICameraV3 interface needed to start and retrieve
a single exposure.
"""

import logging
import time

from .client import AlpacaClient

logger = logging.getLogger(__name__)

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

    def set_binning(self, bin_x: int, bin_y: int | None = None) -> None:
        bin_y = bin_y if bin_y is not None else bin_x
        self._c._put("binx", BinX=bin_x)
        self._c._put("biny", BinY=bin_y)

    def expose(self, duration: float, light: bool = True, readout_timeout: float = 120.0) -> None:
        """
        Start an exposure and wait until the image is ready in the download buffer.

        duration        – exposure length in seconds
        light           – True for a light frame, False for a dark/bias
        readout_timeout – extra seconds beyond *duration* to allow for sensor
                          readout and ALPACA transfer (default 120 s; raise for
                          large/slow sensors or a slow network link)
        """
        logger.info("Starting %.2f s %s exposure", duration, "light" if light else "dark")
        self._c._put("startexposure", Duration=duration, Light=light)

        # Poll imageready — the authoritative ALPACA flag that the image has
        # landed in the download buffer.  Do NOT gate on CameraState == IDLE:
        # some drivers set imageready while still in STATE_DOWNLOAD (4), and
        # requiring IDLE would miss that window entirely.
        deadline = time.monotonic() + duration + readout_timeout
        while time.monotonic() < deadline:
            state = self.camera_state()
            if state == _STATE_ERROR:
                raise RuntimeError("Camera entered error state during exposure")
            if self.image_ready():
                logger.info("Exposure complete — image ready for download (camera state=%d)", state)
                return
            time.sleep(0.5)

        raise TimeoutError(
            f"Camera exposure did not complete within {duration + readout_timeout:.0f} s "
            f"({duration:.1f} s exposure + {readout_timeout:.0f} s readout budget)"
        )

    def abort_exposure(self) -> None:
        self._c._put("abortexposure")
        logger.warning("Exposure aborted")

    def image_array(self, timeout: float = 300.0) -> list:
        """Return the last image as a nested list (row-major). Large frames will be slow over HTTP."""
        logger.info("Downloading image array…")
        data = self._c._get("imagearray", timeout=timeout)
        logger.info("Image array received")
        return data
