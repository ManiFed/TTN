#!/usr/bin/env python3
"""
Unit tests for the two photometry features added on this branch:

  1. Astrometry.net plate-solve backend — fresh WCS per stack, with the solved
     WCS merged back into the FITS header (replacing any stale onboard WCS).
  2. Multi-catalog comparison stars — APASS/ATLAS in addition to AAVSO/Gaia,
     gathered in order until a target count is reached.

These cover the network-free, pure logic. Catalog queries and solve-field are
exercised via monkeypatched stand-ins, so the suite needs no internet or ASTAP.

Run with:  python3 -m unittest tests.test_photometry_features
"""

import os
import tempfile
import unittest

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

import src.photometry as P


class MergeCompStarsTest(unittest.TestCase):
    def test_dedup_within_separation(self):
        a = [{"ra_deg": 10.0, "dec_deg": 73.0, "mag_v": 12.0}]
        b = [
            {"ra_deg": 10.0 + 0.5 / 3600, "dec_deg": 73.0, "mag_v": 12.1},  # dup
            {"ra_deg": 10.1, "dec_deg": 73.0, "mag_v": 13.0},               # new
        ]
        merged = P._merge_comp_stars(a, b)
        self.assertEqual(len(merged), 2)

    def test_ra_threshold_scales_with_cos_dec(self):
        # At Dec=+73°, a 10 arcsec RA offset is < 5 arcsec on the sky, so it must
        # be treated as a duplicate — the regression this scaling guards against.
        a = [{"ra_deg": 10.0, "dec_deg": 73.0, "mag_v": 12.0}]
        near = [{"ra_deg": 10.0 + 10 / 3600, "dec_deg": 73.0, "mag_v": 12.2}]
        self.assertEqual(len(P._merge_comp_stars(a, near)), 1)


class GatherComparisonStarsTest(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "aavso": P._get_comparison_stars_aavso,
            "apass": P._get_comparison_stars_apass,
            "atlas": P._get_comparison_stars_atlas,
            "gaia": P._get_comparison_stars_gaia,
        }

    def tearDown(self):
        P._get_comparison_stars_aavso = self._orig["aavso"]
        P._get_comparison_stars_apass = self._orig["apass"]
        P._get_comparison_stars_atlas = self._orig["atlas"]
        P._get_comparison_stars_gaia = self._orig["gaia"]

    def test_order_and_short_circuit(self):
        calls = []
        P._get_comparison_stars_aavso = lambda *a, **k: (
            calls.append("aavso") or [{"ra_deg": 1, "dec_deg": 73, "mag_v": 11}])
        P._get_comparison_stars_apass = lambda *a, **k: (
            calls.append("apass") or [
                {"ra_deg": 2, "dec_deg": 73, "mag_v": 12},
                {"ra_deg": 3, "dec_deg": 73, "mag_v": 12.5}])
        P._get_comparison_stars_gaia = lambda *a, **k: (
            calls.append("gaia") or [{"ra_deg": 4, "dec_deg": 73, "mag_v": 13}])

        res = P._gather_comparison_stars(
            "T", 10, 73, 0.5, 15, ["aavso", "apass", "gaia"], target_count=3)
        # Stops once 3 stars are collected — gaia is never queried.
        self.assertEqual(calls, ["aavso", "apass"])
        self.assertEqual(len(res), 3)

    def test_unknown_catalog_and_query_error_tolerated(self):
        P._get_comparison_stars_aavso = lambda *a, **k: [
            {"ra_deg": 1, "dec_deg": 73, "mag_v": 11}]
        P._get_comparison_stars_atlas = lambda *a, **k: (
            _ for _ in ()).throw(RuntimeError("vizier down"))
        res = P._gather_comparison_stars(
            "T", 10, 73, 0.5, 15, ["bogus", "atlas", "aavso"], target_count=99)
        self.assertTrue(any(s["mag_v"] == 11 for s in res))


class MaskedHelperTest(unittest.TestCase):
    def test_masked_detects_missing_cells(self):
        from astropy.table import Table
        t = Table({"Vmag": np.ma.array([12.0, 0.0], mask=[False, True])})
        self.assertFalse(P._masked(t[0], "Vmag"))
        self.assertTrue(P._masked(t[1], "Vmag"))
        self.assertTrue(P._masked(t[0], "missing_column"))


class WcsInjectTest(unittest.TestCase):
    def test_strip_and_inject_replaces_wcs_preserves_rest(self):
        data = np.zeros((20, 20), dtype=np.float32)
        hdr = fits.Header()
        hdr["CRVAL1"] = 99.9    # stale onboard WCS to be replaced
        hdr["CD1_1"] = 0.001
        hdr["OBJECT"] = "T CrB"  # non-WCS card to preserve
        fpath = tempfile.mktemp(suffix=".fits")
        fits.PrimaryHDU(data, hdr).writeto(fpath, overwrite=True)

        wh = fits.Header()
        for k, v in [
            ("WCSAXES", 2), ("CTYPE1", "RA---TAN"), ("CTYPE2", "DEC--TAN"),
            ("CRVAL1", 150.0), ("CRVAL2", 73.0), ("CRPIX1", 10.5),
            ("CRPIX2", 10.5), ("CD1_1", -2e-4), ("CD1_2", 0.0),
            ("CD2_1", 0.0), ("CD2_2", 2e-4),
        ]:
            wh[k] = v
        wpath = tempfile.mktemp(suffix=".wcs")
        fits.PrimaryHDU(header=wh).writeto(wpath, overwrite=True)

        try:
            self.assertTrue(P._inject_wcs(fpath, wpath))
            with fits.open(fpath) as hd:
                out = hd[0].header
                self.assertAlmostEqual(out["CRVAL1"], 150.0)   # replaced
                self.assertEqual(out["CTYPE1"], "RA---TAN")
                self.assertEqual(out["OBJECT"], "T CrB")        # preserved
                WCS(out, naxis=2)  # constructs without error
        finally:
            for p in (fpath, wpath):
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()
