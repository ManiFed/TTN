#!/usr/bin/env python3
"""Regression: stale dawn Safety stop text must clear when safety.safe is true (#53)."""

import unittest
from unittest.mock import MagicMock

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
            gen = dashboard._safety_error_gen
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
                self.assertEqual(dashboard._safety_error_gen, gen)
        finally:
            dashboard._safety_mgr = prev
            with dashboard._state_lock:
                dashboard._state["error"] = None

    def test_poller_clears_stale_safety_stop_when_snap_safe(self):
        with dashboard._state_lock:
            dashboard._state["error"] = "Safety stop: dawn — leftover"
            dashboard._state["safety"]["safe"] = False
            gen_at_check = dashboard._safety_error_gen
        safety_snap = {"safe": True, "reason": "", "parked": False}
        with dashboard._state_lock:
            dashboard._state["safety"].update(safety_snap)
            dashboard._clear_safety_stop_if_current(
                safety_snap, gen_at_check=gen_at_check)
        with dashboard._state_lock:
            self.assertTrue(dashboard._state["safety"]["safe"])
            self.assertIsNone(dashboard._state.get("error"))

    def test_stale_safe_snap_does_not_clear_newer_stop(self):
        """Codex #57: fresh trip after safe snapshot must keep its banner."""
        with dashboard._state_lock:
            dashboard._state["error"] = "Safety stop: dawn — leftover"
            gen_at_check = dashboard._safety_error_gen
        # A fresher emergency trip lands before the stale clear runs.
        with dashboard._state_lock:
            dashboard._safety_error_gen = gen_at_check + 1
            dashboard._state["error"] = "Safety stop: telescope unreachable"
        safety_snap = {"safe": True, "reason": "", "parked": False}
        with dashboard._state_lock:
            dashboard._clear_safety_stop_if_current(
                safety_snap, gen_at_check=gen_at_check)
            self.assertEqual(
                dashboard._state["error"],
                "Safety stop: telescope unreachable")
        # Cleanup
        with dashboard._state_lock:
            dashboard._state["error"] = None

    def test_api_status_preserves_newer_stop_after_stale_safe(self):
        fake_mgr = MagicMock()
        fake_mgr.status.return_value = {"safe": True, "reason": "", "parked": False}
        with dashboard._state_lock:
            dashboard._state["error"] = "Safety stop: dawn — leftover"
            # Simulate gen bump that api_status will observe as mismatched.
            # We bump after the handler samples gen by patching via side effect.
        prev = dashboard._safety_mgr
        dashboard._safety_mgr = fake_mgr

        real_status = fake_mgr.status

        def status_and_trip():
            # Between api_status's gen sample and clear, a new trip lands.
            with dashboard._state_lock:
                dashboard._safety_error_gen += 1
                dashboard._state["error"] = "Safety stop: disconnect"
            return {"safe": True, "reason": "", "parked": False}

        fake_mgr.status.side_effect = status_and_trip
        try:
            resp = self.client.get("/api/status")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body.get("error"), "Safety stop: disconnect")
            with dashboard._state_lock:
                self.assertEqual(
                    dashboard._state.get("error"),
                    "Safety stop: disconnect")
        finally:
            dashboard._safety_mgr = prev
            with dashboard._state_lock:
                dashboard._state["error"] = None


if __name__ == "__main__":
    unittest.main()
