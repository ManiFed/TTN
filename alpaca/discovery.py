"""
ALPACA Autodiscovery (ASCOM standard, section 3).

Sends the UDP broadcast "alpacadiscovery1" and collects JSON responses
from any ALPACA servers on the LAN. Each response contains an 'AlpacaPort'
field giving the HTTP port the server listens on.
"""

import json
import logging
import socket

logger = logging.getLogger(__name__)

DISCOVERY_MESSAGE = b"alpacadiscovery1"
BROADCAST_ADDR = "255.255.255.255"


def discover_servers(port: int = 32227, timeout: float = 5.0) -> list[dict]:
    """
    Broadcast the ALPACA discovery datagram and return a list of discovered
    servers as dicts: {"address": str, "port": int}.
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
