#!/usr/bin/env python3
"""Manual fits_export frames must reach photometry (issue #70).

Also covers issue #89 (target name / AUID override for Manual RA frames),
issue #79 (ASTAP fail + fallback WCS must still reach VSP when override/AUID
is set; measured=None must not be reported as override-ignored),
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
                              side_effect=lambda p, target_name=None, auid=None:
                                  enqueued.append((p, target_name, auid))):
                resp = client.post("/api/photometry/enqueue", json={"path": path})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            self.assertEqual(enqueued, [(os.path.realpath(path), None, None)])

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
                              side_effect=lambda p, target_name=None, auid=None:
                                  enqueued.append((p, target_name, auid))):
                resp = client.post("/api/photometry/enqueue", json={
                    "path": path,
                    "target_name": "SS Cyg",
                    "auid": "000-BCP-220",
                })
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["target_name"], "SS Cyg")
            self.assertEqual(body["auid"], "000-BCP-220")
            self.assertEqual(
                enqueued,
                [(os.path.realpath(path), "SS Cyg", "000-BCP-220")],
            )

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
                              side_effect=lambda p, target_name=None, auid=None:
                                  enqueued.append((p, target_name, auid))):
                resp = client.post("/api/photometry/enqueue", json={
                    "path": path, "auid": "000-BCP-220",
                })
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["target_name"], "000-BCP-220")
            self.assertEqual(body["auid"], "000-BCP-220")
            self.assertEqual(
                enqueued,
                [(os.path.realpath(path), "000-BCP-220", "000-BCP-220")],
            )

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

        def fake_pipeline_ex(fits_path, cfg):
            calls.append(cfg["photometry"]["target"]["name"])
            return ({
                "target_name": "Manual RA 21.7147h",
                "magnitude": 1.0,
                "uncertainty": 0.1,
                "snr": 10,
                "quality_flag": "poor",
                "bjd": 2460000.5,
                "sky_mag": None,
            }, None)

        with patch.object(dash, "_load_config", return_value={"photometry": {}}), \
             patch("src.photometry.run_pipeline_ex", side_effect=fake_pipeline_ex):
            with dash._state_lock:
                dash._state["photometry"]["last_result"] = "sentinel"
            dash._run_photometry_bg("/tmp/manual.fits", target_name="SS Cyg")
            with dash._state_lock:
                self.assertIsNone(dash._state["photometry"]["last_result"])
        self.assertEqual(calls, ["SS Cyg"])

    def test_run_photometry_bg_measured_none_is_not_override_ignored(self):
        """Issue #79: pipeline None after override must not look like ignore."""
        events = []

        def fake_pipeline_ex(fits_path, cfg):
            self.assertEqual(cfg["photometry"]["target"]["name"], "SS Cyg")
            self.assertEqual(cfg["photometry"]["target"]["auid"], "000-BCP-220")
            # Stale config coords cleared; no last-commanded slew in this test
            # so ra_deg/dec_deg stay absent (FITS pointing would win).
            self.assertNotIn("ra_deg", cfg["photometry"]["target"])
            self.assertNotIn("dec_deg", cfg["photometry"]["target"])
            return None, {
                "stage": "zero_point",
                "reason_code": "no_zero_point",
                "message": "no usable comparison stars",
                "target_name": "SS Cyg",
            }

        class _Tel:
            def event(self, name, severity="info", detail=None):
                events.append((name, severity, detail))

        with patch.object(dash, "_load_config", return_value={
                "photometry": {
                    "target": {"name": "", "ra_deg": 10.0, "dec_deg": 20.0},
                },
             }), \
             patch("src.photometry.run_pipeline_ex", side_effect=fake_pipeline_ex), \
             patch.object(dash, "_telemetry", _Tel()):
            with dash._state_lock:
                dash._state["photometry"]["last_result"] = "sentinel"
                dash._state["photometry"]["last_rejection"] = None
            dash._run_photometry_bg(
                "/tmp/manual.fits",
                target_name="SS Cyg",
                auid="000-BCP-220",
            )
            with dash._state_lock:
                self.assertIsNone(dash._state["photometry"]["last_result"])
                rej = dash._state["photometry"]["last_rejection"]
        self.assertIsNotNone(rej)
        self.assertEqual(rej["reason_code"], "no_zero_point")
        self.assertFalse(
            any(name == "photometry_override_ignored" for name, *_ in events),
            events,
        )

    def test_frame_has_target_recognizes_auid(self):
        cfg = {"photometry": {"target": {"auid": "000-BCP-220"}}}
        self.assertTrue(dash._frame_has_target("/nonexistent.fits", cfg))

    def test_enqueue_job_dict_carries_name_and_auid(self):
        got = []

        class _Q:
            def put_nowait(self, job):
                got.append(job)

            def qsize(self):
                return len(got)

        with patch.object(dash, "_phot_queue", _Q()):
            dash._enqueue_photometry(
                "/tmp/x.fits", target_name="SS Cyg", auid="000-BCP-220")
        self.assertEqual(
            got,
            [{"path": "/tmp/x.fits", "target_name": "SS Cyg", "auid": "000-BCP-220"}],
        )

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

    def test_override_queries_vsp_before_off_frame_abort(self):
        """Issue #79: with AUID override, VSP runs even if target looks off-frame."""
        from src import photometry as phot
        import numpy as np
        from astropy.io import fits

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "off.fits")
            data = np.zeros((64, 64), dtype=np.float32)
            data[32, 32] = 5000.0
            hdu = fits.PrimaryHDU(data)
            hdu.header["OBJECT"] = "Manual RA 21.7147h"
            hdu.header["RA"] = 325.72
            hdu.header["DEC"] = 43.58
            hdu.header["IMAGETYP"] = "LIGHT"
            hdu.writeto(path)

            vsp_ids = []

            def fake_gather(target_name, ra_deg, dec_deg, *args, **kwargs):
                vsp_ids.append(target_name)
                return [{
                    "auid": "000-AAA-001",
                    "ra_deg": ra_deg,
                    "dec_deg": dec_deg,
                    "mag_v": 12.0,
                    "mag_err": 0.02,
                    "source": "aavso_V",
                }]

            cfg = {"photometry": {
                "target": {"name": "SS Cyg", "auid": "000-BCP-220",
                           "ra_deg": 10.0, "dec_deg": 80.0},
                "pixel_scale": 2.4,
                "comparison_catalogs": ["aavso"],
            }}

            self.assertTrue(phot._inject_pointing_wcs(path, 325.72, 43.58, 2.4))
            real_wcs_cls = phot.WCS

            class _Wrap(real_wcs_cls):
                def world_to_pixel(self, *a, **k):
                    return (-50.0, -50.0)

            with patch.object(phot, "_ensure_wcs", return_value="pointing"), \
                 patch.object(phot, "_gather_comparison_stars", side_effect=fake_gather), \
                 patch.object(phot, "WCS", _Wrap):
                meas, rej = phot.run_pipeline_ex(path, cfg)

            self.assertIsNone(meas)
            self.assertIsNotNone(rej)
            self.assertEqual(rej.get("reason_code"), "target_off_frame")
            self.assertEqual(vsp_ids, ["000-BCP-220"])
            detail = rej.get("detail") or {}
            self.assertEqual(detail.get("n_comp_candidates"), 1)
            self.assertEqual(detail.get("vsp_id"), "000-BCP-220")

    def test_pointing_wcs_never_quality_good(self):
        from src import photometry as phot
        flag, reasons = phot.evaluate_quality(
            {"snr": 100, "uncertainty": 0.01, "n_comparison_stars": 5,
             "airmass": 1.1, "zp_scatter": 0.02,
             "target_saturated": False, "target_blended": False,
             "wcs_source": "pointing"},
            {},
        )
        self.assertEqual(flag, "acceptable")
        self.assertTrue(any(r["check"] == "wcs_source" for r in reasons))


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


class CommandedEqHemisphereTest(unittest.TestCase):
    """Mount-reported Dec mirrored south must not poison FITS / photometry."""

    def setUp(self):
        with dash._last_commanded_lock:
            dash._last_commanded_eq["ra_h"] = None
            dash._last_commanded_eq["dec"] = None

    def tearDown(self):
        with dash._last_commanded_lock:
            dash._last_commanded_eq["ra_h"] = None
            dash._last_commanded_eq["dec"] = None

    def test_choose_eq_keeps_northern_commanded_dec(self):
        # SS Cyg-like: commanded +43.5864°, mount reports −43.5867°.
        ra_h, dec = dash._choose_eq_for_fits(
            21.7236, -43.5867, cmd_ra_h=21.7222, cmd_dec=43.5864,
        )
        self.assertAlmostEqual(ra_h, 21.7222, places=4)
        self.assertAlmostEqual(dec, 43.5864, places=4)

    def test_choose_eq_keeps_mount_when_signs_agree(self):
        ra_h, dec = dash._choose_eq_for_fits(
            21.7236, 43.5900, cmd_ra_h=21.7222, cmd_dec=43.5864,
        )
        self.assertAlmostEqual(ra_h, 21.7236, places=4)
        self.assertAlmostEqual(dec, 43.5900, places=4)

    def test_override_seeds_commanded_coords(self):
        dash._note_commanded_eq(21.7222, 43.5864)
        seen = {}

        def fake_pipeline_ex(fits_path, cfg):
            seen.update(cfg["photometry"]["target"])
            return None, {"reason_code": "no_zero_point", "stage": "zero_point",
                          "message": "x", "target_name": "SS Cyg"}

        with patch.object(dash, "_load_config", return_value={
                "photometry": {"target": {"ra_deg": 1.0, "dec_deg": 2.0}},
             }),              patch("src.photometry.run_pipeline_ex", side_effect=fake_pipeline_ex),              patch.object(dash, "_telemetry", type("T", (), {"event": staticmethod(lambda *a, **k: None)})()):
            dash._run_photometry_bg("/tmp/manual.fits", target_name="SS Cyg")

        self.assertEqual(seen.get("name"), "SS Cyg")
        # Stale 1.0/2.0 cleared; commanded SS Cyg coords seeded instead.
        self.assertAlmostEqual(float(seen["ra_deg"]), 21.7222 * 15.0, places=3)
        self.assertAlmostEqual(float(seen["dec_deg"]), 43.5864, places=3)

