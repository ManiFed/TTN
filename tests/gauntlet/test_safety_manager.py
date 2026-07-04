"""Gauntlet: SafetyManager vs. dying hardware and dawn (F5, F12 adjacent).

The watchdog must park on prolonged disconnect, park at dawn, recover the
latch at dusk, and never double-park.  Exercised synchronously by calling
the check methods the monitor thread runs, with a programmable telescope.
"""

import time
import unittest
from unittest.mock import patch

from alpaca.safety_manager import SafetyManager


class FakeTelescope:
    """Programmable ALPACA telescope double."""

    def __init__(self):
        self.reachable = True
        self.reconnectable = False
        self.park_calls = 0
        self.park_raises = False
        self._c = self  # SafetyManager pings via tel._c.ping()

    # AlpacaClient surface
    def ping(self, timeout=5.0):
        if not self.reachable:
            raise ConnectionError("telescope unreachable")
        return True

    def connect(self):
        if not self.reconnectable:
            raise ConnectionError("still unreachable")
        self.reachable = True

    def disconnect(self):
        pass

    def park(self):
        if self.park_raises:
            raise RuntimeError("park command failed")
        self.park_calls += 1


def _mgr(tel, **safety_over) -> SafetyManager:
    safety = {
        "enabled": True,
        "disconnect_timeout": 0.05,
        "heartbeat_interval": 0.01,
        "heartbeat_timeout": 0.1,
        "reconnect_attempts": 1,
        "reconnect_delay": 0.0,
        "park_at_dawn": True,
        "observer": {"latitude": 31.36, "longitude": -99.44},
    }
    safety.update(safety_over)
    mgr = SafetyManager(config={"safety": safety})
    mgr.attach_telescope(tel)
    # Skip the 10 s post-attach grace period.
    mgr._telescope_attached_at = time.monotonic() - 60
    return mgr


class DisconnectParkTest(unittest.TestCase):
    def test_healthy_telescope_stays_safe(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        mgr._run_connection_check()
        self.assertTrue(mgr.is_safe())
        self.assertEqual(tel.park_calls, 0)

    def test_transient_dropout_recovers_via_reconnect(self):
        tel = FakeTelescope()
        tel.reachable = False
        tel.reconnectable = True
        mgr = _mgr(tel)
        mgr._run_connection_check()
        self.assertTrue(mgr.is_safe())        # reconnect succeeded
        self.assertEqual(tel.park_calls, 0)

    def test_prolonged_disconnect_parks(self):
        tel = FakeTelescope()
        tel.reachable = False
        mgr = _mgr(tel)
        mgr._run_connection_check()            # starts the disconnect timer
        self.assertTrue(mgr.is_safe())         # within timeout — still safe
        time.sleep(0.08)                       # exceed disconnect_timeout
        mgr._run_connection_check()
        self.assertFalse(mgr.is_safe())
        # The scope was unreachable, so park is attempted but state is latched
        # regardless — the crucial part is the latch + reason.
        self.assertIn("unreachable", mgr.status()["reason"])

    def test_park_failure_still_latches_unsafe(self):
        tel = FakeTelescope()
        tel.park_raises = True
        mgr = _mgr(tel)
        mgr.emergency_park("test failure")
        self.assertFalse(mgr.is_safe())
        self.assertTrue(mgr.status()["parked"])  # latched even though park raised

    def test_emergency_park_is_idempotent(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        mgr.emergency_park("first")
        mgr.emergency_park("second")
        self.assertEqual(tel.park_calls, 1)
        self.assertIn("first", mgr.status()["reason"])

    def test_on_unsafe_callback_failure_does_not_block_park(self):
        tel = FakeTelescope()
        mgr = SafetyManager(config={"safety": {"observer": {}}},
                            on_unsafe=lambda: 1 / 0)
        mgr.attach_telescope(tel)
        mgr.emergency_park("with exploding callback")
        self.assertEqual(tel.park_calls, 1)

    def test_reattach_clears_latched_state(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        mgr.emergency_park("oops")
        self.assertFalse(mgr.is_safe())
        mgr.attach_telescope(tel)
        self.assertTrue(mgr.is_safe())


class DawnParkTest(unittest.TestCase):
    def test_sun_above_threshold_parks(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        with patch("alpaca.safety_manager._solar_elevation", return_value=+5.0):
            mgr._run_dawn_check()
        self.assertFalse(mgr.is_safe())
        self.assertTrue(mgr.status()["reason"].startswith("dawn"))
        self.assertEqual(tel.park_calls, 1)

    def test_dawn_latch_autoclears_at_dusk(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        with patch("alpaca.safety_manager._solar_elevation", return_value=+5.0):
            mgr._run_dawn_check()
        self.assertFalse(mgr.is_safe())
        with patch("alpaca.safety_manager._solar_elevation", return_value=-25.0):
            mgr._run_dawn_clear()
        self.assertTrue(mgr.is_safe())

    def test_dusk_clear_leaves_non_dawn_latch_alone(self):
        tel = FakeTelescope()
        mgr = _mgr(tel)
        mgr.emergency_park("telescope unreachable for 600s")
        with patch("alpaca.safety_manager._solar_elevation", return_value=-25.0):
            mgr._run_dawn_clear()
        self.assertFalse(mgr.is_safe())  # disconnect park must not auto-clear

    def test_no_location_never_dawn_parks(self):
        tel = FakeTelescope()
        mgr = _mgr(tel, observer={})
        with patch("alpaca.safety_manager._solar_elevation", return_value=+30.0):
            mgr._run_dawn_check()
        self.assertTrue(mgr.is_safe())


class PointingSafetyTest(unittest.TestCase):
    def test_horizon_mask_interpolates(self):
        mgr = SafetyManager(config={"safety": {
            "observer": {},
            "horizon_mask": [[10, 0], [30, 90], [10, 180], [10, 270]],
        }})
        self.assertFalse(mgr.is_pointing_safe(15.0, 90.0))   # needs 30°
        self.assertTrue(mgr.is_pointing_safe(35.0, 90.0))
        self.assertAlmostEqual(mgr.min_safe_altitude(45.0), 20.0, places=1)

    def test_empty_mask_allows_everything(self):
        mgr = SafetyManager(config={"safety": {"observer": {}}})
        self.assertTrue(mgr.is_pointing_safe(0.5, 123.0))


if __name__ == "__main__":
    unittest.main()
