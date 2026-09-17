#!/usr/bin/env python3
"""Issue #134: ASTAP timeout / has_wcs=false must fall back to pointing WCS.

When AUID/target is known, photometry must not hard-fail solely on ASTAP miss:
use pointing/RADEC WCS (quality warn) instead of excluding all comps /
silent no_zero_point.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.io import fits

import src.photometry as phot


class AstapTimeoutPointingFallbackTest(unittest.TestCase):
    def _write_no_wcs_fits(self, path: Path, ra_deg: float, dec_deg: float) -> None:
        data = np.random.normal(100.0, 5.0, size=(200, 200)).astype(np.float32)
        # Put a fake star near center for centroid paths.
        data[100, 100] += 5000.0
        hdr = fits.Header()
        hdr["SIMPLE"] = True
        hdr["BITPIX"] = -32
        hdr["NAXIS"] = 2
        hdr["NAXIS1"] = 200
        hdr["NAXIS2"] = 200
        hdr["OBJECT"] = "T CrB"
        hdr["RA"] = ra_deg
        hdr["DEC"] = dec_deg
        hdr["IMAGETYP"] = "LIGHT"
        fits.PrimaryHDU(data=data, header=hdr).writeto(path, overwrite=True)

    def test_ensure_wcs_falls_back_on_astap_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "tcrb.fits"
            self._write_no_wcs_fits(fits_path, 239.87567, 25.92017)

            timed_out = phot.AstapResult(False, "ASTAP timed out after 90 s")
            with patch.object(phot, "_run_astap", return_value=timed_out):
                src = phot._ensure_wcs(
                    str(fits_path), 239.87567, 25.92017,
                    solver="astap", pixel_scale=None,  # must still default to 2.4
                )
            self.assertEqual(src, "pointing")
            self.assertTrue(phot._LAST_WCS_ENSURE.get("astap_timed_out"))
            self.assertIn("timed out", (phot._LAST_WCS_ENSURE.get("astap_message") or "").lower())

            with fits.open(fits_path) as hdul:
                hdr = hdul[0].header
                self.assertEqual(hdr.get("BS_WCS"), "pointing")
                self.assertIn("CRVAL1", hdr)
                self.assertIn("CDELT1", hdr)

    def test_ensure_wcs_uses_configured_scale_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "tcrb.fits"
            self._write_no_wcs_fits(fits_path, 239.87567, 25.92017)
            timed_out = phot.AstapResult(False, "ASTAP timed out after 90 s")
            with patch.object(phot, "_run_astap", return_value=timed_out):
                src = phot._ensure_wcs(
                    str(fits_path), 239.87567, 25.92017,
                    solver="astap", pixel_scale=2.4,
                )
            self.assertEqual(src, "pointing")
            self.assertAlmostEqual(float(phot._LAST_WCS_ENSURE.get("pixel_scale")), 2.4)

    def test_evaluate_quality_warns_on_pointing_wcs(self):
        flag, reasons = phot.evaluate_quality(
            {
                "snr": 13.0,
                "uncertainty": 0.08,
                "n_comparison_stars": 5,
                "airmass": 1.5,
                "zp_scatter": 0.05,
                "target_saturated": False,
                "target_blended": False,
                "wcs_source": "pointing",
            },
            {"snr_threshold": 20, "zp_scatter_max": 0.3, "zp_scatter_warn": 0.15},
        )
        self.assertEqual(flag, "acceptable")
        self.assertTrue(any(r["check"] == "wcs_source" for r in reasons))


class ZpScatterRobustnessTest(unittest.TestCase):
    """Issue #136: SNR≥10 must not hard-fail on modest zp_scatter."""

    def test_soft_zp_scatter_when_snr_clears_poor_floor(self):
        flag, reasons = phot.evaluate_quality(
            {
                "snr": 13.0,  # >= half of default snr_threshold=20
                "uncertainty": 0.12,
                "n_comparison_stars": 4,
                "airmass": 6.12,  # warn-only
                "zp_scatter": 0.35,  # > 0.30 max but <= 1.5×
                "target_saturated": False,
                "target_blended": False,
            },
            {"snr_threshold": 20, "zp_scatter_max": 0.3, "zp_scatter_warn": 0.15,
             "max_airmass": 3.0},
        )
        self.assertEqual(flag, "acceptable")
        zp = [r for r in reasons if r["check"] == "zp_scatter"][0]
        self.assertEqual(zp["outcome"], "warn")

    def test_hard_fail_when_zp_scatter_badly_wrong(self):
        flag, reasons = phot.evaluate_quality(
            {
                "snr": 13.0,
                "uncertainty": 0.12,
                "n_comparison_stars": 4,
                "airmass": 1.5,
                "zp_scatter": 0.55,  # > 1.5 × 0.30
                "target_saturated": False,
                "target_blended": False,
            },
            {"snr_threshold": 20, "zp_scatter_max": 0.3, "zp_scatter_warn": 0.15},
        )
        self.assertEqual(flag, "poor")
        zp = [r for r in reasons if r["check"] == "zp_scatter"][0]
        self.assertEqual(zp["outcome"], "fail")


if __name__ == "__main__":
    unittest.main()
