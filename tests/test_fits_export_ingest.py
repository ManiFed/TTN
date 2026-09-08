#!/usr/bin/env python3
"""Manual fits_export frames must reach photometry (issue #70)."""

import os
import tempfile
import unittest
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
                 patch.object(dash, "_enqueue_photometry", side_effect=lambda p: enqueued.append(p)):
                resp = client.post("/api/photometry/enqueue", json={"path": path})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["ok"])
            self.assertEqual(enqueued, [os.path.realpath(path)])

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


if __name__ == "__main__":
    unittest.main()
