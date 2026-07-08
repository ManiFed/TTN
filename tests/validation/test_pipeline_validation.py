#!/usr/bin/env python3
"""
End-to-end photometry validation against synthetic scenes with known truth.

Every test is offline and deterministic (seeded RNG, frozen file catalogs,
WCS pre-written so no plate solver runs).  The contract under test:

    For each failure mode, the pipeline either recovers the true magnitude
    within its own reported uncertainty, or rejects/flags the frame with a
    machine-readable reason.

Run:  pytest tests/validation -q
"""

import warnings

import numpy as np
import pytest

from src.photometry import run_pipeline_ex
from tests.validation.synthimg import make_scene

warnings.filterwarnings("ignore")

# Truth-recovery tolerance for a single frame: 3× the pipeline's own reported
# uncertainty, floored at 0.02 mag (rounding + centroid quantisation).
def _assert_recovered(res, scene, k_sigma=3.0):
    assert res is not None
    dmag = res["magnitude"] - scene.target_mag
    tol = max(k_sigma * res["uncertainty"], 0.02)
    assert abs(dmag) < tol, (
        f"residual {dmag:+.4f} exceeds {tol:.4f} "
        f"(reported unc {res['uncertainty']:.4f})"
    )


# ── Baseline accuracy ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_baseline_recovery(tmp_path, seed):
    """A clean, well-sampled frame recovers the truth and is flagged good."""
    scene = make_scene(str(tmp_path), seed=seed)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    _assert_recovered(res, scene)
    assert res["quality_flag"] == "good"
    assert res["quality_reasons"] == []
    assert res["snr"] > 20
    assert res["comparison_stars"] >= 5
    # Zero point recovered close to truth
    assert abs(res["zero_point"] - scene.zero_point) < 0.1
    # Provenance is complete and auditable
    prov = res["provenance"]
    assert prov["wcs_source"] == "header"
    assert prov["time"]["time_ref"] == "mid_exposure"
    assert prov["time"]["exptime_s"] == scene.exptime_s
    assert len(prov["comparison_stars"]) == prov["n_comp_in_field"]
    used = [c for c in prov["comparison_stars"] if c["used"]]
    assert len(used) == res["comparison_stars"]
    for c in used:
        assert c["source"] == "synthetic_truth"
        assert c["cat_mag"] is not None and c["reject_reason"] is None


def test_uncertainty_calibration(tmp_path):
    """
    Across many noise realisations the reported uncertainty must not
    *underestimate* the true error: RMS of (residual / reported_unc) ≤ ~1.2.
    (Values well below 1 mean the estimate is conservative — acceptable,
    documented in the validation report.)
    """
    z_scores, residuals = [], []
    for seed in range(10, 22):
        scene = make_scene(str(tmp_path), name=f"cal{seed}", seed=seed)
        res, rej = run_pipeline_ex(scene.fits_path, scene.config())
        assert rej is None, f"seed {seed} unexpectedly rejected: {rej}"
        d = res["magnitude"] - scene.target_mag
        residuals.append(d)
        z_scores.append(d / res["uncertainty"])
    z = np.array(z_scores)
    assert np.sqrt(np.mean(z ** 2)) <= 1.2, f"uncertainties underestimated: z={z.round(2)}"
    assert np.max(np.abs(z)) < 4.0
    # Ensemble bias across realisations stays below 0.03 mag
    assert abs(np.mean(residuals)) < 0.03


def test_faint_target_not_flagged_good(tmp_path):
    """A low-SNR target must not be reported as 'good'."""
    scene = make_scene(str(tmp_path), seed=5, target_mag=14.3)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    if res is None:
        assert rej["reason_code"] in ("nonpositive_target_flux",)
        return
    assert res["quality_flag"] != "good"
    assert any(r["check"] in ("snr", "uncertainty") for r in res["quality_reasons"])
    _assert_recovered(res, scene, k_sigma=4.0)


# ── Astrometry stress ──────────────────────────────────────────────────────────

def test_wcs_offset_recovered_by_centroiding(tmp_path):
    """A Seestar-like pointing error (≈10 px) is absorbed by centroid refinement."""
    scene = make_scene(str(tmp_path), seed=6, wcs_offset_px=(6.0, -8.0))
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    _assert_recovered(res, scene)
    assert res["quality_flag"] in ("good", "acceptable")


def test_gross_wcs_error_rejected_not_mismeasured(tmp_path):
    """
    A WCS wrong by ~60 px (beyond the centroid search box) must produce a
    rejection — never a quietly wrong magnitude of blank sky.
    """
    scene = make_scene(str(tmp_path), seed=7, wcs_offset_px=(60.0, 45.0))
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    if res is not None:
        # If anything was measured, the ensemble must have collapsed enough
        # to withhold the 'good' flag.
        assert res["quality_flag"] != "good"
        return
    assert rej["reason_code"] in ("nonpositive_target_flux", "no_zero_point")
    assert rej["stage"] in ("photometry", "zero_point")


def test_edge_target_rejected(tmp_path):
    scene = make_scene(str(tmp_path), seed=8, target_offset_px=(232.0, 0.0))
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert res is None
    assert rej["reason_code"] == "target_off_frame"
    assert rej["stage"] == "field"
    assert rej["detail"]["margin"] == 20


# ── Comparison-star pathologies ───────────────────────────────────────────────

def test_bad_comparison_star_sigma_clipped(tmp_path):
    """One comp star 0.8 mag wrong in the catalog is clipped from the ensemble
    and leaves the target magnitude unbiased."""
    scene = make_scene(str(tmp_path), seed=9,
                       comp_mag_offsets={"SYN-002": +0.8})
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    _assert_recovered(res, scene)
    audit = {c["auid"]: c for c in res["provenance"]["comparison_stars"]}
    assert audit["SYN-002"]["used"] is False
    assert audit["SYN-002"]["reject_reason"] == "zp_sigma_clipped"


def test_coherent_catalog_bias_is_not_detectable(tmp_path):
    """
    KNOWN LIMITATION (documented in the validation report): if *every*
    comparison star's catalog magnitude shares a coherent bias, differential
    photometry transfers that bias to the target.  This test pins the
    behaviour so the limitation stays visible.
    """
    bias = +0.30
    offsets = {f"SYN-{i:03d}": bias for i in range(8)}
    scene = make_scene(str(tmp_path), seed=10, comp_mag_offsets=offsets)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    dmag = res["magnitude"] - scene.target_mag
    assert abs(dmag - bias) < 0.1, "coherent bias should pass straight through"


def test_sparse_field_rejected(tmp_path):
    scene = make_scene(str(tmp_path), seed=11, n_comp=1)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert res is None
    assert rej["reason_code"] == "too_few_comparison_stars"
    assert rej["detail"]["n_in_field"] == 1


def test_two_comp_stars_capped_at_acceptable(tmp_path):
    scene = make_scene(str(tmp_path), seed=12, comp_mags=[11.0, 11.8])
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    assert res["comparison_stars"] == 2
    assert res["quality_flag"] != "good"
    assert any(r["check"] == "comparison_stars" for r in res["quality_reasons"])


def test_empty_catalog_rejected(tmp_path):
    scene = make_scene(str(tmp_path), seed=13)
    cfg = scene.config(comparison_star_file="/nonexistent/catalog.json")
    res, rej = run_pipeline_ex(scene.fits_path, cfg)
    assert res is None
    assert rej["reason_code"] == "no_comparison_stars"
    assert rej["detail"]["catalogs"] == ["file"]


# ── Saturation ─────────────────────────────────────────────────────────────────

def test_saturated_target_flagged_poor(tmp_path):
    """A clipped target PSF biases the magnitude faint — the saturation gate
    must force quality=poor with a machine-readable reason."""
    scene = make_scene(str(tmp_path), seed=14, target_mag=8.0, clip_adu=3000)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config(saturation_adu=3000))
    assert rej is None
    assert res["quality_flag"] == "poor"
    assert any(r["check"] == "target_saturated" and r["outcome"] == "fail"
               for r in res["quality_reasons"])
    assert res["provenance"]["target_saturated"] is True
    # The measured magnitude really is biased faint — the gate is load-bearing.
    assert res["magnitude"] > scene.target_mag + 0.1


def test_saturated_comp_star_excluded(tmp_path):
    scene = make_scene(str(tmp_path), seed=15,
                       comp_mags=[10.2, 11.4, 11.8, 12.0, 12.3, 12.6],
                       clip_adu=800)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config(saturation_adu=800))
    assert rej is None
    audit = {c["auid"]: c for c in res["provenance"]["comparison_stars"]}
    # The mag-10.2 comp peaks ≈1150 ADU and must be excluded as saturated
    assert audit["SYN-000"]["reject_reason"] == "saturated"
    assert audit["SYN-000"]["used"] is False
    _assert_recovered(res, scene)


# ── Observing-condition stress ─────────────────────────────────────────────────

def test_poor_seeing_recovered(tmp_path):
    scene = make_scene(str(tmp_path), seed=16, fwhm_px=8.0, target_mag=10.8)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    assert abs(res["fwhm"] - 8.0) < 1.6, "FWHM estimate should track poor seeing"
    _assert_recovered(res, scene)


def test_bright_sky_not_flagged_good(tmp_path):
    """Moon-bright background (33× nominal sky) crushes SNR — must not be good."""
    scene = make_scene(str(tmp_path), seed=17, sky_adu=5000.0)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    if res is None:
        assert rej["reason_code"] in ("nonpositive_target_flux", "no_zero_point")
        return
    assert res["quality_flag"] != "good"
    _assert_recovered(res, scene, k_sigma=4.0)


# ── Frame handling ─────────────────────────────────────────────────────────────

def test_dark_frame_rejected(tmp_path):
    scene = make_scene(str(tmp_path), seed=18, imagetyp="DARK")
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert res is None
    assert rej["reason_code"] == "non_light_frame"
    assert rej["stage"] == "frame_type"


def test_osc_cube_collapsed_to_luminance(tmp_path):
    scene = make_scene(str(tmp_path), seed=19, cube3d=True)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    _assert_recovered(res, scene)


def test_missing_dateobs_recorded_in_provenance(tmp_path):
    scene = make_scene(str(tmp_path), seed=20, date_obs=None)
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert rej is None
    assert res["provenance"]["time"]["time_ref"] == "system_clock"
    assert res["provenance"]["time"]["date_obs"] is None


def test_rejections_are_machine_readable(tmp_path):
    """Every rejection carries the full audit contract."""
    scene = make_scene(str(tmp_path), seed=21, imagetyp="FLAT")
    res, rej = run_pipeline_ex(scene.fits_path, scene.config())
    assert res is None
    for key in ("rejected", "stage", "reason_code", "message", "fits_file", "detail"):
        assert key in rej
    assert rej["rejected"] is True
    import json
    json.dumps(rej)  # must serialise for incident/audit storage
