#!/usr/bin/env python3
"""
Open Aperture node-side tests: full-frame survey extraction.

Synthetic frames with injected Gaussian stars and a TAN WCS verify that
_run_survey_extraction recovers injected magnitudes, self-calibrates its
zero point from catalog matches, keys sources stably, and reports a
brightened star at its measured (not catalog) magnitude. A real solved
Seestar frame (data/fits/) smoke-tests the same path on live pixels —
no network, no solver.

Run with:  python3 -m unittest tests.test_survey_extraction
"""

import json
import math
import os
import tempfile
import unittest

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

import src.photometry as P

ZCAM_FRAME = os.path.join("data", "fits", "zcam_centered_01.fits")


def _tan_wcs(ra0=180.0, dec0=45.0, scale_arcsec=2.4, w=512, h=512) -> WCS:
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crpix = [w / 2.0, h / 2.0]
    wcs.wcs.crval = [ra0, dec0]
    s = scale_arcsec / 3600.0
    wcs.wcs.cd = [[-s, 0.0], [0.0, s]]
    return wcs


def _inject_star(data: np.ndarray, x: float, y: float, flux: float,
                 fwhm_px: float = 3.5) -> None:
    sigma = fwhm_px / 2.355
    size = int(6 * sigma) + 1
    x0, y0 = int(round(x)), int(round(y))
    yy, xx = np.mgrid[max(0, y0 - size):min(data.shape[0], y0 + size + 1),
                      max(0, x0 - size):min(data.shape[1], x0 + size + 1)]
    g = np.exp(-(((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2)))
    data[max(0, y0 - size):min(data.shape[0], y0 + size + 1),
         max(0, x0 - size):min(data.shape[1], x0 + size + 1)] += \
        flux * g / (2 * math.pi * sigma ** 2)


def _make_field(zp=22.0, star_mags=None, brightened_index=None,
                delta_mag=2.0, seed=42):
    """A synthetic frame + matching catalog.

    Stars are injected at flux = 10**(-0.4*(mag - zp)); the catalog lists
    their true mags. brightened_index injects that star delta_mag brighter
    than its catalog entry — the anomaly the survey must carry faithfully.
    """
    rng = np.random.default_rng(seed)
    w = h = 512
    wcs = _tan_wcs(w=w, h=h)
    data = rng.normal(100.0, 2.0, (h, w)).astype(np.float32)

    star_mags = star_mags or [10.5, 11.0, 11.5, 12.0, 12.3, 12.6,
                              13.0, 13.3, 13.6, 14.0]
    catalog, positions = [], []
    for i, mag in enumerate(star_mags):
        x = 60.0 + (i % 4) * 110.0 + rng.uniform(-5, 5)
        y = 60.0 + (i // 4) * 130.0 + rng.uniform(-5, 5)
        inject_mag = mag - delta_mag if i == brightened_index else mag
        _inject_star(data, x, y, 10 ** (-0.4 * (inject_mag - zp)))
        ra, dec = (float(v) for v in wcs.pixel_to_world_values(x, y))
        catalog.append({"auid": f"CAT-{i:03d}", "ra_deg": ra, "dec_deg": dec,
                        "mag_v": mag, "mag_err": 0.02, "source": "test_cat"})
        positions.append((x, y))
    return data, wcs, catalog, positions


def _extract(data, wcs, catalog, zero_point=None, zp_scatter=None, cfg=None):
    from astropy.stats import sigma_clipped_stats
    _, bkg_med, bkg_std = sigma_clipped_stats(data, sigma=3.0)
    fwhm_px = 3.5
    return P._run_survey_extraction(
        data, wcs, bkg_med, bkg_std, fwhm_px,
        ap_r=3.0 * 2.5, ann_in=fwhm_px * 4.0, ann_out=fwhm_px * 6.0,
        read_noise=5.0, gain=1.0,
        catalog_stars=catalog, phot_cfg=cfg or {},
        zero_point=zero_point, zp_scatter=zp_scatter)


class SurveyExtractionTest(unittest.TestCase):
    def test_recovers_injected_magnitudes_with_given_zp(self):
        data, wcs, catalog, _ = _make_field(zp=22.0)
        sources, zp, _ = _extract(data, wcs, catalog,
                                  zero_point=22.0, zp_scatter=0.03)
        matched = {s["key"]: s for s in sources if s["matched"]}
        self.assertGreaterEqual(len(matched), 8)
        for key, s in matched.items():
            self.assertAlmostEqual(s["mag"], s["cat_mag"], delta=0.15,
                                   msg=f"{key}: {s['mag']} vs {s['cat_mag']}")

    def test_self_calibration_recovers_zero_point(self):
        data, wcs, catalog, _ = _make_field(zp=22.0)
        sources, zp, scatter = _extract(data, wcs, catalog)
        self.assertIsNotNone(zp)
        self.assertAlmostEqual(zp, 22.0, delta=0.1)
        self.assertLess(scatter, 0.1)
        self.assertGreaterEqual(len(sources), 8)

    def test_self_calibration_needs_five_matches(self):
        data, wcs, catalog, _ = _make_field(star_mags=[11.0, 12.0, 13.0])
        sources, zp, _ = _extract(data, wcs, catalog[:3])
        self.assertEqual(sources, [])
        self.assertIsNone(zp)

    def test_brightened_star_measured_at_true_brightness(self):
        data, wcs, catalog, _ = _make_field(brightened_index=4, delta_mag=2.0)
        sources, _, _ = _extract(data, wcs, catalog,
                                 zero_point=22.0, zp_scatter=0.03)
        target = [s for s in sources if s["key"] == "CAT-004"]
        self.assertEqual(len(target), 1)
        dev = target[0]["cat_mag"] - target[0]["mag"]   # positive = brighter
        self.assertGreater(dev, 1.5, "brightening must survive extraction")

    def test_unmatched_source_gets_positional_key(self):
        data, wcs, catalog, _ = _make_field(zp=22.0)
        # A bright interloper the catalog has never heard of.
        _inject_star(data, 400.0, 60.0, 10 ** (-0.4 * (10.0 - 22.0)))
        sources, _, _ = _extract(data, wcs, catalog,
                                 zero_point=22.0, zp_scatter=0.03)
        unmatched = [s for s in sources if not s["matched"]]
        self.assertGreaterEqual(len(unmatched), 1)
        self.assertTrue(all(s["key"].startswith("p") for s in unmatched))
        self.assertTrue(all(s["cat_mag"] is None for s in unmatched))

    def test_max_sources_cap_keeps_brightest(self):
        data, wcs, catalog, _ = _make_field(zp=22.0)
        sources, _, _ = _extract(data, wcs, catalog,
                                 zero_point=22.0, zp_scatter=0.03,
                                 cfg={"survey_max_sources": 3})
        self.assertLessEqual(len(sources), 3)


class SourceKeyTest(unittest.TestCase):
    def test_catalog_id_wins(self):
        cs = {"auid": "000-BBC-123", "ra_deg": 10.0, "dec_deg": 20.0}
        self.assertEqual(P._survey_source_key(cs, 10.001, 20.001),
                         "000-BBC-123")

    def test_catalog_position_when_no_id(self):
        cs = {"auid": "", "ra_deg": 10.12345, "dec_deg": -20.98765}
        key = P._survey_source_key(cs, 10.2, -20.9)
        self.assertEqual(key, f"c{cs['ra_deg']:.4f}{cs['dec_deg']:+.4f}")
        # Stable across nodes: derived from catalog coords, not measured ones.
        self.assertEqual(key, P._survey_source_key(cs, 10.1, -21.0))

    def test_positional_key_when_unmatched(self):
        self.assertEqual(P._survey_source_key(None, 187.65432, 5.4321),
                         "p187.654+5.432")


class CatalogCacheTest(unittest.TestCase):
    def test_cache_roundtrip_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig_dir = P._CATALOG_CACHE_DIR
            P._CATALOG_CACHE_DIR = tmp
            calls = {"n": 0}

            def fake_atlas(ra, dec, r, m, n_max=600):
                calls["n"] += 1
                return [{"auid": "", "ra_deg": ra, "dec_deg": dec,
                         "mag_v": 12.0, "mag_err": 0.05,
                         "source": "atlas_refcat2"}] * 200

            orig_atlas = P._get_comparison_stars_atlas
            P._get_comparison_stars_atlas = fake_atlas
            try:
                first = P._load_survey_catalog(180.0, 45.0, 0.7, 17.0)
                self.assertEqual(calls["n"], 1)
                second = P._load_survey_catalog(180.0, 45.0, 0.7, 17.0)
                self.assertEqual(calls["n"], 1, "second load must hit cache")
                self.assertEqual(len(first), len(second))
                # Expired cache refetches.
                cache_file = os.path.join(tmp, os.listdir(tmp)[0])
                blob = json.loads(open(cache_file).read())
                blob["fetched_at"] = 0
                open(cache_file, "w").write(json.dumps(blob))
                P._load_survey_catalog(180.0, 45.0, 0.7, 17.0)
                self.assertEqual(calls["n"], 2)
            finally:
                P._CATALOG_CACHE_DIR = orig_dir
                P._get_comparison_stars_atlas = orig_atlas


@unittest.skipUnless(os.path.exists(ZCAM_FRAME),
                     "real Seestar frame not present")
class RealFrameSmokeTest(unittest.TestCase):
    """The survey extractor against real solved Seestar pixels: build a
    catalog from the frame's own bright detections (offset by a known ZP),
    then check self-calibration recovers that ZP. No network, no solver."""

    def test_zcam_frame_end_to_end(self):
        from astropy.stats import sigma_clipped_stats
        from photutils.detection import DAOStarFinder

        with fits.open(ZCAM_FRAME, memmap=False,
                       ignore_missing_simple=True) as hdul:
            data = np.array(hdul[0].data, dtype=np.float32)
            if data.ndim == 3:
                data = data.mean(axis=int(np.argmin(data.shape)))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                wcs = WCS(hdul[0].header, naxis=2)

        _, bkg_med, bkg_std = sigma_clipped_stats(data, sigma=3.0)
        found = DAOStarFinder(fwhm=4.0, threshold=8.0 * bkg_std,
                              exclude_border=True)(data - bkg_med)
        self.assertIsNotNone(found, "no stars detected in real frame")
        found.sort("peak", reverse=True)
        found = found[:30]

        true_zp = 21.5
        catalog = []
        for i, row in enumerate(found):
            x = float(row["xcentroid"]) if "xcentroid" in found.colnames \
                else float(row["x_centroid"])
            y = float(row["ycentroid"]) if "ycentroid" in found.colnames \
                else float(row["y_centroid"])
            ra, dec = (float(v) for v in wcs.pixel_to_world_values(x, y))
            flux = max(float(row["flux"]), 1e-3)
            catalog.append({
                "auid": f"Z-{i:03d}", "ra_deg": ra, "dec_deg": dec,
                "mag_v": -2.5 * math.log10(flux) + true_zp,
                "mag_err": 0.03, "source": "frame_derived"})

        sources, zp, scatter = P._run_survey_extraction(
            data, wcs, bkg_med, bkg_std, 4.0,
            ap_r=8.0, ann_in=14.0, ann_out=22.0,
            read_noise=5.0, gain=1.0,
            catalog_stars=catalog, phot_cfg={"survey_snr_min": 5.0})

        self.assertIsNotNone(zp)
        self.assertGreaterEqual(len(sources), 10)
        n_matched = sum(1 for s in sources if s["matched"])
        self.assertGreaterEqual(n_matched, 10)


if __name__ == "__main__":
    unittest.main()
