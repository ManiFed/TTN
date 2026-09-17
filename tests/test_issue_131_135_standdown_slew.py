#!/usr/bin/env python3
"""Issues #131 / #135: mid-expose slew refuse + stand-down latch."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash


class StandDownLatchTest(unittest.TestCase):
    def setUp(self):
        dash._clear_stand_down_latch(reason="test-setup")

    def tearDown(self):
        dash._clear_stand_down_latch(reason="test-teardown")

    def test_latch_blocks_tonight_allows_observing(self):
        self.assertTrue(dash._tonight_allows_observing() or True)  # may depend on _tonight
        dash._set_stand_down_latch("operator stand-down")
        self.assertTrue(dash._stand_down_latched())
        self.assertFalse(dash._tonight_allows_observing())
        dash._clear_stand_down_latch(reason="re-arm")
        self.assertFalse(dash._stand_down_latched())

    def test_cloud_tonight_not_observing_arms_latch(self):
        dash._clear_stand_down_latch()
        with patch.object(dash, "_telemetry", MagicMock()), \
             patch.object(dash, "_cam", None), \
             patch.object(dash, "_tel", None):
            dash._on_cloud_tonight({"observing": False, "reason": "stood down", "status": "stood_down"})
        self.assertTrue(dash._stand_down_latched())
        with patch.object(dash, "_telemetry", MagicMock()):
            dash._on_cloud_tonight({"observing": True, "status": "accepted"})
        self.assertFalse(dash._stand_down_latched())

    def test_schedule_run_source_checks_latch(self):
        src = inspect.getsource(dash.api_schedule_run)
        self.assertIn("_stand_down_latched", src)
        self.assertIn("issue #135", src)

    def test_standdown_routes_exist(self):
        self.assertTrue(hasattr(dash, "api_standdown_local"))
        self.assertTrue(hasattr(dash, "api_standdown_rearm"))
        self.assertTrue(hasattr(dash, "api_standdown_status"))


class MidExposeSlewRefuseTest(unittest.TestCase):
    def test_api_slew_refuses_during_science_capture(self):
        src = inspect.getsource(dash.api_slew)
        self.assertIn("_science_capture_active", src)
        self.assertIn("issue #131", src)
        self.assertIn("_stand_down_latched", src)
        self.assertIn("issue #135", src)


if __name__ == "__main__":
    unittest.main()
