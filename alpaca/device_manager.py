"""
DeviceManager — connects to all enabled devices from a discovered server.

Owns the lifecycle of every device object so that main.py stays clean.
"""

import logging
from typing import Optional

from .camera import Camera
from .filterwheel import FilterWheel
from .focuser import Focuser
from .telescope import Telescope

logger = logging.getLogger(__name__)


class DeviceManager:
    def __init__(self, host: str, port: int, config: dict):
        self.host = host
        self.port = port
        self._cfg = config
        self._api_version = config.get("alpaca", {}).get("api_version", 1)

        self.telescope: Optional[Telescope] = None
        self.camera: Optional[Camera] = None
        self.focuser: Optional[Focuser] = None
        self.filterwheel: Optional[FilterWheel] = None

    def connect_all(self) -> None:
        devices_cfg = self._cfg.get("devices", {})

        if devices_cfg.get("telescope", {}).get("enabled", False):
            num = devices_cfg["telescope"].get("device_number", 0)
            self.telescope = Telescope(self.host, self.port, num, self._api_version)
            self.telescope.connect()

        if devices_cfg.get("camera", {}).get("enabled", False):
            num = devices_cfg["camera"].get("device_number", 0)
            self.camera = Camera(self.host, self.port, num, self._api_version)
            self.camera.connect()

        if devices_cfg.get("focuser", {}).get("enabled", False):
            num = devices_cfg["focuser"].get("device_number", 0)
            self.focuser = Focuser(self.host, self.port, num, self._api_version)
            self.focuser.connect()

        if devices_cfg.get("filterwheel", {}).get("enabled", False):
            num = devices_cfg["filterwheel"].get("device_number", 0)
            self.filterwheel = FilterWheel(self.host, self.port, num, self._api_version)
            self.filterwheel.connect()

    def disconnect_all(self) -> None:
        for device in (self.telescope, self.camera, self.focuser, self.filterwheel):
            if device is not None:
                try:
                    device.disconnect()
                except Exception as exc:
                    logger.warning("Error during disconnect: %s", exc)
