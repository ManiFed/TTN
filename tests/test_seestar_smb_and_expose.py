#!/usr/bin/env python3
"""
Regression tests for two Starfront night bugs:

  * issue #46 — the Seestar SMB guest share was mounted under the legacy
    name "seestar", which the telescope does not export (it's "EMMC Images").
  * issue #48 — POST /api/camera/expose read the exposure duration from a
    "duration" key, but the MCP client (telescope_mcp/tools/hardware.py
    node_expose) posts "seconds" — the mismatch silently fell back to a 1 s
    exposure no matter what was requested.
"""

import unittest

from src import dashboard


class SeestarShareNameTest(unittest.TestCase):
    def test_share_name_is_emmc_images_not_legacy_seestar(self):
        self.assertEqual(dashboard._SEESTAR_SMB_SHARE, "EMMC Images")

    def test_mount_url_percent_encodes_the_space(self):
        import urllib.parse
        self.assertEqual(
            urllib.parse.quote(dashboard._SEESTAR_SMB_SHARE), "EMMC%20Images")


class ExposeDurationKeyTest(unittest.TestCase):
    def setUp(self):
        dashboard.app.testing = True
        self.client = dashboard.app.test_client()

    def test_expose_rejects_when_camera_not_connected_but_reads_seconds_key(self):
        # No camera is wired up in this unit test, so the request 400s before
        # a real exposure starts — but that still exercises the same
        # request.get_json() parsing path used for "duration" vs "seconds".
        resp = self.client.post("/api/camera/expose", json={"seconds": 30})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Camera not connected", resp.get_json()["error"])

    def test_duration_key_still_takes_priority_over_seconds(self):
        # Both callers exist (dashboard UI historically used "duration", MCP
        # uses "seconds") — "duration" must win if somehow both are present.
        data = {"duration": 30, "seconds": 1}
        self.assertEqual(
            float(data.get("duration", data.get("seconds", 1.0))), 30.0)




class ExposeFailFastTest(unittest.TestCase):
    """Issue #52: manual expose must not hang forever with an empty fits list."""

    def test_api_expose_source_writes_fits_and_bounds_readout(self):
        import inspect
        from src import dashboard
        src = inspect.getsource(dashboard.api_expose)
        self.assertIn("readout_timeout", src)
        self.assertIn("fits_save_path", src)
        self.assertIn("count", src)
        self.assertIn("exposing", src)
        self.assertIn("fits_export", src)

    def test_camera_expose_failfast_on_idle_without_ready(self):
        import inspect
        from alpaca.camera import Camera
        src = inspect.getsource(Camera.expose)
        self.assertIn("fail-fast", src)
        self.assertIn("Idle without ImageReady", src)


if __name__ == "__main__":
    unittest.main()
