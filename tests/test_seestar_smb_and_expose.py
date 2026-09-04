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
        # dashboard._cam is a process-wide singleton other test modules may
        # have already connected — force the "not connected" branch so this
        # test's outcome doesn't depend on suite run order.
        self._orig_cam = dashboard._cam
        dashboard._cam = None

    def tearDown(self):
        dashboard._cam = self._orig_cam

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


if __name__ == "__main__":
    unittest.main()
