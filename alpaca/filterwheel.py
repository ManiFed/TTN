"""
ALPACA FilterWheel device wrapper.

Covers the IFilterWheelV2 interface subset for position queries and moves.
"""

import logging

from .client import AlpacaClient

logger = logging.getLogger(__name__)

_MOVING = -1  # ALPACA spec: position == -1 while wheel is rotating


class FilterWheel:
    def __init__(self, host: str, port: int, device_number: int = 0, api_version: int = 1):
        self._c = AlpacaClient(host, port, "filterwheel", device_number, api_version)

    def connect(self) -> None:
        self._c.connect()
        logger.info("FilterWheel connected: %s", self._c.name())

    def disconnect(self) -> None:
        self._c.disconnect()
        logger.info("FilterWheel disconnected")

    def position(self) -> int:
        return int(self._c._get("position"))

    def is_moving(self) -> bool:
        return self.position() == _MOVING

    def filter_names(self) -> list[str]:
        return list(self._c._get("names"))

    def set_position(self, slot: int) -> None:
        """Rotate the wheel to *slot* (0-based) and wait until it settles there.

        Waits for position == slot rather than merely "not moving": some
        drivers briefly report the old position before the move starts, and a
        wheel that settles on the wrong slot must raise — a silently wrong
        filter corrupts the photometric band of every subsequent frame.
        """
        logger.info("FilterWheel moving to slot %d", slot)
        self._c._put("position", Position=slot)
        try:
            self._c.wait_for(lambda: self.position() == slot,
                             timeout=30, label=f"filter wheel at slot {slot}")
        except TimeoutError:
            final = self.position()
            raise RuntimeError(
                f"FilterWheel did not reach slot {slot} within 30 s "
                f"(reports {'moving' if final == _MOVING else f'slot {final}'})"
            )
        logger.info("FilterWheel at slot %d", slot)

    def set_position_by_name(self, name: str) -> None:
        """Rotate the wheel to the named filter (case-insensitive)."""
        names = [n.lower() for n in self.filter_names()]
        try:
            slot = names.index(name.lower())
        except ValueError:
            raise ValueError(f"Filter '{name}' not found. Available: {self.filter_names()}")
        self.set_position(slot)
