#!/usr/bin/env python3
"""Issues #122 / #123: photometry POST, empty-FITS error, park reconnect, no stale FITS.

#122 (2026-09-14 ~2:00am ET): ImageReady true but no fits_export FITS;
POST /api/photometry → bare 405.

#123 (2026-09-14 ~3:15am ET): After SS Cyg slew, expose drops Seestar camera
and parks at 20.22h+38.4; operator enqueued yesterday's FITS.

Expected:
  - POST /api/photometry with path aliases to enqueue; without path → JSON 405
  - ImageReady / expose finished with empty fits_written sets camera.error
  - Manual expose mid-expose park abort+reslew; camera-drop reconnect
  - Previous-night fits_export paths are never enqueued

Run with:  python3 -m pytest tests/test_issue_122_123.py
"""

from __future__ import annotations

import inspect
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import src.dashboard as dash


SS_CYG_RA_H = 21.71187
SS_CYG_DEC = 43.586
CYGNUS_RA_H = 20.219722
CYGNUS_DEC = 38.428889


class PhotometryPostAliasTest(unittest.TestCase):
    def test_route_accepts_get_and_post(self):
        mod = inspect.getsource(dash)
        idx = mod.index("def api_photometry(")
        deco_block = mod[max(0, idx - 400):idx]
        self.assertIn('methods=["GET", "POST"]', deco_block)
        self.assertIn("/api/photometry", deco_block)

    def test_post_without_path_returns_json_405_hint(self):
        src = inspect.getsource(dash.api_photometry)
        self.assertIn("use POST /api/photometry/enqueue", src)
        self.assertIn('"/api/photometry/enqueue"', src)
        self.assertIn("405", src)
        self.assertIn("api_photometry_enqueue", src)

    def test_post_with_path_aliases_to_enqueue(self):
        src = inspect.getsource(dash.api_photometry)
        self.assertIn('data.get("path") or data.get("fits_path")', src)
        self.assertIn("return api_photometry_enqueue()", src)


class EmptyFitsWrittenErrorTest(unittest.TestCase):
    def test_api_expose_sets_camera_error_when_fits_written_empty(self):
        src = inspect.getsource(dash.api_expose)
        self.assertIn("fits_written empty", src)
        self.assertIn("issue #122", src)
        self.assertIn("ImageReady", src)

    def test_capture_raises_when_science_path_download_fails(self):
        src = inspect.getsource(dash._capture_image)
        self.assertIn("ImageReady but FITS download/write failed", src)
        self.assertIn("if fits_path:", src)


class PreviousNightFitsGuardTest(unittest.TestCase):
    def test_detects_yesterday_under_fits_export(self):
        tonight = dash._fits_export_night_utc()
        # Build a synthetic yesterday path.
        y, m, d = map(int, tonight.split("-"))
        import datetime as _dt
        yesterday = (_dt.date(y, m, d) - _dt.timedelta(days=1)).isoformat()
        path = f"/tmp/fits_export/{yesterday}/SS Cyg_01_deadbeef.fits"
        self.assertEqual(dash._fits_path_night_utc(path), yesterday)
        self.assertTrue(dash._is_previous_night_fits(path, tonight=tonight))
        today_path = f"/tmp/fits_export/{tonight}/SS Cyg_01_alive.fits"
        self.assertFalse(dash._is_previous_night_fits(today_path, tonight=tonight))

    def test_enqueue_refuses_previous_night(self):
        src = inspect.getsource(dash._enqueue_photometry)
        self.assertIn("_is_previous_night_fits", src)
        self.assertIn("issue #123", src)
        src2 = inspect.getsource(dash.api_photometry_enqueue)
        self.assertIn("_is_previous_night_fits", src2)
        self.assertIn("409", src2)

    def test_enqueue_drops_previous_night_without_queueing(self):
        y, m, d = map(int, dash._fits_export_night_utc().split("-"))
        import datetime as _dt
        yesterday = (_dt.date(y, m, d) - _dt.timedelta(days=1)).isoformat()
        path = f"/workspace/fits_export/{yesterday}/SS Cyg_01_fdb4984621.fits"
        put = MagicMock()
        with patch.object(dash, "_phot_queue", MagicMock(put_nowait=put)), \
             patch.object(dash, "_notify_commissioning_fits"), \
             patch.object(dash, "_telemetry", MagicMock()):
            dash._enqueue_photometry(path, target_name="SS Cyg")
        put.assert_not_called()


class ManualExposeParkAndReconnectTest(unittest.TestCase):
    def test_api_expose_has_mid_expose_park_cancel_and_reslew(self):
        src = inspect.getsource(dash.api_expose)
        self.assertIn("_cancel_if_parked_or_abort", src)
        self.assertIn("_mount_left_target", src)
        self.assertIn("_reslew_manual", src)
        self.assertIn("at-ImageReady manual", src)
        self.assertIn("_reconnect_camera_after_drop", src)
        self.assertIn("issue #123", src)

    def test_cygnus_park_vs_ss_cyg_detected(self):
        self.assertTrue(
            dash._pointing_off_target(
                CYGNUS_RA_H, CYGNUS_DEC, SS_CYG_RA_H, SS_CYG_DEC, tol_deg=1.0,
            )
        )

    def test_camera_unreachable_classifier(self):
        self.assertTrue(
            dash._camera_unreachable_exc(
                TimeoutError("HTTPSConnectionPool timed out")
            )
        )
        self.assertTrue(
            dash._camera_unreachable_exc(
                ConnectionError("Can't reach the camera at 172.22.5.229:32323")
            )
        )
        self.assertFalse(dash._camera_unreachable_exc(ValueError("bad binning")))

    def test_schedule_reconnect_raises_cancelled_for_retry(self):
        src = inspect.getsource(dash._run_schedule_observation)
        self.assertIn("_reconnect_camera_after_drop", src)
        self.assertIn("camera dropped mid-expose", src)
        self.assertIn("issue #123", src)

    def test_api_slew_remembers_commanded_coords(self):
        src = inspect.getsource(dash.api_slew)
        self.assertIn("_remember_commanded_slew", src)
        dash._remember_commanded_slew(SS_CYG_RA_H, SS_CYG_DEC, label="test")
        ra, dec = dash._commanded_slew_radec()
        self.assertAlmostEqual(ra, SS_CYG_RA_H, places=5)
        self.assertAlmostEqual(dec, SS_CYG_DEC, places=5)


class MountLeftTargetSsCygTest(unittest.TestCase):
    def test_mount_left_true_for_cygnus_ish_park(self):
        fake_tel = MagicMock()
        fake_tel.ra.return_value = CYGNUS_RA_H
        fake_tel.dec.return_value = CYGNUS_DEC
        fake_tel.is_slewing.return_value = False
        with patch.object(dash, "_tel", fake_tel):
            self.assertTrue(dash._mount_left_target(SS_CYG_RA_H, SS_CYG_DEC))


if __name__ == "__main__":
    unittest.main()
