#!/usr/bin/env python3
"""
Universal frame ingestion Phase 3 — ingestion triage heuristics.

A synthetic fixture corpus (the plan's P3 contract): a clean linear star field
must pass; a stretched/8-bit-processed frame, a star-trailed frame, and a
sourceless junk frame must each be rejected with the right label — all before
any solver time is spent. Pure numpy/astropy/photutils, no DB.

Run with:  python3 -m unittest tests.test_triage
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

from cloud import triage

CONFIG = {"triage": {"min_sources": 8, "max_elongation": 1.8}}


def _star_field(n_stars=60, shape=(256, 256), bg=100.0, noise=3.0,
                elong=1.0, seed=1):
    """A linear frame: low sky background + Gaussian PSF stars."""
    rng = np.random.default_rng(seed)
    img = rng.normal(bg, noise, size=shape).astype("float64")
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for _ in range(n_stars):
        cx = rng.uniform(10, shape[1] - 10)
        cy = rng.uniform(10, shape[0] - 10)
        amp = rng.uniform(300, 3000)
        sx, sy = 1.6, 1.6 * elong          # elong>1 → trailed
        img += amp * np.exp(-(((xx - cx) ** 2) / (2 * sx ** 2)
                              + ((yy - cy) ** 2) / (2 * sy ** 2)))
    return img


def _write(img, header=None) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".fits", delete=False)
    hdu = fits.PrimaryHDU(data=img.astype("float32"))
    if header:
        for k, v in header.items():
            hdu.header[k] = v
    hdu.writeto(f.name, overwrite=True)
    return f.name


class TriageTest(unittest.TestCase):
    def setUp(self):
        self._paths = []

    def tearDown(self):
        for p in self._paths:
            try:
                Path(p).unlink()
            except OSError:
                pass

    def _fixture(self, img, header=None):
        p = _write(img, header)
        self._paths.append(p)
        return p

    def test_linear_star_field_passes(self):
        v = triage.classify(self._fixture(_star_field()), CONFIG)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["label"], "sky_linear")
        self.assertGreaterEqual(v["gates"]["n_sources"], 8)

    def test_sourceless_junk_rejected(self):
        # Flat noise, no stars — not a star field.
        rng = np.random.default_rng(3)
        img = rng.normal(500, 3.0, size=(256, 256))
        v = triage.classify(self._fixture(img), CONFIG)
        self.assertFalse(v["ok"])
        self.assertEqual(v["label"], "not_sky")

    def test_processing_software_header_rejected(self):
        # A real-looking field but exported through PixInsight → non-linear.
        v = triage.classify(
            self._fixture(_star_field(), {"CREATOR": "PixInsight 1.8"}), CONFIG)
        self.assertFalse(v["ok"])
        self.assertEqual(v["label"], "stretched")
        self.assertIn("pixinsight", v["reason"].lower())

    def test_stretched_white_pileup_rejected(self):
        # Aggressive stretch: half the frame pushed near white.
        img = _star_field(bg=100.0)
        img[: img.shape[0] // 2, :] = img.max()
        v = triage.classify(self._fixture(img), CONFIG)
        self.assertFalse(v["ok"])
        self.assertEqual(v["label"], "stretched")

    def test_trailed_frame_rejected(self):
        v = triage.classify(self._fixture(_star_field(elong=3.0, seed=5)), CONFIG)
        self.assertFalse(v["ok"])
        self.assertEqual(v["label"], "trailed")

    def test_disabled_triage_passes_everything(self):
        rng = np.random.default_rng(9)
        junk = rng.normal(500, 3.0, size=(256, 256))
        v = triage.classify(self._fixture(junk), {"triage": {"enabled": False}})
        self.assertTrue(v["ok"])


if __name__ == "__main__":
    unittest.main()
