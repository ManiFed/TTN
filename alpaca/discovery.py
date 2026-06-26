"""
ALPACA Autodiscovery (ASCOM standard, section 3).

Sends the UDP broadcast "alpacadiscovery1" and collects JSON responses
from any ALPACA servers on the LAN. Each response contains an 'AlpacaPort'
field giving the HTTP port the server listens on.
"""

import json
import logging
import socket

import requests

logger = logging.getLogger(__name__)

DISCOVERY_MESSAGE = b"alpacadiscovery1"
BROADCAST_ADDR = "255.255.255.255"


def _fetch_device_info(address: str, port: int) -> dict:
    """Query the ALPACA management API for device name and serial (UniqueID)."""
    try:
        url = f"http://{address}:{port}/management/v1/configureddevices"
        r = requests.get(url, timeout=3)
        r.raise_for_status()
        devices = r.json().get("Value", [])
        for dev in devices:
            if dev.get("DeviceType", "").lower() == "telescope":
                return {
                    "device_name": dev.get("DeviceName", ""),
                    "serial": dev.get("UniqueID", ""),
                }
    except Exception:
        pass
    return {}


def discover_servers(port: int = 32227, timeout: float = 5.0) -> list[dict]:
    """
    Broadcast the ALPACA discovery datagram and return a list of discovered
    servers as dicts: {"address": str, "port": int, "device_name": str, "serial": str}.
    """
    found = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)
            sock.bind(("", 0))

            logger.debug("Broadcasting ALPACA discovery on port %d", port)
            sock.sendto(DISCOVERY_MESSAGE, (BROADCAST_ADDR, port))

            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                    payload = json.loads(data.decode("utf-8"))
                    alpaca_port = int(payload.get("AlpacaPort", 11111))
                    entry = {"address": addr[0], "port": alpaca_port}
                    logger.info("Discovered ALPACA server at %s:%d", entry["address"], entry["port"])
                    found.append(entry)
                except TimeoutError:
                    break
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("Ignoring malformed discovery response from %s: %s", addr[0], exc)
    except OSError as exc:
        logger.error("Discovery socket error: %s", exc)

    if not found:
        logger.warning("No ALPACA servers found within %.1f s", timeout)
        return found

    for entry in found:
        info = _fetch_device_info(entry["address"], entry["port"])
        entry.update(info)
        if info.get("device_name"):
            logger.info(
                "  → %s (serial: %s)",
                info["device_name"], info.get("serial", "unknown"),
            )

    return found
