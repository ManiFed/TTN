#!/usr/bin/env python3
"""Starfront 2026-09-20/21: wcs_transform_failed (1 vs 2 world inputs).

Live SS Cyg FITS had CRVAL/CD-style headers that astropy loaded as a
*non-celestial* WCS.  ``world_to_pixel(SkyCoord)`` then raised::

    Number of world inputs (1) does not match expected (2)

and ``pixel_to_world`` returned a list (``'list' object has no attribute 'ra'``).

These tests reproduce that failure mode and prove celestial validation +
pointing fallback + transform helpers fix it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
import astropy.units as u

import src.photometry as phot


def _write_non_celestial_cd_fits(path: Path, ra_deg: float, dec_deg: float) -> None:
    """Seestar-like header: CRVAL1/2 + CD1_1/CD2_2 but empty CTYPE (non-celestial)."""
    h, w = 200, 200
    data = np.random.normal(100.0, 5.0, size=(h, w)).astype(np.float32)
    data[100, 100] += 5000.0
    hdr = fits.Header()
    hdr["SIMPLE"] = True
    hdr["BITPIX"] = -32
    hdr["NAXIS"] = 2
    hdr["NAXIS1"] = w
    hdr["NAXIS2"] = h
    hdr["OBJECT"] = "SS Cyg"
    hdr["RA"] = ra_deg
    hdr["DEC"] = dec_deg
    hdr["IMAGETYP"] = "LIGHT"
    # Partial / non-celestial WCS — exactly the Starfront failure seed.
    hdr["CRPIX1"] = w / 2.0
    hdr["CRPIX2"] = h / 2.0
    hdr["CRVAL1"] = ra_deg
    hdr["CRVAL2"] = dec_deg
    ps = 2.4 / 3600.0
    hdr["CD1_1"] = -ps
    hdr["CD2_2"] = ps
    # Deliberately omit CTYPE1/CTYPE2 (or leave empty) so has_celestial is False.
    fits.PrimaryHDU(data=data, header=hdr).writeto(path, overwrite=True)


class WcsTransformWorldInputsStarfrontTest(unittest.TestCase):
    """Reproduce and fix Starfront wcs_transform_failed."""

    SS_CYG_RA = 325.67892
    SS_CYG_DEC = 43.58472

    def test_non_celestial_wcs_reproduces_starfront_world_inputs_error(self):
        """Document the exact pre-fix failure mode on a non-celestial CD header."""
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "sscyg_bad_wcs.fits"
            _write_non_celestial_cd_fits(fits_path, self.SS_CYG_RA, self.SS_CYG_DEC)
            with fits.open(fits_path) as hdul:
                wcs = WCS(hdul[0].header, naxis=2)
            self.assertFalse(wcs.has_celestial)
            sky = SkyCoord(ra=self.SS_CYG_RA * u.deg, dec=self.SS_CYG_DEC * u.deg)
            with self.assertRaises(ValueError) as ctx:
                wcs.world_to_pixel(sky)
            self.assertIn("world inputs", str(ctx.exception).lower())
            # Same root cause as plate_solver "'list' object has no attribute 'ra'"
            out = wcs.pixel_to_world(100.0, 100.0)
            self.assertIsInstance(out, list)
            with self.assertRaises(AttributeError) as ctx2:
                _ = out.ra  # type: ignore[attr-defined]
            self.assertIn("list", str(ctx2.exception).lower())

    def test_header_has_celestial_wcs_rejects_non_celestial_cd(self):
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "sscyg_bad_wcs.fits"
            _write_non_celestial_cd_fits(fits_path, self.SS_CYG_RA, self.SS_CYG_DEC)
            with fits.open(fits_path) as hdul:
                hdr = hdul[0].header
                self.assertFalse(phot._header_has_celestial_wcs(hdr))
                self.assertIsNone(phot._wcs_from_header(hdr))

    def test_ensure_wcs_falls_back_when_header_wcs_non_celestial(self):
        """Non-celestial CRVAL+CD must NOT be accepted as source='header'."""
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "sscyg_bad_wcs.fits"
            _write_non_celestial_cd_fits(fits_path, self.SS_CYG_RA, self.SS_CYG_DEC)
            timed_out = phot.AstapResult(False, "ASTAP timed out after 90 s")
            with patch.object(phot, "_run_astap", return_value=timed_out):
                src = phot._ensure_wcs(
                    str(fits_path), self.SS_CYG_RA, self.SS_CYG_DEC,
                    solver="astap", pixel_scale=2.4,
                )
            self.assertEqual(src, "pointing")
            self.assertTrue(phot._fits_celestial_wcs_ok(str(fits_path)))
            with fits.open(fits_path) as hdul:
                self.assertEqual(hdul[0].header.get("BS_WCS"), "pointing")
                self.assertTrue(phot._header_has_celestial_wcs(hdul[0].header))
                wcs = phot._wcs_from_header(hdul[0].header)
            self.assertIsNotNone(wcs)
            sky = SkyCoord(ra=self.SS_CYG_RA * u.deg, dec=self.SS_CYG_DEC * u.deg)
            tx, ty = phot._world_to_pixel_sky(wcs, sky)
            self.assertTrue(0 <= tx < 200)
            self.assertTrue(0 <= ty < 200)
            sky2 = phot._pixel_to_skycoord(wcs, tx, ty)
            self.assertIsNotNone(sky2)
            self.assertAlmostEqual(float(sky2.ra.deg), self.SS_CYG_RA, places=3)

    def test_world_to_pixel_sky_fallback_on_non_celestial(self):
        """Helper must not raise the Starfront 1-vs-2 error on a bad WCS."""
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "sscyg_bad_wcs.fits"
            _write_non_celestial_cd_fits(fits_path, self.SS_CYG_RA, self.SS_CYG_DEC)
            with fits.open(fits_path) as hdul:
                wcs = WCS(hdul[0].header, naxis=2)
            sky = SkyCoord(ra=self.SS_CYG_RA * u.deg, dec=self.SS_CYG_DEC * u.deg)
            tx, ty = phot._world_to_pixel_sky(wcs, sky)
            # Fallback path must return finite in-frame pixels (exact CRPIX
            # rounding differs for non-celestial CD); must NOT raise 1-vs-2.
            self.assertTrue(np.isfinite(tx) and np.isfinite(ty))
            self.assertTrue(0 <= tx < 200 and 0 <= ty < 200)
            sky2 = phot._pixel_to_skycoord(wcs, 100.0, 100.0)
            self.assertIsNotNone(sky2)
            self.assertTrue(hasattr(sky2, "ra"))

    def test_pointing_inject_strips_non_celestial_and_is_skycoord_safe(self):
        with tempfile.TemporaryDirectory() as td:
            fits_path = Path(td) / "sscyg_bad_wcs.fits"
            _write_non_celestial_cd_fits(fits_path, self.SS_CYG_RA, self.SS_CYG_DEC)
            ok = phot._inject_pointing_wcs(
                str(fits_path), self.SS_CYG_RA, self.SS_CYG_DEC, 2.4
            )
            self.assertTrue(ok)
            with fits.open(fits_path) as hdul:
                wcs = phot._wcs_from_header(hdul[0].header)
            self.assertIsNotNone(wcs)
            self.assertTrue(wcs.has_celestial)
            sky = SkyCoord(ra=self.SS_CYG_RA * u.deg, dec=self.SS_CYG_DEC * u.deg)
            # Must succeed via high-level SkyCoord path (celestial WCS).
            tx, ty = wcs.world_to_pixel(sky)
            self.assertTrue(np.isfinite(float(tx)) and np.isfinite(float(ty)))


if __name__ == "__main__":
    unittest.main()
