#!/usr/bin/env python3
"""
Component-level validation of the photometry pipeline's numerical building
blocks: the aperture noise model, FWHM estimation, BJD timing, and airmass.

All offline and deterministic.  Run:  pytest tests/validation -q
"""

import math
import warnings

import numpy as np
import pytest
from astropy.io import fits

import src.photometry as P
from tests.validation.synthimg import make_scene, _render_star

warnings.filterwarnings("ignore")


# ── Aperture photometry noise model ────────────────────────────────────────────

def _single_star_frame(flux=50000.0, fwhm=4.0, sky=200.0, shape=(120, 120),
                       seed=None, gain=1.0, read_noise=5.0):
    img = np.full(shape, sky, dtype=np.float64)
    _render_star(img, 60.0, 60.0, flux, fwhm / 2.355)
    if seed is not None:
        rng = np.random.default_rng(seed)
        img = rng.poisson(np.maximum(img * gain, 0)).astype(float) / gain
        img += rng.normal(0, read_noise / gain, size=shape)
    return img.astype(np.float32)


def test_flux_recovery_noise_free():
    """With no noise, the aperture sum recovers ≥99.8% of a Gaussian star's
    flux at r = 2.5×FWHM (aperture-loss check)."""
    flux = 50000.0
    img = _single_star_frame(flux=flux)
    fluxes, errs = P._aperture_photometry(
        img, [(60.0, 60.0)], ap_radius=10.0, ann_inner=16.0, ann_outer=24.0)
    assert fluxes is not None
    assert abs(fluxes[0] - flux) / flux < 0.002


def test_error_model_matches_ccd_equation():
    """The reported flux error must match the analytic CCD equation."""
    flux, sky, gain, rn, r = 50000.0, 200.0, 0.8, 5.0, 10.0
    img = _single_star_frame(flux=flux, sky=sky)
    fluxes, errs = P._aperture_photometry(
        img, [(60.0, 60.0)], ap_radius=r, ann_inner=16.0, ann_outer=24.0,
        read_noise=rn, gain=gain)
    area = math.pi * r * r
    expected = math.sqrt((fluxes[0] + area * sky) / gain + area * (rn / gain) ** 2)
    assert abs(errs[0] - expected) / expected < 0.05


def test_error_grows_with_sky_background():
    imgs = [_single_star_frame(sky=s) for s in (50.0, 2000.0)]
    errs = []
    for img in imgs:
        f, e = P._aperture_photometry(img, [(60.0, 60.0)], 10.0, 16.0, 24.0)
        errs.append(e[0])
    assert errs[1] > 3 * errs[0]


def test_empirical_flux_scatter_matches_reported_error():
    """Monte-Carlo: the scatter of measured fluxes over noise realisations
    must agree with the reported error within 25%."""
    flux = 20000.0
    measured, reported = [], []
    for seed in range(40):
        img = _single_star_frame(flux=flux, seed=seed)
        f, e = P._aperture_photometry(img, [(60.0, 60.0)], 8.0, 16.0, 24.0)
        measured.append(f[0])
        reported.append(e[0])
    empirical = float(np.std(measured))
    assert abs(np.mean(measured) - flux) / flux < 0.01
    assert 0.75 < empirical / float(np.mean(reported)) < 1.25


# ── FWHM estimation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("true_fwhm", [2.5, 4.0, 6.0, 8.0])
def test_fwhm_estimate_tracks_truth(tmp_path, true_fwhm):
    scene = make_scene(str(tmp_path), name=f"fwhm{true_fwhm}", seed=30,
                       fwhm_px=true_fwhm)
    data = fits.getdata(scene.fits_path).astype(float)
    est = P._estimate_fwhm(data, fallback_px=4.0)
    assert abs(est - true_fwhm) / true_fwhm < 0.15, (
        f"FWHM {est:.2f} vs truth {true_fwhm}"
    )


def test_fwhm_fallback_on_empty_frame():
    rng = np.random.default_rng(0)
    noise = rng.normal(100.0, 5.0, size=(200, 200)).astype(np.float32)
    assert P._estimate_fwhm(noise, fallback_px=3.7) == 3.7


# ── BJD timing ─────────────────────────────────────────────────────────────────

_SITE = {"observatory": {"latitude": 40.0, "longitude": -105.0, "elevation": 1600},
         "safety": {"observer": {"latitude": 40.0, "longitude": -105.0}}}


def test_bjd_midpoint_shift_equals_half_exposure():
    hdr_short = {"DATE-OBS": "2026-01-15T06:30:00", "EXPTIME": 0.001}
    hdr_long  = {"DATE-OBS": "2026-01-15T06:30:00", "EXPTIME": 600.0}
    b0, p0 = P._compute_bjd_ex(hdr_short, 150.0, 20.0, _SITE)
    b1, p1 = P._compute_bjd_ex(hdr_long, 150.0, 20.0, _SITE)
    shift_s = (b1 - b0) * 86400.0
    assert abs(shift_s - 300.0) < 1.0
    assert p1["time_ref"] == "mid_exposure"
    assert p1["exptime_s"] == 600.0


def test_bjd_midpoint_correction_can_be_disabled():
    hdr = {"DATE-OBS": "2026-01-15T06:30:00", "EXPTIME": 600.0}
    cfg = dict(_SITE, photometry={"bjd_midpoint_correction": False})
    bjd, prov = P._compute_bjd_ex(hdr, 150.0, 20.0, cfg)
    assert prov["time_ref"] == "exposure_start"
    bjd_on, _ = P._compute_bjd_ex(hdr, 150.0, 20.0, _SITE)
    assert abs((bjd_on - bjd) * 86400.0 - 300.0) < 1.0


def test_bjd_includes_barycentric_roemer_term():
    """BJD − JD(UTC) must equal TDB−UTC plus a Rømer term ≤ ±8.5 min, and match
    an independent astropy computation to sub-second precision."""
    from astropy.time import Time
    from astropy.coordinates import SkyCoord, EarthLocation
    import astropy.units as u

    hdr = {"DATE-OBS": "2026-03-21T04:00:00", "EXPTIME": 0.001}
    ra, dec = 200.0, -10.0
    bjd, prov = P._compute_bjd_ex(hdr, ra, dec, _SITE)
    assert prov["barycentric"] is True

    t = Time("2026-03-21T04:00:00", scale="utc")
    loc = EarthLocation(lat=40.0 * u.deg, lon=-105.0 * u.deg, height=1600 * u.m)
    ltt = t.light_travel_time(SkyCoord(ra=ra * u.deg, dec=dec * u.deg),
                              kind="barycentric", location=loc)
    expected = (t.tdb + ltt).jd
    assert abs(bjd - expected) * 86400.0 < 1.0
    assert abs(ltt.to_value("min")) < 8.5


@pytest.mark.parametrize("date_obs", [
    "2026-01-15T06:30:00",
    "2026-01-15T06:30:00.123",
    "2026-01-15T06:30:00Z",
    "2026-01-15 06:30:00",
])
def test_bjd_dateobs_format_tolerance(date_obs):
    hdr = {"DATE-OBS": date_obs, "EXPTIME": 10.0}
    bjd, prov = P._compute_bjd_ex(hdr, 150.0, 20.0, _SITE)
    assert 2461055.0 < bjd < 2461056.0   # JD of 2026-01-15
    assert prov["time_ref"] == "mid_exposure"


# ── Airmass ────────────────────────────────────────────────────────────────────

def test_airmass_header_priority():
    assert P._compute_airmass({"AIRMASS": 1.234}, _SITE) == pytest.approx(1.234)


def test_airmass_computed_from_geometry_is_physical():
    hdr = {"RA": 150.0, "DEC": 20.0, "DATE-OBS": "2026-01-15T06:30:00"}
    am = P._compute_airmass(hdr, _SITE)
    assert 1.0 <= am <= 5.8


def test_airmass_fallback_without_observer():
    hdr = {"RA": 150.0, "DEC": 20.0, "DATE-OBS": "2026-01-15T06:30:00"}
    assert P._compute_airmass(hdr, {}) == 1.5


# ── Offline file catalog backend ───────────────────────────────────────────────

def test_file_catalog_radius_and_mag_filters(tmp_path):
    import json
    path = tmp_path / "cat.json"
    entries = [
        {"ra_deg": 150.0, "dec_deg": 20.0, "mag_v": 12.0, "auid": "A"},
        {"ra_deg": 150.0, "dec_deg": 20.1, "mag_v": 16.5, "auid": "B"},  # too faint
        {"ra_deg": 155.0, "dec_deg": 25.0, "mag_v": 12.0, "auid": "C"},  # too far
        {"ra_deg": "bad", "dec_deg": 20.0, "mag_v": 12.0, "auid": "D"},  # malformed
    ]
    path.write_text(json.dumps(entries))
    out = P._get_comparison_stars_file(str(path), 150.0, 20.0, 0.5, 15.0)
    assert [s["auid"] for s in out] == ["A"]
    assert out[0]["source"] == "file"
    assert out[0]["mag_err"] == 0.05


def test_file_catalog_missing_file_returns_empty():
    assert P._get_comparison_stars_file("/nonexistent.json", 150.0, 20.0, 0.5, 15.0) == []
