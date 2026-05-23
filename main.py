#!/usr/bin/env python3
"""
ALPACA skeleton — entry point.

Discovers a server on the LAN, connects to enabled devices, runs a brief
smoke-test sequence (slew + short exposure), then disconnects cleanly.
"""

import logging
import sys
import time

import yaml

from alpaca.device_manager import DeviceManager
from alpaca.discovery import discover_servers


def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def run_smoke_test(manager: DeviceManager, cfg: dict) -> None:
    tel = manager.telescope
    cam = manager.camera

    if tel is not None:
        tel_cfg = cfg.get("telescope", {})
        tel.unpark()
        tel.set_tracking(True)
        tel.slew_to_coordinates(
            ra=tel_cfg.get("slew_ra", 0.0),
            dec=tel_cfg.get("slew_dec", 0.0),
        )
        logger.info("Holding at destination for 3 minutes — check your pier cam…")
        time.sleep(180)
        logger.info("Hold complete, continuing.")

    if cam is not None:
        cam_cfg = cfg.get("camera", {})
        binning = cam_cfg.get("binning", 1)
        duration = cam_cfg.get("exposure_duration", 1.0)
        cam.set_binning(binning)
        cam.expose(duration=duration, light=True)
        # Intentionally skip image_array() download in the smoke test —
        # large raw arrays over HTTP are slow; hook in real code here.

    if tel is not None:
        tel.park()


def main() -> int:
    cfg = load_config()
    setup_logging(cfg)
    logger = logging.getLogger(__name__)

    alpaca_cfg = cfg.get("alpaca", {})
    servers = discover_servers(
        port=alpaca_cfg.get("discovery_port", 32227),
        timeout=alpaca_cfg.get("discovery_timeout", 5),
    )

    if not servers:
        logger.error("No ALPACA servers found — is the server running on this network?")
        return 1

    # Use the first discovered server.
    server = servers[0]
    logger.info("Using server %s:%d", server["address"], server["port"])

    manager = DeviceManager(server["address"], server["port"], cfg)

    try:
        manager.connect_all()
        run_smoke_test(manager, cfg)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
    except Exception:
        logger.exception("Unhandled error during smoke test")
        return 1
    finally:
        manager.disconnect_all()

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
