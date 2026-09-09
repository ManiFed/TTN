#!/usr/bin/env python3
"""Regression: approximate/pointing WCS must keep northern Dec (SS Cyg).

Starfront NodeAgent 1.0.68 saw ASTAP rc=1 then pointing WCS with
RA≈325.85° Dec=−43.59° while SS Cyg is Dec≈+43.6°. Wrong-sign Dec
yields no valid comps / no ZP.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import numpy as np
from astropy.io import fits

import src.photometry as P


# SS Cyg ≈ RA 21.7222h = 325.833°, Dec +43.5864°
_SS_CYG_RA = 325.8333
_SS_CYG_DEC = 43.5864


class DecHemisphereMirrorTest(unittest.TestCase):
    def test_detects_ss_cyg_mirror(self):
        self.assertTrue(P._dec_hemisphere_mirrored(-43.5867, _SS_CYG_DEC))
        self.assertFalse(P._dec_hemisphere_mirrored(_SS_CYG_DEC, _SS_CYG_DEC))
        self.assertFalse(P._dec_hemisphere_mirrored(-20.0, _SS_CYG_DEC))  # real south

    def test_resolve_prefers_northern_target(self):
        ra, dec = P._resolve_pointing_radec(
            325.854, -43.5867, _SS_CYG_RA, _SS_CYG_DEC,
        )
        self.assertAlmostEqual(ra, _SS_CYG_RA, places=3)
        self.assertAlmostEqual(dec, _SS_CYG_DEC, places=3)

    def test_resolve_keeps_mount_when_consistent(self):
        ra, dec = P._resolve_pointing_radec(
            325.854, 43.5900, _SS_CYG_RA, _SS_CYG_DEC,
        )
        self.assertAlmostEqual(ra, 325.854, places=3)
        self.assertAlmostEqual(dec, 43.5900, places=3)


class InjectPointingWcsDecSignTest(unittest.TestCase):
    def _write(self, td, *, ra, dec, crval2=None, cd=False):
        path = os.path.join(td, "sscyg.fits")
        data = np.zeros((64, 64), dtype=np.float32)
        data[32, 32] = 1000.0
        hdu = fits.PrimaryHDU(data)
        hdu.header["OBJECT"] = "SS Cyg"
        hdu.header["RA"] = ra
        hdu.header["DEC"] = dec
        hdu.header["IMAGETYP"] = "LIGHT"
        if crval2 is not None:
            hdu.header["CRVAL1"] = ra
            hdu.header["CRVAL2"] = crval2
            hdu.header["CRPIX1"] = 32.5
            hdu.header["CRPIX2"] = 32.5
            hdu.header["CTYPE1"] = "RA---TAN"
            hdu.header["CTYPE2"] = "DEC--TAN"
            if cd:
                hdu.header["CD1_1"] = -2.4 / 3600.0
                hdu.header["CD1_2"] = 0.0
                hdu.header["CD2_1"] = 0.0
                hdu.header["CD2_2"] = 2.4 / 3600.0
            else:
                hdu.header["CDELT1"] = -2.4 / 3600.0
                hdu.header["CDELT2"] = 2.4 / 3600.0
        hdu.writeto(path)
        return path

    def test_ss_cyg_flipped_header_dec_keeps_positive_crval2(self):
        """Header DEC south, pipeline target north → CRVAL2 stays north."""
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, ra=325.854, dec=-43.5867)
            ok = P._inject_pointing_wcs(path, _SS_CYG_RA, _SS_CYG_DEC, 2.4)
            self.assertTrue(ok)
            with fits.open(path) as hdul:
                hdr = hdul[0].header
                self.assertEqual(hdr.get("BS_WCS"), "pointing")
                self.assertAlmostEqual(float(hdr["CRVAL2"]), _SS_CYG_DEC, places=3)
                self.assertGreater(float(hdr["CRVAL2"]), 0.0)
                self.assertAlmostEqual(float(hdr["DEC"]), _SS_CYG_DEC, places=3)
                self.assertNotIn("CD1_1", hdr)

    def test_strips_stale_cd_matrix_before_pointing(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(
                td, ra=325.854, dec=-43.5867, crval2=-43.5867, cd=True,
            )
            self.assertTrue(P._inject_pointing_wcs(path, _SS_CYG_RA, _SS_CYG_DEC, 2.4))
            with fits.open(path) as hdul:
                hdr = hdul[0].header
                self.assertNotIn("CD1_1", hdr)
                self.assertNotIn("CD2_2", hdr)
                self.assertIn("CDELT1", hdr)
                self.assertAlmostEqual(float(hdr["CRVAL2"]), _SS_CYG_DEC, places=3)

    def test_pipeline_config_coords_override_mirrored_fits_dec(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, ra=325.854, dec=-43.5867)
            cfg = {"photometry": {
                "target": {
                    "name": "SS Cyg",
                    "auid": "000-BCP-220",
                    "ra_deg": _SS_CYG_RA,
                    "dec_deg": _SS_CYG_DEC,
                },
                "solver": "none",
                "pixel_scale": 2.4,
            }}
            seen = {}

            def fake_ensure(fits_path, ra_deg, dec_deg, **kwargs):
                seen["ra"] = ra_deg
                seen["dec"] = dec_deg
                P._inject_pointing_wcs(fits_path, ra_deg, dec_deg, 2.4)
                return "pointing"

            with mock.patch.object(P, "_ensure_wcs", side_effect=fake_ensure), \
                 mock.patch.object(P, "_gather_comparison_stars", return_value=[]), \
                 mock.patch.object(P, "_estimate_fwhm", return_value=4.0):
                meas, rej = P.run_pipeline_ex(path, cfg)

            self.assertAlmostEqual(seen["dec"], _SS_CYG_DEC, places=3)
            self.assertGreater(seen["dec"], 0.0)
            self.assertIsNone(meas)
            self.assertIsNotNone(rej)


if __name__ == "__main__":
    unittest.main()
