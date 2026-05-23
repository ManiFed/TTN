#!/usr/bin/env python3
"""
ALPACA CLI

Usage examples:
  alpaca --discover
  alpaca --activate
  alpaca --status
  alpaca --slew 8.13 70.37
  alpaca --park
  alpaca --expose 5.0
"""

import argparse
import logging
import sys

import yaml

from alpaca.device_manager import DeviceManager
from alpaca.discovery import discover_servers

logger = logging.getLogger(__name__)


def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def get_server(cfg: dict):
    alpaca_cfg = cfg.get("alpaca", {})
    servers = discover_servers(
        port=alpaca_cfg.get("discovery_port", 32227),
        timeout=alpaca_cfg.get("discovery_timeout", 5),
    )
    if not servers:
        logger.error("No ALPACA servers found — is the server running on this network?")
        return None
    server = servers[0]
    logger.info("Using server %s:%d", server["address"], server["port"])
    return server


def cmd_discover(cfg: dict) -> int:
    alpaca_cfg = cfg.get("alpaca", {})
    servers = discover_servers(
        port=alpaca_cfg.get("discovery_port", 32227),
        timeout=alpaca_cfg.get("discovery_timeout", 5),
    )
    if not servers:
        print("No ALPACA servers found.")
        return 1
    print(f"Found {len(servers)} server(s):")
    for s in servers:
        print(f"  {s['address']}:{s['port']}")
    return 0


def cmd_activate(cfg: dict) -> int:
    server = get_server(cfg)
    if server is None:
        return 1
    manager = DeviceManager(server["address"], server["port"], cfg)
    try:
        manager.connect_all()
        print("Devices activated successfully.")
    except Exception:
        logger.exception("Error during activation")
        return 1
    finally:
        manager.disconnect_all()
    return 0


def cmd_status(cfg: dict) -> int:
    server = get_server(cfg)
    if server is None:
        return 1
    manager = DeviceManager(server["address"], server["port"], cfg)
    try:
        manager.connect_all()
        tel = manager.telescope
        if tel is not None:
            print(f"  RA:       {tel.ra():.4f} h")
            print(f"  Dec:      {tel.dec():.4f} °")
            print(f"  Tracking: {tel.is_tracking()}")
            print(f"  Parked:   {tel.is_parked()}")
            print(f"  Slewing:  {tel.is_slewing()}")
        else:
            print("No telescope connected.")
    except Exception:
        logger.exception("Error getting status")
        return 1
    finally:
        manager.disconnect_all()
    return 0


def cmd_slew(cfg: dict, ra: float, dec: float) -> int:
    server = get_server(cfg)
    if server is None:
        return 1
    manager = DeviceManager(server["address"], server["port"], cfg)
    try:
        manager.connect_all()
        tel = manager.telescope
        if tel is None:
            logger.error("Telescope not enabled in config.")
            return 1
        tel.unpark()
        tel.set_tracking(True)
        tel.slew_to_coordinates(ra=ra, dec=dec)
    except Exception:
        logger.exception("Error during slew")
        return 1
    finally:
        manager.disconnect_all()
    return 0


def cmd_park(cfg: dict) -> int:
    server = get_server(cfg)
    if server is None:
        return 1
    manager = DeviceManager(server["address"], server["port"], cfg)
    try:
        manager.connect_all()
        tel = manager.telescope
        if tel is None:
            logger.error("Telescope not enabled in config.")
            return 1
        tel.park()
    except Exception:
        logger.exception("Error during park")
        return 1
    finally:
        manager.disconnect_all()
    return 0


def cmd_expose(cfg: dict, duration: float) -> int:
    server = get_server(cfg)
    if server is None:
        return 1
    manager = DeviceManager(server["address"], server["port"], cfg)
    try:
        manager.connect_all()
        cam = manager.camera
        if cam is None:
            logger.error("Camera not enabled in config.")
            return 1
        cam_cfg = cfg.get("camera", {})
        cam.set_binning(cam_cfg.get("binning", 1))
        cam.expose(duration=duration, light=True)
        print(f"Exposure of {duration}s complete.")
    except Exception:
        logger.exception("Error during exposure")
        return 1
    finally:
        manager.disconnect_all()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="alpaca",
        description="ALPACA telescope control CLI",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--discover", action="store_true", help="Scan for ALPACA servers on the LAN")
    group.add_argument("--activate", action="store_true", help="Connect to all enabled devices")
    group.add_argument("--status", action="store_true", help="Print current telescope position and state")
    group.add_argument("--slew", nargs=2, metavar=("RA", "DEC"), type=float, help="Slew to RA (decimal hours) and Dec (decimal degrees)")
    group.add_argument("--park", action="store_true", help="Park the telescope")
    group.add_argument("--expose", metavar="DURATION", type=float, help="Take a camera exposure (seconds)")

    args = parser.parse_args()
    cfg = load_config()
    setup_logging(cfg)

    if args.discover:
        return cmd_discover(cfg)
    if args.activate:
        return cmd_activate(cfg)
    if args.status:
        return cmd_status(cfg)
    if args.slew:
        return cmd_slew(cfg, ra=args.slew[0], dec=args.slew[1])
    if args.park:
        return cmd_park(cfg)
    if args.expose is not None:
        return cmd_expose(cfg, duration=args.expose)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
