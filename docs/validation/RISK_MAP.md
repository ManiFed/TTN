# Photometry Technical Risk Map

Audit of every identified source of photometric error in The Telescope Net
pipeline (`src/photometry.py` → `cloud/data_pipeline.py` → AAVSO), ranked by
likely scientific impact. Each risk lists its mechanism, current status, and
the test or gate that covers it. "Silent" means the error would not have been
visible in the reported uncertainty or quality flag.

Status legend: **GATED** = hard quality gate + regression test ·
**TESTED** = behaviour pinned by offline tests · **BOUNDED** = effect visible
in reported scatter/uncertainty · **OPEN** = needs data or future work.

---

## Tier 1 — could silently corrupt science

### 1. Untransformed colour response (CV vs Johnson V) — OPEN (disclosed)
The Seestar's OSC sensor is collapsed to a luminance image
(`photometry.py`, colour-cube average) and calibrated against Johnson-V
comparison magnitudes. The effective bandpass is much wider than V, so the
zero point carries a colour term: for a target much redder or bluer than the
comparison ensemble the systematic error can reach **0.1–0.3 mag** and does
not appear in the reported uncertainty. Submissions are correctly declared
`FILT=CV`, `TRANS=NO`, so AAVSO treats them accordingly — the risk is bounded
by disclosure, not by measurement.
*Next step:* estimate a per-target colour correction from Gaia BP−RP and
either apply-with-provenance or add the term to the uncertainty. Requires
real two-colour reference fields from beta nodes (see FIXTURE_MANIFEST).

### 2. Coherent comparison-catalog bias — TESTED (limitation pinned)
If every comparison star shares a bias (catalog zero-point error, Gaia G→V or
ATLAS g,r→V transformation offset in an unusual stellar population),
differential photometry transfers it 1:1 to the target. Cross-node validation
cannot catch it — all nodes use the same catalogs.
`test_coherent_catalog_bias_is_not_detectable` pins this behaviour so the
limitation stays visible. Mitigations in place: AAVSO sequences are queried
first and win de-duplication; each measurement's provenance records every
star's catalog of origin (`provenance.comparison_stars[].source`).

### 3. Saturation / nonlinearity — GATED (new in v1.1.0)
Previously ungated: nothing checked actual pixel values, only the
catalog-magnitude proxy `mag_min`, so a clipped target PSF produced a
quietly-faint magnitude. Now the peak pixel around the target and every
comparison star is checked against `photometry.saturation_adu`
(default 60 000): a saturated target forces `quality_flag=poor` with a
machine-readable reason; saturated comparison stars are excluded from the
ensemble with `reject_reason="saturated"`.
Tests: `test_saturated_target_flagged_poor` (verifies the bias is real and
caught), `test_saturated_comp_star_excluded`.
*Residual risk:* sensor nonlinearity below the clip level is uncharacterised
for the IMX462 — needs a beta-node exposure ladder.

### 4. Time-stamp semantics and mid-exposure BJD — GATED (new in v1.1.0)
BJD was computed from `DATE-OBS` with no exposure-midpoint correction: for a
5-minute stack that is a **+2.5 min** systematic on every timestamp — fatal
for exoplanet transit timing (a headline science program). v1.1.0 shifts to
mid-exposure using `EXPTIME`/`EXPOSURE`/`LIVETIME` (config-gated via
`photometry.bjd_midpoint_correction`), records
`provenance.time.{time_ref,exptime_s}`, and the dashboard's own FITS exporter
now writes `DATE-OBS` as the exposure *start* per the FITS convention (it
previously wrote the save time ≈ exposure end).
Tests: `test_bjd_midpoint_shift_equals_half_exposure`,
`test_bjd_includes_barycentric_roemer_term` (Rømer term verified ≤ ±8.5 min
against an independent astropy computation).
*Residual risk:* the Seestar's own stacked-FITS header semantics
(is `DATE-OBS` stack start? is `EXPTIME` per-sub or accumulated?) are
unconfirmed — item 1 in the fixture manifest.

---

## Tier 2 — degrades precision or reliability (visible in scatter)

### 5. FWHM overestimation under noise — FIXED + TESTED (v1.1.0)
The second-moment FWHM estimator is exact on noise-free data but rectified
sky noise in stamp wings inflated it ~2× at Seestar-like S/N (measured 8.5 px
for a true 4.0 px PSF). Apertures scale with FWHM, so the aperture radius
doubled and SNR halved. Now a Gaussian PSF fit (`photutils.psf.fit_fwhm`) is
preferred, recovering 3.9/4.0 px; the moment method remains as fallback for
older photutils. Test: `test_fwhm_estimate_tracks_truth` (2.5–8 px, ±15%).

### 6. WCS error, centroid mis-lock, and blending — GATED
Small pointing errors (≈10 px, Seestar-typical) are absorbed by centroid
refinement (`test_wcs_offset_recovered_by_centroiding`). Gross WCS errors
(≥ the centroid search box) produce a rejection, never a quiet measurement of
blank sky (`test_gross_wcs_error_rejected_not_mismeasured`) — the 5σ local
peak guard prevents locking onto noise.
*Residual risk:* a blended neighbour of comparable brightness inside the
search box is now detected from image morphology. A blended target is forced
to `quality=poor`; blended comparison stars are excluded with
`reject_reason="bright_neighbour"`. C5 real crowded-field fixtures remain
necessary to tune the flux/separation thresholds.

### 7. Pointing-WCS fallback — BOUNDED
Without a plate solver the pipeline constructs a TAN WCS from reported
pointing (tens of arcsec error survives). Provenance now records
`wcs_source="pointing"` vs `"solved_*"`/`"header"` so downstream review can
distrust these frames. The quality gate does not yet demote them — deliberate
for now (centroid refinement usually recovers), revisit with beta statistics.

### 8. Zero-point ensemble statistics — BOUNDED (conservative)
`zp_scatter` is the *unweighted, full* std-dev of the clipped ZP ensemble and
is added in quadrature to the target's Poisson error. The proper error of the
weighted-mean ZP is ≈ scatter/√N, so reported uncertainties **overestimate**:
measured calibration z-RMS ≈ 0.6 (see VALIDATION_REPORT). Conservative
direction — safe for AAVSO, but under-reports our true precision. The 2.5σ
sigma-clip on ≥4 stars can also clip legitimate stars in small ensembles.
New gates: `zp_scatter > 0.15` blocks "good", `> 0.30` forces "poor"
(`evaluate_quality`, tested).

### 9. No flat-fielding / vignetting gradients — OPEN
No dark/bias/flat calibration is applied (the Seestar does some internally;
uncharacterised). Vignetting makes the effective zero point
position-dependent; field-edge comparison stars then inflate `zp_scatter`
(visible, not silent) and can bias the ZP if comps cluster on one side.
Needs: beta-node flat/dithered-field data to measure the illumination profile.

### 10. Alt-az field rotation and stacking artefacts — OPEN
Field rotation gives the stack's outer regions fewer effective sub-exposures
(higher noise, possible smearing) than the per-pixel noise model assumes;
edge comps get underestimated errors. The 20 px edge margin does not cover
this. Needs real stacks with sub-counts to characterise.

---

## Tier 3 — bounded or operational

11. **Airmass provenance — GATED** — header and geometry-derived values are
identified in provenance. Missing geometry produces `airmass=null`, blocks a
`good` quality flag, and exports AAVSO `AMASS=na`; no synthetic fallback is
reported.
12. **Gaia G→V transformation** — Evans et al. (2018) coefficients verified;
    out-of-range colours (BP−RP ∉ [−0.5, 2.75]) fall back to G with
    `mag_err=0.20`, so inverse-variance weighting suppresses them.
13. **Cloud ingest bounds** (`Measurement.is_valid`) are loose
    (uncertainty < 5.0) but only bound storage, not submission — submission
    requires `good/acceptable` plus cross-validation (tested).
14. **WebObs response parsing** — success only recognised from the explicit
    "N observation(s)" token; Auth0/WAF 200-challenge pages are treated as
    errors, never as silent acceptance (tested).
15. **Cross-validation window logic** — outlier = deviation > 0.3 mag *and*
    > 3σ from the co-temporal median (tested, including the
    within-uncertainty case). Single-node epochs get a 6 h hold-back, then
    submit unconfirmed — a residual trust assumption on single nodes.
16. **OSC cube collapse** — colour axis chosen as the shortest dimension;
    correct for Seestar cubes, could misfire on exotic strip images
    (< 3 px on a side; not observed in practice). Plane-average luminance
    tested end-to-end (`test_osc_cube_collapsed_to_luminance`).

---

## What beta nodes must collect next (priority order)

See `FIXTURE_MANIFEST.md` for the full specification.

1. Seestar stacked-FITS header semantics (DATE-OBS/EXPTIME ladder).
2. Exposure ladder on a standard field → nonlinearity + true saturation level.
3. AAVSO standard-field stacks (M67 / NGC 7790 dippers region) → external
   accuracy anchor against curated sequences, colour-term measurement.
4. Crowded-field stacks (|b| < 10°) → centroid mis-lock rate.
5. Twilight/dithered flats → vignetting profile.
6. Same-target same-night stacks from ≥2 nodes → real cross-node scatter.
