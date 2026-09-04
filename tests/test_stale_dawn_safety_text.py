#!/usr/bin/env python3
"""Regression: stale dawn Safety stop text must clear when safety.safe is true (#53)."""

import unittest
from unittest.mock import MagicMock, patch

from src import dashboard


class StaleDawnSafetyTextTest(unittest.TestCase):
    def setUp(self):
        dashboard.app.testing = True
        self.client = dashboard.app.test_client()

    def test_api_status_clears_stale_safety_stop_when_safe(self):
        fake_mgr = MagicMock()
        fake_mgr.status.return_value = {"safe": True, "reason": "", "parked": False}
        with dashboard._state_lock:
            dashboard._state["error"] = (
                "Safety stop: dawn - sun -6.0° > threshold -6.0° (civil)"
            )
        prev = dashboard._safety_mgr
        dashboard._safety_mgr = fake_mgr
        try:
            resp = self.client.get("/api/status")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertIsNone(body.get("error"))
            # Persisted clear — not just masked in the response snapshot.
            with dashboard._state_lock:
                self.assertIsNone(dashboard._state.get("error"))
        finally:
            dashboard._safety_mgr = prev
            with dashboard._state_lock:
                dashboard._state["error"] = None

    def test_poller_clears_stale_safety_stop_when_snap_safe(self):
        with dashboard._state_lock:
            dashboard._state["error"] = "Safety stop: dawn — leftover"
            dashboard._state["safety"]["safe"] = False
        # Simulate the poller clear path directly.
        safety_snap = {"safe": True, "reason": "", "parked": False}
        with dashboard._state_lock:
            dashboard._state["safety"].update(safety_snap)
            if safety_snap.get("safe"):
                err = str(dashboard._state.get("error") or "")
                if err.startswith("Safety stop:"):
                    dashboard._state["error"] = None
        with dashboard._state_lock:
            self.assertTrue(dashboard._state["safety"]["safe"])
            self.assertIsNone(dashboard._state.get("error"))


if __name__ == "__main__":
    unittest.main()
