#!/usr/bin/env python3
"""Manual fits_export frames must reach photometry (issue #70).

Also covers issue #89 (target name / AUID override for Manual RA frames)
and issue #87 (AAVSO WebObs HTTP status+body logging).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.dashboard as dash


class FitsAlreadyPhotometeredTest(unittest.TestCase):
    def test_detects_enriched_export(self):
        from astropy.io import fits
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "enriched.fits")
            hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32))
            hdu.header["SWCREATE"] = "The Telescope Net Node node_x"
            hdu.header["DATE-BLD"] = "2026-09-08T00:00:00Z"
            hdu.header["HISTORY"] = "Differential photometry: mag=10.0+/-0.01 snr=50 quality=ok"
            hdu.writeto(path)
            self.assertTrue(dash._fits_already_photometered(path))

    def test_manual_frame_not_marked(self):
        from astropy.io import fits
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "manual.fits")
            hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32))
            hdu.header["OBJECT"] = "Manual RA 15.8h"
            hdu.writeto(path)
            self.assertFalse(dash._fits_already_photometered(path))


class PhotometryEnqueueApiTest(unittest.TestCase):
    def test_enqueue_accepts_fits_export_path(self):
        from astropy.io import fits
        import numpy as np
        client = dash.app.test_client()
        with tempfile.TemporaryDirectory() as td:
            export = os.path.join(td, "fits_export", "2026-09-08")
            os.makedirs(export)
            path = os.path.join(export, "Manual.fits")
            fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32)).writeto(path)
            enqueued = []
            with patch.object(dash, "_fits_export_dir", return_value=os.path.join(td, "fits_export")), \
                 patch.object(dash, "_enqueue_photometry",
                              side_effect=lambda p, target_name=None: enqueued.append(
                                  (p, target_name))):
                resp = client.post("/api/photometry/enqueue", json={"path": path})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            self.assertEqual(enqueued, [(os.path.realpath(path), None)])

    def test_enqueue_rejects_outside_roots(self):
        from astropy.io import fits
        import numpy as np
        client = dash.app.test_client()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "elsewhere.fits")
            fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32)).writeto(path)
            with patch.object(dash, "_fits_export_dir", return_value=os.path.join(td, "fits_export")):
                resp = client.post("/api/photometry/enqueue", json={"path": path})
            self.assertEqual(resp.status_code, 403)


class OnNewFitsExportSkipTest(unittest.TestCase):
    def test_skips_photometered_exports(self):
        called = []
        with patch.object(dash, "_fits_already_photometered", return_value=True), \
             patch.object(dash, "_on_new_fits", side_effect=lambda info: called.append(info)):
            dash._on_new_fits_export({"path": "/tmp/x.fits"})
        self.assertEqual(called, [])

    def test_forwards_manual_frames(self):
        called = []
        with patch.object(dash, "_fits_already_photometered", return_value=False), \
             patch.object(dash, "_on_new_fits", side_effect=lambda info: called.append(info)):
            dash._on_new_fits_export({"path": "/tmp/manual.fits"})
        self.assertEqual(len(called), 1)


class PhotometryTargetOverrideApiTest(unittest.TestCase):
    """Issue #89: MCP/manual photometry must accept target name / AUID override."""

    def test_enqueue_passes_target_name_override(self):
        from astropy.io import fits
        import numpy as np
        client = dash.app.test_client()
        with tempfile.TemporaryDirectory() as td:
            export = os.path.join(td, "fits_export", "2026-09-09")
            os.makedirs(export)
            path = os.path.join(export, "Manual_RA.fits")
            hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32))
            hdu.header["OBJECT"] = "Manual RA 21.7147h"
            hdu.writeto(path)
            enqueued = []
            with patch.object(dash, "_fits_export_dir",
                              return_value=os.path.join(td, "fits_export")), \
                 patch.object(dash, "_enqueue_photometry",
                              side_effect=lambda p, target_name=None: enqueued.append(
                                  (p, target_name))):
                resp = client.post("/api/photometry/enqueue", json={
                    "path": path,
                    "target_name": "SS Cyg",
                    "auid": "000-BCP-220",
                })
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["target_name"], "SS Cyg")
            self.assertEqual(enqueued, [(os.path.realpath(path), "SS Cyg")])

    def test_enqueue_accepts_auid_alone(self):
        from astropy.io import fits
        import numpy as np
        client = dash.app.test_client()
        with tempfile.TemporaryDirectory() as td:
            export = os.path.join(td, "fits_export", "2026-09-09")
            os.makedirs(export)
            path = os.path.join(export, "Manual.fits")
            fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32)).writeto(path)
            enqueued = []
            with patch.object(dash, "_fits_export_dir",
                              return_value=os.path.join(td, "fits_export")), \
                 patch.object(dash, "_enqueue_photometry",
                              side_effect=lambda p, target_name=None: enqueued.append(
                                  (p, target_name))):
                resp = client.post("/api/photometry/enqueue", json={
                    "path": path, "auid": "000-BCP-220",
                })
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["target_name"], "000-BCP-220")
            self.assertEqual(enqueued, [(os.path.realpath(path), "000-BCP-220")])

    def test_enqueue_job_dict_carries_override(self):
        got = []

        class _Q:
            def put_nowait(self, job):
                got.append(job)

            def qsize(self):
                return len(got)

        with patch.object(dash, "_phot_queue", _Q()):
            dash._enqueue_photometry("/tmp/x.fits", target_name="SS Cyg")
        self.assertEqual(got, [{"path": "/tmp/x.fits", "target_name": "SS Cyg"}])

    def test_run_photometry_bg_injects_override_and_refuses_mismatch(self):
        calls = []

        def fake_pipeline(fits_path, cfg):
            calls.append(cfg["photometry"]["target"]["name"])
            return {
                "target_name": "Manual RA 21.7147h",
                "magnitude": 1.0,
                "uncertainty": 0.1,
                "snr": 10,
                "quality_flag": "poor",
                "bjd": 2460000.5,
                "sky_mag": None,
            }

        with patch.object(dash, "_load_config", return_value={"photometry": {}}), \
             patch.object(dash, "_run_photometry", side_effect=fake_pipeline):
            with dash._state_lock:
                dash._state["photometry"]["last_result"] = "sentinel"
            dash._run_photometry_bg("/tmp/manual.fits", target_name="SS Cyg")
            with dash._state_lock:
                self.assertIsNone(dash._state["photometry"]["last_result"])
        self.assertEqual(calls, ["SS Cyg"])

    def test_api_expose_source_reads_target_override_keys(self):
        import inspect
        src = inspect.getsource(dash.api_expose)
        self.assertIn("target_name", src)
        self.assertIn("auid", src)
        self.assertIn("expose_target", src)


class PhotometryConfigAuidOverrideTest(unittest.TestCase):
    def _write_manual_fits(self, td):
        from astropy.io import fits
        import numpy as np
        path = os.path.join(td, "manual.fits")
        hdu = fits.PrimaryHDU(np.zeros((32, 32), dtype=np.float32))
        hdu.header["OBJECT"] = "Manual RA 21.7147h"
        hdu.header["RA"] = 325.72
        hdu.header["DEC"] = 43.58
        hdu.header["IMAGETYP"] = "LIGHT"
        hdu.writeto(path)
        return path

    def test_pipeline_prefers_config_name_over_fits_object(self):
        from src import photometry as phot
        with tempfile.TemporaryDirectory() as td:
            path = self._write_manual_fits(td)
            cfg = {"photometry": {
                "target": {"name": "SS Cyg", "auid": "000-BCP-220"},
                "solver": "none",
            }}
            with patch.object(phot, "_ensure_wcs", return_value=""):
                _meas, rej = phot.run_pipeline_ex(path, cfg)
            self.assertIsNotNone(rej)
            self.assertEqual(rej.get("target_name"), "SS Cyg")

    def test_pipeline_uses_auid_when_name_absent(self):
        from src import photometry as phot
        with tempfile.TemporaryDirectory() as td:
            path = self._write_manual_fits(td)
            cfg = {"photometry": {
                "target": {"auid": "000-BCP-220"},
                "solver": "none",
            }}
            with patch.object(phot, "_ensure_wcs", return_value=""):
                _meas, rej = phot.run_pipeline_ex(path, cfg)
            self.assertIsNotNone(rej)
            self.assertEqual(rej.get("target_name"), "000-BCP-220")


class AavsoHttpLoggingTest(unittest.TestCase):
    """Issue #87: every WebObs POST must log HTTP status + body snippet."""

    def test_post_to_webobs_logs_status_and_body(self):
        from src import aavso_submission as aavso
        import logging
        import requests

        class _Resp:
            status_code = 200
            text = "Thanks! 1 observation(s) were uploaded successfully."
            headers = {"Content-Type": "text/html; charset=utf-8"}

        logs = []

        class _H(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        h = _H()
        aavso.logger.setLevel(logging.INFO)
        aavso.logger.addHandler(h)
        try:
            with patch.object(requests, "post", return_value=_Resp()):
                with tempfile.TemporaryDirectory() as td:
                    result = aavso._post_to_webobs(
                        "text", "u", "p", "https://example.test/webobs",
                        os.path.join(td, "x.txt"),
                        os.path.join(td, "x_response.txt"),
                    )
        finally:
            aavso.logger.removeHandler(h)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(
            any("WebObs HTTP 200" in m and "body=" in m for m in logs),
            logs,
        )


class McpTargetOverrideSurfaceTest(unittest.TestCase):
    def test_hardware_tools_accept_target_override(self):
        src = Path("telescope_mcp/tools/hardware.py").read_text()
        self.assertIn("target_name", src)
        self.assertIn("auid", src)
        self.assertIn("def node_expose(", src)
        self.assertIn("def node_photometry_enqueue(", src)
        # Regression: node_aavso must keep its @server.tool decorator.
        self.assertIn("@server.tool()\n    def node_aavso", src.replace("\r\n", "\n"))


if __name__ == "__main__":
    unittest.main()
