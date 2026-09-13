"""
ALPACA Autodiscovery (ASCOM standard, section 3).

Sends the UDP broadcast "alpacadiscovery1" and collects JSON responses
from any ALPACA servers on the LAN. Each response contains an 'AlpacaPort'
field giving the HTTP port the server listens on.
"""

import json
import ipaddress
import logging
import socket

import requests

logger = logging.getLogger(__name__)

DISCOVERY_MESSAGE = b"alpacadiscovery1"
BROADCAST_ADDR = "255.255.255.255"

# A Starfront-night LAN is full of ALPACA decoys that will happily answer the
# discovery broadcast and accept a connection: N.I.N.A.'s built-in Alpaca
# server (commonly on :32330) and the ASCOM/Alpaca simulator (commonly on
# :32323). Reconnecting to either wastes a night. Identity is verified from
# the device name/description string returned by the ALPACA management API
# (see _fetch_device_info below) — reject anything that names a simulator or
# NINA, and only accept strings that positively name a ZWO Seestar.
_REJECT_IDENTITY_SUBSTRINGS = (
    "nina",
    "n.i.n.a",
    "ascom simulator",
    "alpaca simulator",
    "simulator",
)
_ACCEPT_IDENTITY_SUBSTRINGS = (
    "seestar",
    "zwo",
    "s50",
    "s30",
)


def is_verified_seestar(device_name: str) -> bool:
    """Return True only when *device_name* positively identifies a real ZWO
    Seestar (S50 / S30PROSF).

    Identity is unknown-by-default: a blank name, a name that doesn't
    mention Seestar/ZWO/S50/S30, or one that names a known decoy (NINA, an
    ASCOM or Alpaca simulator) is rejected. This is deliberately stricter
    than "not a known decoy" — an unrecognized responder on the LAN is not
    assumed to be the telescope just because it isn't obviously a simulator.
    """
    name = (device_name or "").strip().lower()
    if not name:
        return False
    if any(bad in name for bad in _REJECT_IDENTITY_SUBSTRINGS):
        return False
    return any(good in name for good in _ACCEPT_IDENTITY_SUBSTRINGS)


def _fetch_device_info(address: str, port: int) -> dict:
    """Query the ALPACA management API for device name and serial (UniqueID)."""
    # Discovery replies are unauthenticated UDP packets.  Do not let a reply
    # turn this LAN-only helper into a request proxy.
    try:
        address = str(ipaddress.ip_address(address))
    except ValueError:
        return {}
    if not ipaddress.ip_address(address).is_private or not 1 <= port <= 65535:
        return {}
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
            # Binding to all interfaces ('') is intentional and required here:
            # this is a UDP broadcast socket (SO_BROADCAST) used to send the
            # ALPACA discovery datagram to 255.255.255.255 and receive replies
            # from any local interface/subnet the LAN telescope may be on.
            # Binding to a single interface would silently miss replies on
            # multi-homed hosts. Port 0 lets the OS pick an ephemeral source
            # port, so this never listens on a well-known/fixed port either.
            sock.bind(("", 0))  # lgtm[py/bind-socket-all-network-interfaces]

            logger.debug("Broadcasting ALPACA discovery on port %d", port)
            sock.sendto(DISCOVERY_MESSAGE, (BROADCAST_ADDR, port))

            while True:
                try:
                    data, addr = sock.recvfrom(1024)
                    payload = json.loads(data.decode("utf-8"))
                    alpaca_port = int(payload.get("AlpacaPort", 11111))
                    if not 1 <= alpaca_port <= 65535:
                        continue
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
