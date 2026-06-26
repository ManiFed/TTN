#!/usr/bin/env python3
"""
Local photometry pipeline — Layer 5 in the The Telescope Net architecture.

Given a FITS file from the Seestar, produces a calibrated differential
photometry measurement suitable for AAVSO submission.

Public API
----------
    from photometry import run_pipeline
    result = run_pipeline(fits_path, config)   # returns dict or None

Output dict (matches AAVSO Extended File Format fields)
-------------------------------------------------------
    {
        "target_name":      "SN2025abc",
        "bjd":              2460500.1234,
        "magnitude":        13.42,
        "uncertainty":      0.08,
        "filter":           "CV",
        "airmass":          1.34,
        "fwhm":             3.2,         # pixels
        "snr":              45.0,
        "comparison_stars": 7,
        "quality_flag":     "good",      # good / acceptable / poor
        "node_id":          "node_042",
        "zero_point":       22.41,
        "zp_scatter":       0.03,
        "fits_file":        "image.fits",
    }
"""

import logging
import math
import os
import subprocess
import tempfile
import time
from typing import Optional

import numpy as np
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
import astropy.units as u
import warnings

warnings.filterwarnings("ignore", category=FITSFixedWarning)

logger = logging.getLogger("photometry")


# ── Public entry point ─────────────────────────────────────────────────────────

def run_pipeline(fits_path: str, config: dict) -> Optional[dict]:
    """
    Run the full photometry pipeline on a single FITS file.

    The target identity (name, RA, Dec) is read from the FITS header first,
    then optionally overridden by config["photometry"]["target"].

    Returns a measurement dict on success, None on unrecoverable failure.
    """
    t0 = time.monotonic()
    # Fill any unset photometry params from the telescope spec catalog (pixel
    # scale, field radius, magnitude limits, read noise, FWHM fallback, …).
    # Idempotent and explicit-config-wins, so a fully-specified config (e.g. the
    # Seestar S50 in config.yaml) is left exactly as-is.
    try:
        from src.telescope_specs import enrich_config_with_telescope
        config = enrich_config_with_telescope(config)
    except Exception as exc:
        logger.debug("Telescope enrichment skipped: %s", exc)
    phot_cfg = config.get("photometry", {})
    node_id = phot_cfg.get("node_id", "node_unknown")
    filter_name = phot_cfg.get("filter_name", "CV")

    # ── Load FITS ──────────────────────────────────────────────────────────────
    try:
        with fits.open(fits_path, memmap=False, ignore_missing_simple=True) as hdul:
            header = dict(hdul[0].header)
            data = np.array(hdul[0].data, dtype=np.float32)
    except Exception as exc:
        logger.error("Cannot open FITS %s: %s", fits_path, exc)
        return None

    if data is None or data.size == 0:
        logger.error("FITS file has no image data: %s", fits_path)
        return None

    # Collapse one-shot-colour cubes to a 2-D luminance image by averaging the
    # colour planes.  Taking a single plane (the old behaviour took plane 0 = the
    # red channel) measures the target in a bandpass far from the Johnson V of the
    # comparison stars, injecting a systematic colour error into the zero point.
    # Averaging the planes matches the luminance image the live stacker builds.
    if data.ndim == 3:
        caxis = int(np.argmin(data.shape))   # colour axis is the shortest one
        data = data.mean(axis=caxis)
    if data.ndim != 2:
        logger.error("Unexpected data shape %s in %s", data.shape, fits_path)
        return None

    # ── Reject non-science frames ──────────────────────────────────────────────
    image_type = str(header.get("IMAGETYP", "LIGHT")).strip().upper()
    if image_type not in ("LIGHT", "LIGHT FRAME", ""):
        logger.info("Skipping non-LIGHT frame (IMAGETYP=%s): %s",
                    image_type, os.path.basename(fits_path))
        return None

    # ── Detector gain ──────────────────────────────────────────────────────────
    # Priority: FITS header EGAIN/CCDGAIN (camera-reported) > config > default
    hdr_gain = header.get("EGAIN") or header.get("CCDGAIN")
    if hdr_gain is not None:
        gain = float(hdr_gain)
        logger.debug("Gain from FITS header: %.3f e⁻/ADU", gain)
    else:
        gain = float(phot_cfg.get("gain", 1.0))

    # ── Extract target identity ────────────────────────────────────────────────
    target_name = str(header.get("OBJECT", "")).strip()
    header_ra   = header.get("RA")    # degrees (FITS standard)
    header_dec  = header.get("DEC")   # degrees

    # Config override (useful for Phase 0 manual testing)
    tgt_cfg = phot_cfg.get("target", {})
    if tgt_cfg.get("name"):
        target_name = str(tgt_cfg["name"])
    ra_deg  = float(tgt_cfg["ra_deg"])  if tgt_cfg.get("ra_deg")  is not None else (float(header_ra)  if header_ra  is not None else None)
    dec_deg = float(tgt_cfg["dec_deg"]) if tgt_cfg.get("dec_deg") is not None else (float(header_dec) if header_dec is not None else None)

    if not target_name:
        logger.warning("No target name in FITS header or config — skipping")
        return None
    if ra_deg is None or dec_deg is None:
        logger.warning("No RA/Dec for target '%s' — skipping", target_name)
        return None

    logger.info("Pipeline start: %s  RA=%.4f°  Dec=%.4f°  file=%s",
                target_name, ra_deg, dec_deg, os.path.basename(fits_path))

    # ── Step 1: Ensure WCS ────────────────────────────────────────────────────
    # An accurate WCS on *this* stack is what makes the comparison-star
    # cross-match (Step 4) land on the right pixels. The Seestar writes an
    # onboard WCS, but its alt-az astrometry can be off by tens of arcsec; with
    # force_plate_solve enabled we re-solve every stack from scratch (preferably
    # with Astrometry.net's blind solver) to eliminate that error entirely.
    solver           = str(phot_cfg.get("solver", "astap")).strip().lower()
    astap_path       = phot_cfg.get("astap_path", "astap")
    solve_field_path = phot_cfg.get("solve_field_path", "solve-field")
    search_radius    = float(phot_cfg.get("astap_search_radius", 10))
    pixel_scale      = phot_cfg.get("pixel_scale")  # arcsec/px, optional scale hint
    force_solve      = bool(phot_cfg.get("force_plate_solve", False))
    if not _ensure_wcs(
        fits_path, ra_deg, dec_deg,
        solver=solver,
        astap_path=astap_path,
        solve_field_path=solve_field_path,
        search_radius=search_radius,
        pixel_scale=pixel_scale,
        force=force_solve,
    ):
        logger.error("Plate solve failed — cannot proceed without WCS")
        return None

    # Reload header after potential ASTAP update
    try:
        with fits.open(fits_path, memmap=False, ignore_missing_simple=True) as hdul:
            header = dict(hdul[0].header)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                wcs = WCS(hdul[0].header, naxis=2)
    except Exception as exc:
        logger.error("Cannot reload WCS from %s: %s", fits_path, exc)
        return None

    # ── Step 2: Confirm target is in the image field ──────────────────────────
    h, w = data.shape
    target_sky = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    try:
        tx, ty = wcs.world_to_pixel(target_sky)
        tx, ty = float(tx), float(ty)
    except Exception as exc:
        logger.error("WCS world_to_pixel failed: %s", exc)
        return None

    margin = 20  # pixels — target must be this far from edges for reliable photometry
    if not (margin <= tx < w - margin and margin <= ty < h - margin):
        logger.warning("Target %s is outside image bounds or too close to edge "
                       "(x=%.1f y=%.1f in %dx%d image)", target_name, tx, ty, w, h)
        return None

    logger.debug("Target pixel: x=%.1f  y=%.1f", tx, ty)

    # ── Step 3: Estimate FWHM ─────────────────────────────────────────────────
    fwhm_fallback = float(phot_cfg.get("fwhm_fallback_px", 4.0))
    fwhm_px = _estimate_fwhm(data, fallback_px=fwhm_fallback)
    logger.info("FWHM estimate: %.2f px", fwhm_px)

    # Aperture geometry
    ap_factor   = float(phot_cfg.get("aperture_factor",  2.5))
    ann_inner_f = float(phot_cfg.get("annulus_inner",    4.0))
    ann_outer_f = float(phot_cfg.get("annulus_outer",    6.0))
    ap_r    = max(3.0, fwhm_px * ap_factor)
    ann_in  = max(ap_r + 1.0, fwhm_px * ann_inner_f)
    ann_out = max(ann_in + 3.0, fwhm_px * ann_outer_f)

    # ── Step 4: Get comparison stars ──────────────────────────────────────────
    field_radius_deg = float(phot_cfg.get("field_radius", 0.5))
    mag_limit        = float(phot_cfg.get("mag_limit", 15.0))
    mag_min          = float(phot_cfg.get("mag_min", 10.0))  # skip bright/saturated stars

    # Catalog chain, queried in order and accumulated until we have enough.
    # AAVSO sequence stars are best (curated V mags) but sparse; APASS and ATLAS
    # reach several magnitudes fainter and stay dense even at high declination
    # (e.g. Dec ≈ +73°, where Tycho-2/AAVSO coverage thins out), so they fill in
    # the 10–20 matches that broadband differential photometry wants.
    catalogs     = phot_cfg.get("comparison_catalogs", ["aavso", "apass", "gaia"])
    target_count = int(phot_cfg.get("comparison_target_count", 8))
    comp_stars = _gather_comparison_stars(
        target_name, ra_deg, dec_deg, field_radius_deg, mag_limit,
        catalogs, target_count,
    )

    if not comp_stars:
        logger.error("No comparison stars found in field")
        return None

    # Filter to stars within the image frame
    comp_in_field = []
    for cs in comp_stars:
        try:
            sky = SkyCoord(ra=cs["ra_deg"] * u.deg, dec=cs["dec_deg"] * u.deg)
            cx, cy = wcs.world_to_pixel(sky)
            cx, cy = float(cx), float(cy)
        except Exception:
            continue
        if margin <= cx < w - margin and margin <= cy < h - margin:
            cs = dict(cs)
            cs["x_px"] = cx
            cs["y_px"] = cy
            if cs.get("mag_v", 99) >= mag_min:
                comp_in_field.append(cs)
            else:
                logger.debug("Skipping comp star V=%.2f (brighter than mag_min=%.1f — likely saturated)",
                             cs.get("mag_v", 0), mag_min)

    logger.info("Comparison stars in field: %d / %d", len(comp_in_field), len(comp_stars))
    if len(comp_in_field) < 2:
        logger.error("Too few comparison stars in image field (%d)", len(comp_in_field))
        return None

    # ── Step 5a: Centroid-refine positions onto actual source peaks ───────────
    # The pointing-WCS can be off by tens of pixels; snap each position to the
    # nearest source centroid before measuring flux, so the aperture is centred.
    from photutils.centroids import centroid_sources
    from astropy.stats import sigma_clipped_stats as _scs

    _, bkg_med, bkg_std = _scs(data, sigma=3.0)
    bkg_sub = data - bkg_med
    search_r = int(max(15, fwhm_px * 3))
    peak_threshold = 5.0 * bkg_std   # a real source must rise this far above sky

    raw_positions = [(tx, ty)] + [(cs["x_px"], cs["y_px"]) for cs in comp_in_field]
    refined = []
    for rx, ry in raw_positions:
        try:
            cx_arr, cy_arr = centroid_sources(
                bkg_sub, rx, ry,
                box_size=2 * search_r + 1,
            )
            cx, cy = float(np.atleast_1d(cx_arr)[0]), float(np.atleast_1d(cy_arr)[0])
            dx, dy = cx - rx, cy - ry
            if abs(dx) > search_r or abs(dy) > search_r:
                raise ValueError("centroid drifted out of search box")
            # Guard against the centroid snapping onto noise or a hot pixel: the
            # local peak around the refined position must be a significant source.
            # If not, keep the WCS position rather than measure a phantom star.
            iy, ix = int(round(cy)), int(round(cx))
            y0, y1 = max(0, iy - 3), min(data.shape[0], iy + 4)
            x0, x1 = max(0, ix - 3), min(data.shape[1], ix + 4)
            local_peak = float(bkg_sub[y0:y1, x0:x1].max()) if (y1 > y0 and x1 > x0) else 0.0
            if local_peak < peak_threshold:
                raise ValueError(f"no significant source at centroid (peak {local_peak:.1f} < {peak_threshold:.1f})")
            refined.append((float(cx), float(cy)))
        except Exception as exc:
            logger.debug("Centroid refinement failed for (%.1f, %.1f): %s", rx, ry, exc)
            refined.append((rx, ry))  # fall back to WCS position

    tx_r, ty_r = refined[0]
    logger.info(
        "Target centroid: WCS (%.1f, %.1f) → refined (%.1f, %.1f) Δ=(%.1f, %.1f)",
        tx, ty, tx_r, ty_r, tx_r - tx, ty_r - ty,
    )
    tx, ty = tx_r, ty_r

    # ── Step 5: Aperture photometry ───────────────────────────────────────────
    positions = refined
    read_noise = float(phot_cfg.get("read_noise", header.get("RDNOISE", 5.0)))
    fluxes, flux_errors = _aperture_photometry(data, positions, ap_r, ann_in, ann_out, read_noise, gain)
    if fluxes is None:
        logger.error("Aperture photometry failed")
        return None

    target_flux      = fluxes[0]
    target_flux_err  = flux_errors[0]
    comp_fluxes      = fluxes[1:]
    comp_flux_errors = flux_errors[1:]

    if target_flux <= 0:
        logger.warning("Target flux non-positive (%.1f) — target may be too faint or saturated",
                       target_flux)
        return None

    # ── Step 6: Differential photometry ──────────────────────────────────────
    def instr_mag(flux: float) -> float:
        return -2.5 * math.log10(max(flux, 1e-10))

    target_instr = instr_mag(target_flux)
    zero_points, zp_weights = [], []

    for i, cs in enumerate(comp_in_field):
        if comp_fluxes[i] <= 0:
            continue
        ref_mag = cs.get("mag_v")
        if ref_mag is None:
            continue
        zp = ref_mag - instr_mag(comp_fluxes[i])
        if comp_flux_errors[i] > 0:
            sigma_instr = 1.0857 * (comp_flux_errors[i] / comp_fluxes[i])
        else:
            sigma_instr = 0.05
        # The per-star ZP uncertainty is the instrumental (Poisson) error in
        # quadrature with the *catalog* uncertainty on the reference magnitude.
        # Without the catalog term a noisy Gaia G→V star (mag_err up to ~0.2)
        # would be weighted the same as a curated AAVSO sequence star (~0.01),
        # biasing the ensemble zero point toward the less reliable catalog.
        cat_err  = float(cs.get("mag_err") or 0.05)
        sigma_zp = math.sqrt(sigma_instr ** 2 + cat_err ** 2)
        weight   = 1.0 / max(sigma_zp ** 2, 1e-6)
        zero_points.append(zp)
        zp_weights.append(weight)

    if not zero_points:
        logger.error("Could not compute zero point — no valid comparison stars with known magnitudes")
        return None

    zp_arr  = np.array(zero_points)
    w_arr   = np.array(zp_weights)

    # Sigma-clip the ZP ensemble to remove outliers (saturated / variable comp stars).
    if len(zp_arr) >= 4:
        from astropy.stats import sigma_clip as _sigma_clip
        masked = _sigma_clip(zp_arr, sigma=2.5, maxiters=5)
        good   = ~masked.mask
        n_clipped = int(np.sum(masked.mask))
        if n_clipped:
            logger.info("ZP sigma-clip: removed %d outlier(s) from %d comp stars", n_clipped, len(zp_arr))
        zp_arr = zp_arr[good]
        w_arr  = w_arr[good]

    zero_point = float(np.average(zp_arr, weights=w_arr))
    zp_scatter = float(np.std(zp_arr)) if len(zp_arr) > 1 else 0.05

    target_mag = target_instr + zero_point

    # Uncertainty: quadrature sum of target Poisson noise + zero-point scatter
    sigma_poisson = 1.0857 * (target_flux_err / target_flux) if target_flux_err > 0 else 0.05
    uncertainty   = float(math.sqrt(sigma_poisson ** 2 + zp_scatter ** 2))

    # ── Step 7: Ancillary quantities ──────────────────────────────────────────
    snr     = float(target_flux / target_flux_err) if target_flux_err > 0 else 0.0
    airmass = _compute_airmass(header, config)
    bjd     = _compute_bjd(header, ra_deg, dec_deg, config)

    # ── Step 8: Quality flag ──────────────────────────────────────────────────
    n_comp_used    = len(zero_points)
    min_comp       = int(phot_cfg.get("min_comparison_stars", 3))
    snr_threshold  = float(phot_cfg.get("snr_threshold", 20))
    max_unc        = float(phot_cfg.get("max_uncertainty", 0.3))
    max_airmass    = float(phot_cfg.get("max_airmass", 3.0))

    if (snr >= snr_threshold and uncertainty < max_unc
            and n_comp_used >= min_comp and airmass < max_airmass):
        quality_flag = "good"
    elif (snr >= snr_threshold * 0.5 and uncertainty < max_unc * 1.5
          and n_comp_used >= 2):
        quality_flag = "acceptable"
    else:
        quality_flag = "poor"

    elapsed = time.monotonic() - t0
    logger.info(
        "Pipeline done in %.1f s — %s  mag=%.3f±%.3f  SNR=%.1f  "
        "comp=%d  airmass=%.2f  quality=%s",
        elapsed, target_name, target_mag, uncertainty,
        snr, n_comp_used, airmass, quality_flag,
    )

    return {
        "target_name":      target_name,
        "bjd":              round(bjd, 6),
        "magnitude":        round(target_mag, 4),
        "uncertainty":      round(uncertainty, 4),
        "filter":           filter_name,
        "airmass":          round(airmass, 3),
        "fwhm":             round(fwhm_px, 2),
        "snr":              round(snr, 1),
        "comparison_stars": n_comp_used,
        "quality_flag":     quality_flag,
        "node_id":          node_id,
        "zero_point":       round(zero_point, 3),
        "zp_scatter":       round(zp_scatter, 3),
        "fits_file":        os.path.basename(fits_path),
    }


# ── Step 1 helpers: WCS / plate solving ───────────────────────────────────────

def _ensure_wcs(fits_path: str, ra_deg: float, dec_deg: float,
                *,
                solver: str = "astap",
                astap_path: str = "astap",
                solve_field_path: str = "solve-field",
                search_radius: float = 10.0,
                pixel_scale: Optional[float] = None,
                force: bool = False) -> bool:
    """
    Return True if the FITS file has a valid WCS, plate-solving if needed.

    solver  – "astrometry" (Astrometry.net's solve-field) or "astap".
    force   – when True, re-solve even if a WCS is already present. The Seestar
              writes an onboard WCS whose alt-az astrometry can be off by tens of
              arcsec; forcing a fresh solve on each stack removes that error
              before comparison stars are cross-matched to pixels.
    """
    # Reuse an existing WCS unless the caller insists on a fresh solve.
    if not force:
        try:
            with fits.open(fits_path, memmap=False, ignore_missing_simple=True) as hdul:
                hdr = hdul[0].header
                if "CRVAL1" in hdr and "CRVAL2" in hdr and "CD1_1" in hdr:
                    logger.info("WCS already in FITS header — skipping plate solve")
                    return True
                # Accept CDELT-style WCS only when it came from a real plate solver,
                # not from our pointing fallback (which may have used target coords
                # instead of the mount's actual RA/DEC).  Re-inject when it's ours.
                if "CRVAL1" in hdr and "CRVAL2" in hdr and "CDELT1" in hdr:
                    if hdr.get("BS_WCS") == "pointing":
                        logger.info(
                            "Existing WCS is pointing-based — re-injecting with "
                            "telescope RA/DEC from header"
                        )
                        # fall through so _inject_pointing_wcs corrects CRVAL
                    else:
                        logger.info("WCS (CDELT) already in FITS header — skipping plate solve")
                        return True
        except Exception as exc:
            logger.warning("Could not inspect FITS header: %s", exc)
    else:
        logger.info("force_plate_solve enabled — re-solving stack with %s", solver)

    if solver == "astrometry":
        if _run_astrometry_net(fits_path, ra_deg, dec_deg,
                               solve_field_path, search_radius, pixel_scale):
            return True
        logger.warning("Astrometry.net solve failed — falling back to ASTAP")
        return _run_astap(fits_path, ra_deg, dec_deg, astap_path, search_radius)

    logger.info("Running ASTAP plate solver")
    if _run_astap(fits_path, ra_deg, dec_deg, astap_path, search_radius):
        return True

    # No plate solver available — fall back to constructing a simple TAN WCS
    # from the telescope's reported pointing and the known pixel scale.  This
    # is less accurate than a proper solve (pointing errors survive), but it
    # allows the pipeline to proceed when ASTAP is not installed, and the
    # quality_flag will reflect the resulting zero-point scatter.
    if pixel_scale:
        logger.warning(
            "Plate solve unavailable — constructing approximate WCS from "
            "telescope pointing (RA=%.4f° Dec=%.4f°, scale=%.3f\"/px).  "
            "Install ASTAP for accurate astrometry.",
            ra_deg, dec_deg, pixel_scale,
        )
        return _inject_pointing_wcs(fits_path, ra_deg, dec_deg, pixel_scale)

    logger.error(
        "Plate solve failed and photometry.pixel_scale is not set — "
        "cannot construct fallback WCS.  Install ASTAP or set pixel_scale."
    )
    return False


def _inject_pointing_wcs(fits_path: str, ra_deg: float, dec_deg: float,
                         pixel_scale_arcsec: float) -> bool:
    """Write a simple TAN WCS into the FITS header from telescope pointing.

    Prefers RA/DEC keywords already in the FITS header (telescope mount's
    reported pointing) over the caller-supplied target coords, because the
    mount pointing is what physically centres the frame.  Falls back to
    ra_deg/dec_deg if those keys are absent.
    """
    try:
        with fits.open(fits_path, mode="update", memmap=False,
                       ignore_missing_simple=True) as hdul:
            hdr  = hdul[0].header
            # Use the mount's reported pointing if present; else target coords
            tel_ra  = float(hdr.get("RA",  ra_deg))
            tel_dec = float(hdr.get("DEC", dec_deg))
            h, w = hdul[0].data.shape[-2], hdul[0].data.shape[-1]
            ps   = pixel_scale_arcsec / 3600.0  # arcsec → degrees
            hdr["CTYPE1"] = ("RA---TAN", "TAN projection")
            hdr["CTYPE2"] = ("DEC--TAN", "TAN projection")
            hdr["CRPIX1"] = (w / 2.0 + 0.5, "Reference pixel X")
            hdr["CRPIX2"] = (h / 2.0 + 0.5, "Reference pixel Y")
            hdr["CRVAL1"] = (tel_ra,  "Reference RA (deg)")
            hdr["CRVAL2"] = (tel_dec, "Reference Dec (deg)")
            hdr["CDELT1"] = (-ps,  "deg/pixel (RA, East-left)")
            hdr["CDELT2"] = ( ps,  "deg/pixel (Dec)")
            hdr["BS_WCS"]  = ("pointing", "WCS source: telescope pointing")
            hdul.flush()
        return True
    except Exception as exc:
        logger.error("Could not inject pointing WCS: %s", exc)
        return False


def _run_astrometry_net(fits_path: str, ra_deg: float, dec_deg: float,
                        solve_field_path: str = "solve-field",
                        search_radius: float = 10.0,
                        pixel_scale: Optional[float] = None) -> bool:
    """
    Plate-solve with Astrometry.net's ``solve-field`` and write the resulting WCS
    (including SIP distortion terms) back into ``fits_path`` in place.

    ``solve-field`` writes its products to a scratch directory; we read the
    ``<base>.wcs`` header it produces and merge those keywords into the original
    file, so downstream ``WCS(header)`` construction is unchanged.
    """
    import shutil

    base    = os.path.splitext(os.path.basename(fits_path))[0]
    # Force workdir into /tmp — astrometry.net generates broken PNM paths when
    # the workdir is in macOS's /var/folders system temp tree.
    workdir = tempfile.mkdtemp(prefix="bs_anet_", dir="/tmp")
    # Also copy the input FITS into /tmp so solve-field's path handling is consistent.
    solve_input = os.path.join(workdir, base + ".fits")
    shutil.copy2(fits_path, solve_input)
    cmd = [
        solve_field_path, solve_input,
        "--overwrite", "--no-plots", "--no-verify",
        "--ra",  f"{ra_deg:.6f}",
        "--dec", f"{dec_deg:.6f}",
        "--radius", f"{float(search_radius):.3f}",
        "--dir", workdir,
        # We only want the .wcs header back; suppress the other heavy products.
        "--new-fits", "none",
        "--corr", "none", "--rdls", "none", "--match", "none",
        "--solved", "none", "--index-xyls", "none",
        "--cpulimit", "120",
    ]
    if pixel_scale:
        ps = float(pixel_scale)
        cmd += [
            "--scale-units", "arcsecperpix",
            "--scale-low",  f"{ps * 0.8:.4f}",
            "--scale-high", f"{ps * 1.2:.4f}",
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        wcs_path = os.path.join(workdir, base + ".wcs")
        if result.returncode == 0 and os.path.exists(wcs_path):
            if _inject_wcs(fits_path, wcs_path):
                logger.info("Astrometry.net plate solve succeeded")
                return True
            logger.error("Astrometry.net solved but WCS could not be written back")
            return False
        logger.error("Astrometry.net failed (rc=%d): %s",
                     result.returncode, (result.stderr or result.stdout)[:300])
        return False
    except FileNotFoundError:
        logger.error(
            "solve-field not found at '%s'. Install Astrometry.net "
            "(https://astrometry.net) with index files for your field scale and "
            "set photometry.solve_field_path in config.yaml",
            solve_field_path,
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("Astrometry.net timed out after 180 s")
        return False
    except Exception as exc:
        logger.error("Astrometry.net error: %s", exc)
        return False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# WCS keyword roots written by plate solvers (and any stale Seestar WCS we want
# to clear before injecting a fresh solution). SIP distortion uses A_/B_/AP_/BP_.
_WCS_SCALAR_KEYS = (
    "WCSAXES", "EQUINOX", "EPOCH", "LONPOLE", "LATPOLE",
    "RADESYS", "RADECSYS", "A_ORDER", "B_ORDER", "AP_ORDER", "BP_ORDER",
)
_WCS_PREFIXES = (
    "CRVAL", "CRPIX", "CDELT", "CTYPE", "CUNIT", "CROTA",
    "CD1_", "CD2_", "PC1_", "PC2_", "A_", "B_", "AP_", "BP_",
)
# Structural / bookkeeping cards in a .wcs file that must NOT be copied across.
_WCS_SKIP_KEYS = {
    "SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "EXTEND", "END",
    "COMMENT", "HISTORY", "DATE", "IMAGEW", "IMAGEH", "BSCALE", "BZERO",
}


def _is_wcs_key(key: str) -> bool:
    return key in _WCS_SCALAR_KEYS or any(key.startswith(p) for p in _WCS_PREFIXES)


def _strip_wcs(header) -> None:
    """Remove existing WCS keywords from a FITS header (in place)."""
    for key in [k for k in list(header.keys()) if k and _is_wcs_key(k)]:
        try:
            del header[key]
        except (KeyError, ValueError):
            pass


def _inject_wcs(fits_path: str, wcs_path: str) -> bool:
    """
    Copy WCS keywords from a solve-field ``.wcs`` header into ``fits_path``,
    replacing any pre-existing WCS so old and new solutions can't mix.
    """
    try:
        wcs_hdr = fits.Header.fromfile(
            wcs_path, sep="", endcard=True, padding=True,
        )
    except Exception:
        # .wcs is a normal (header-only) FITS file; fall back to a full open.
        try:
            with fits.open(wcs_path) as whdul:
                wcs_hdr = whdul[0].header
        except Exception as exc:
            logger.error("Cannot read solved WCS header %s: %s", wcs_path, exc)
            return False

    try:
        with fits.open(fits_path, mode="update", memmap=False,
                       ignore_missing_simple=True) as hdul:
            hdr = hdul[0].header
            _strip_wcs(hdr)
            for card in wcs_hdr.cards:
                key = card.keyword
                if not key or key in _WCS_SKIP_KEYS:
                    continue
                if _is_wcs_key(key):
                    hdr[key] = (card.value, card.comment)
            hdul.flush()
        return True
    except Exception as exc:
        logger.error("Cannot write WCS into %s: %s", fits_path, exc)
        return False


def _run_astap(fits_path: str, ra_deg: float, dec_deg: float,
               astap_path: str, search_radius: float) -> bool:
    """Call ASTAP CLI to plate-solve and write WCS back into the FITS file."""
    # ASTAP takes RA in decimal hours, SPD (South Polar Distance) in degrees
    ra_hours = ra_deg / 15.0
    spd      = 90.0 + dec_deg   # SPD = 90 + dec

    cmd = [
        astap_path,
        "-f",   fits_path,
        "-ra",  f"{ra_hours:.6f}",
        "-spd", f"{spd:.4f}",
        "-r",   str(int(search_radius)),
        "-update",              # write WCS into FITS header in-place
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0:
            logger.info("ASTAP plate solve succeeded")
            return True
        else:
            logger.error("ASTAP failed (rc=%d): %s",
                         result.returncode, (result.stderr or result.stdout)[:300])
            return False
    except FileNotFoundError:
        logger.error(
            "ASTAP not found at '%s'. "
            "Download from https://www.hnsky.org/astap.htm and set "
            "photometry.astap_path in config.yaml",
            astap_path,
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("ASTAP timed out after 90 s")
        return False
    except Exception as exc:
        logger.error("ASTAP error: %s", exc)
        return False


# ── Step 3 helpers: FWHM estimation ───────────────────────────────────────────

def _estimate_fwhm(data: np.ndarray, fallback_px: float = 4.0) -> float:
    """
    Estimate image FWHM in pixels using DAOStarFinder on bright, non-saturated
    stars, fitting second-moment Gaussians on 21×21 stamps.

    ``fallback_px`` is the last-resort PSF width used when no sources can be
    measured.  It is derived per-telescope from the pixel scale (see
    ``telescope_specs.derive_params``); the default 4.0 px matches the Seestar
    S50 (f/5) at typical seeing.
    """
    try:
        from photutils.detection import DAOStarFinder
        from astropy.stats import sigma_clipped_stats

        _, median, std = sigma_clipped_stats(data, sigma=3.0)
        if std <= 0:
            return fallback_px

        daofind = DAOStarFinder(fwhm=5.0, threshold=7.0 * std, exclude_border=True)
        sources = daofind(data - median)
        if sources is None or len(sources) == 0:
            logger.debug("DAOStarFinder found no sources — using fallback FWHM %.1f px", fallback_px)
            return fallback_px

        # Sort by peak flux; skip top 10% (possibly saturated) and bottom 50%
        sources.sort("peak")
        n = len(sources)
        lo = max(0, n // 2)
        hi = max(lo + 1, int(n * 0.9))
        subset = sources[lo:hi]

        # Support both old ('xcentroid') and new ('x_centroid') photutils column names
        x_col = "x_centroid" if "x_centroid" in sources.colnames else "xcentroid"
        y_col = "y_centroid" if "y_centroid" in sources.colnames else "ycentroid"

        fwhms = []
        half = 10  # stamp half-size
        for row in subset[:12]:
            x0 = int(row[x_col])
            y0 = int(row[y_col])
            xs, xe = max(0, x0 - half), min(data.shape[1], x0 + half + 1)
            ys, ye = max(0, y0 - half), min(data.shape[0], y0 + half + 1)
            stamp = data[ys:ye, xs:xe].copy() - median
            np.clip(stamp, 0, None, out=stamp)

            total = float(stamp.sum())
            if total <= 0:
                continue

            # Second-moment FWHM along each axis
            col_s = stamp.sum(axis=0)
            row_s = stamp.sum(axis=1)
            xi = np.arange(col_s.size, dtype=float)
            yi = np.arange(row_s.size, dtype=float)

            xm = float(np.dot(xi, col_s) / col_s.sum()) if col_s.sum() > 0 else half
            ym = float(np.dot(yi, row_s) / row_s.sum()) if row_s.sum() > 0 else half

            sx2 = float(np.dot((xi - xm) ** 2, col_s) / col_s.sum()) if col_s.sum() > 0 else 4.0
            sy2 = float(np.dot((yi - ym) ** 2, row_s) / row_s.sum()) if row_s.sum() > 0 else 4.0

            fwhm = 2.355 * math.sqrt(max((sx2 + sy2) / 2.0, 0.5))
            if 1.5 < fwhm < 25.0:
                fwhms.append(fwhm)

        if not fwhms:
            return fallback_px
        return float(np.median(fwhms))

    except Exception as exc:
        logger.warning("FWHM estimation failed: %s — using fallback %.1f px", exc, fallback_px)
        return fallback_px


# ── Step 4 helpers: comparison star queries ───────────────────────────────────

def _gather_comparison_stars(
    target_name: str,
    ra_deg: float,
    dec_deg: float,
    field_radius_deg: float,
    mag_limit: float,
    catalogs,
    target_count: int,
) -> list:
    """
    Query the configured catalogs in order, accumulating de-duplicated
    comparison stars until ``target_count`` is reached or the list is exhausted.

    Recognised catalog names: "aavso", "apass", "atlas", "gaia". Unknown names
    are skipped with a warning. Order matters — earlier catalogs win on
    duplicates, so list curated/most-reliable photometry first.
    """
    comp_stars: list = []
    for raw in catalogs:
        cat = str(raw).strip().lower()
        if len(comp_stars) >= target_count:
            break
        try:
            if cat == "aavso":
                new = _get_comparison_stars_aavso(
                    target_name, ra_deg, dec_deg, field_radius_deg, mag_limit)
            elif cat == "apass":
                new = _get_comparison_stars_apass(
                    ra_deg, dec_deg, field_radius_deg, mag_limit)
            elif cat == "atlas":
                new = _get_comparison_stars_atlas(
                    ra_deg, dec_deg, field_radius_deg, mag_limit)
            elif cat == "gaia":
                new = _get_comparison_stars_gaia(
                    ra_deg, dec_deg, field_radius_deg, mag_limit)
            else:
                logger.warning("Unknown comparison catalog '%s' — skipping", cat)
                continue
        except Exception as exc:
            logger.warning("Comparison catalog '%s' query failed: %s", cat, exc)
            continue

        before = len(comp_stars)
        comp_stars = _merge_comp_stars(comp_stars, new)
        logger.info("Catalog %s: +%d unique (%d total)",
                    cat, len(comp_stars) - before, len(comp_stars))

    return comp_stars


def _merge_comp_stars(existing: list, new: list, sep_arcsec: float = 5.0) -> list:
    """
    Append ``new`` comparison stars to ``existing``, dropping any within
    ``sep_arcsec`` of one already kept. The RA threshold is scaled by cos(dec)
    so the match radius stays circular on the sky at high declination.
    """
    out = list(existing)
    coords = [(c["ra_deg"], c["dec_deg"]) for c in out]
    thr_deg = sep_arcsec / 3600.0
    for s in new:
        cos_dec = math.cos(math.radians(s["dec_deg"]))
        duplicate = any(
            abs((s["ra_deg"] - er) * cos_dec) < thr_deg and abs(s["dec_deg"] - ed) < thr_deg
            for er, ed in coords
        )
        if not duplicate:
            out.append(s)
            coords.append((s["ra_deg"], s["dec_deg"]))
    return out


def _get_comparison_stars_aavso(
    target_name: str,
    ra_deg: float,
    dec_deg: float,
    field_radius_deg: float,
    mag_limit: float,
) -> list:
    """
    Query the AAVSO Variable Star Plotter (VSP) API for comparison stars.
    Returns a list of dicts with ra_deg, dec_deg, mag_v, mag_err.
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not installed — cannot query AAVSO VSP")
        return []

    fov_arcmin = int(field_radius_deg * 2 * 60)
    # VSP requires star name OR ra/dec, not both.
    if target_name:
        params = {"star": target_name, "fov": fov_arcmin, "maglimit": mag_limit, "format": "json"}
    else:
        params = {"ra": ra_deg, "dec": dec_deg, "fov": fov_arcmin, "maglimit": mag_limit, "format": "json"}
    url = "https://www.aavso.org/apps/vsp/api/chart/"
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning("AAVSO VSP returned HTTP %d", resp.status_code)
            return []
        payload = resp.json()
    except Exception as exc:
        logger.warning("AAVSO VSP request failed: %s", exc)
        return []

    def _sexa_to_deg(s: str, is_ra: bool) -> float:
        """Convert sexagesimal 'HH:MM:SS.ss' or 'DD:MM:SS.s' to decimal degrees."""
        parts = str(s).split(":")
        d = abs(float(parts[0]))
        m = float(parts[1]) if len(parts) > 1 else 0.0
        sec = float(parts[2]) if len(parts) > 2 else 0.0
        val = d + m / 60.0 + sec / 3600.0
        if is_ra:
            val *= 15.0  # hours → degrees
        elif str(s).strip().startswith("-"):
            val = -val
        return val

    comp_stars = []
    for star in payload.get("photometry", []):
        try:
            ra_s  = _sexa_to_deg(star["ra"],  is_ra=True)
            dec_s = _sexa_to_deg(star["dec"], is_ra=False)
        except (KeyError, ValueError, TypeError):
            continue
        bands = {b["band"]: b for b in star.get("bands", [])}
        # Prefer V, fall back to B, then R
        for band_key in ("V", "B", "R"):
            if band_key in bands:
                try:
                    # VSP API uses 'mag' and 'error' (not 'magnitude'/'uncertainty')
                    mag = float(bands[band_key].get("mag") or bands[band_key].get("magnitude"))
                    mag_err = float(bands[band_key].get("error") or bands[band_key].get("uncertainty") or 0.05)
                except (TypeError, ValueError):
                    continue
                if mag > mag_limit:
                    break
                comp_stars.append({
                    "auid":    star.get("auid", ""),
                    "ra_deg":  ra_s,
                    "dec_deg": dec_s,
                    "mag_v":   mag,
                    "mag_err": mag_err,
                    "source":  f"aavso_{band_key}",
                })
                break

    logger.info("AAVSO VSP: %d comparison stars for '%s'", len(comp_stars), target_name)
    return comp_stars


def _get_comparison_stars_gaia(
    ra_deg: float,
    dec_deg: float,
    field_radius_deg: float,
    mag_limit: float,
    n_max: int = 15,
) -> list:
    """
    Query Gaia DR3 via astroquery for comparison stars.
    Uses G-band magnitude as a proxy for V (accurate to ~0.1–0.2 mag for
    solar-type stars; sufficient for broadband CV photometry).
    """
    try:
        from astroquery.gaia import Gaia
        import astropy.units as u
    except ImportError:
        logger.warning("astroquery not installed — cannot query Gaia")
        return []

    coord  = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    radius = field_radius_deg * u.deg

    try:
        Gaia.MAIN_GAIA_TABLE = "gaiadr3.gaia_source"
        Gaia.ROW_LIMIT = n_max * 3  # oversample; we'll filter
        j = Gaia.cone_search_async(coord, radius)
        results = j.get_results()
    except Exception as exc:
        logger.warning("Gaia cone search failed: %s", exc)
        return []

    if results is None or len(results) == 0:
        return []

    # Filter out saturated, variable, or faint stars
    try:
        mask = (
            (results["phot_g_mean_mag"] < mag_limit) &
            (results["phot_g_mean_mag"] > 8.0) &
            (results["phot_g_mean_flux_over_error"] > 50)   # good S/N
        )
        results = results[mask]
    except Exception:
        pass

    results.sort("phot_g_mean_mag")

    comp_stars = []
    for row in results[:n_max]:
        try:
            g_mag = float(row["phot_g_mean_mag"])

            # Evans et al. 2018 (A&A 616, A4) G→V transformation using BP-RP color.
            # V = G - (c0 + c1*(BP-RP) + c2*(BP-RP)^2)
            # Valid range: -0.5 < BP-RP < 2.75
            try:
                bp_rp = float(row["bp_rp"])
                if -0.5 <= bp_rp <= 2.75:
                    v_mag   = g_mag - (-0.01760 - 0.006860 * bp_rp - 0.1732 * bp_rp ** 2)
                    mag_err = 0.05   # residual scatter on the Evans relation (~0.03–0.05 mag)
                else:
                    # Out of calibration range — use G as-is with larger uncertainty
                    v_mag   = g_mag
                    mag_err = 0.20
            except (TypeError, ValueError, KeyError):
                v_mag   = g_mag
                mag_err = 0.15   # no color info; G→V offset unknown

            comp_stars.append({
                "auid":    str(row["source_id"]),
                "ra_deg":  float(row["ra"]),
                "dec_deg": float(row["dec"]),
                "mag_v":   v_mag,
                "mag_err": mag_err,
                "source":  "gaia_dr3",
            })
        except Exception:
            continue

    logger.info("Gaia DR3: %d comparison stars", len(comp_stars))
    return comp_stars


def _get_comparison_stars_apass(
    ra_deg: float,
    dec_deg: float,
    field_radius_deg: float,
    mag_limit: float,
    n_max: int = 25,
) -> list:
    """
    Query APASS DR9 (Vizier II/336/apass9) for comparison stars.

    APASS reports Johnson V directly (to V≈17) over the whole sky, so it needs no
    colour transformation and stays dense at high declination where Tycho-2 and
    AAVSO sequences run out — exactly the regime where Gaia's G→V conversion adds
    avoidable scatter.
    """
    try:
        from astroquery.vizier import Vizier
        import astropy.units as u
    except ImportError:
        logger.warning("astroquery not installed — cannot query APASS")
        return []

    cols = ["RAJ2000", "DEJ2000", "Vmag", "e_Vmag", "Bmag", "e_Bmag"]
    viz = Vizier(columns=cols,
                 column_filters={"Vmag": f">8.0 && <{mag_limit}"})
    viz.ROW_LIMIT = n_max * 3
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)

    try:
        result = viz.query_region(coord, radius=field_radius_deg * u.deg,
                                  catalog="II/336/apass9")
    except Exception as exc:
        logger.warning("APASS Vizier query failed: %s", exc)
        return []

    if not result or len(result) == 0:
        return []
    table = result[0]

    comp_stars = []
    for row in table:
        try:
            if _masked(row, "Vmag"):
                continue
            v_mag = float(row["Vmag"])
            if not (8.0 < v_mag < mag_limit):
                continue
            mag_err = 0.05 if _masked(row, "e_Vmag") else float(row["e_Vmag"])
            comp_stars.append({
                "auid":    "",
                "ra_deg":  float(row["RAJ2000"]),
                "dec_deg": float(row["DEJ2000"]),
                "mag_v":   v_mag,
                "mag_err": max(mag_err, 0.01),
                "source":  "apass_dr9",
            })
        except (TypeError, ValueError, KeyError):
            continue

    comp_stars.sort(key=lambda c: c["mag_v"])
    comp_stars = comp_stars[:n_max]
    logger.info("APASS DR9: %d comparison stars", len(comp_stars))
    return comp_stars


def _get_comparison_stars_atlas(
    ra_deg: float,
    dec_deg: float,
    field_radius_deg: float,
    mag_limit: float,
    n_max: int = 25,
) -> list:
    """
    Query ATLAS-REFCAT2 (Vizier J/ApJ/867/105/refcat2) for comparison stars.

    REFCAT2 is an all-sky catalog complete to m≈19 in Sloan g,r,i — far deeper
    than APASS. It reports Sloan magnitudes, so V is synthesised from g,r with
    the Lupton (2005) transformation V = g − 0.5784·(g−r) − 0.0038 (σ≈0.02 mag),
    making it the best fallback for sparse, high-declination fields.
    """
    try:
        from astroquery.vizier import Vizier
        import astropy.units as u
    except ImportError:
        logger.warning("astroquery not installed — cannot query ATLAS-REFCAT2")
        return []

    cols = ["RA_ICRS", "DE_ICRS", "gmag", "rmag"]
    viz = Vizier(columns=cols, column_filters={"gmag": f">8.0 && <{mag_limit + 1.0}"})
    viz.ROW_LIMIT = n_max * 4
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)

    try:
        result = viz.query_region(coord, radius=field_radius_deg * u.deg,
                                  catalog="J/ApJ/867/105/refcat2")
    except Exception as exc:
        logger.warning("ATLAS-REFCAT2 Vizier query failed: %s", exc)
        return []

    if not result or len(result) == 0:
        return []
    table = result[0]

    comp_stars = []
    for row in table:
        try:
            if _masked(row, "gmag") or _masked(row, "rmag"):
                continue
            g = float(row["gmag"])
            r = float(row["rmag"])
            v_mag = g - 0.5784 * (g - r) - 0.0038   # Lupton 2005 g,r → Johnson V
            if not (8.0 < v_mag < mag_limit):
                continue
            comp_stars.append({
                "auid":    "",
                "ra_deg":  float(row["RA_ICRS"]),
                "dec_deg": float(row["DE_ICRS"]),
                "mag_v":   v_mag,
                "mag_err": 0.05,   # transformation residual + catalog photometry
                "source":  "atlas_refcat2",
            })
        except (TypeError, ValueError, KeyError):
            continue

    comp_stars.sort(key=lambda c: c["mag_v"])
    comp_stars = comp_stars[:n_max]
    logger.info("ATLAS-REFCAT2: %d comparison stars", len(comp_stars))
    return comp_stars


def _masked(row, col: str) -> bool:
    """True if a Vizier table cell is missing/masked (so it should be skipped)."""
    try:
        value = row[col]
    except (KeyError, IndexError):
        return True
    if value is None or value is np.ma.masked:
        return True
    try:
        return bool(np.ma.is_masked(value)) or bool(np.isnan(float(value)))
    except (TypeError, ValueError):
        return False


# ── Step 5 helpers: aperture photometry ───────────────────────────────────────

def _aperture_photometry(
    data: np.ndarray,
    positions: list,          # [(x, y), ...]  pixel coords
    ap_radius: float,
    ann_inner: float,
    ann_outer: float,
    read_noise: float = 5.0,  # in electrons
    gain: float = 1.0,        # e⁻/ADU
) -> tuple:
    """
    Measure background-subtracted flux at each position.

    Returns (fluxes, flux_errors) as numpy arrays, or (None, None) on failure.
    """
    try:
        from photutils.aperture import (
            CircularAperture, CircularAnnulus, aperture_photometry as phot_ap,
        )
        from astropy.stats import sigma_clipped_stats

        apertures = CircularAperture(positions, r=ap_radius)
        annuli    = CircularAnnulus(positions, r_in=ann_inner, r_out=ann_outer)

        # Per-aperture sky background from sigma-clipped annulus median
        bkg_per_px = np.zeros(len(positions))
        for i, mask in enumerate(annuli.to_mask(method="center")):
            ann_data = mask.multiply(data)
            if ann_data is None:
                continue
            ann_1d = ann_data[mask.data > 0]
            if len(ann_1d) < 5:
                continue
            _, bkg_median, _ = sigma_clipped_stats(ann_1d, sigma=3.0)
            bkg_per_px[i] = float(bkg_median)

        phot_table = phot_ap(data, apertures)
        ap_area    = math.pi * ap_radius ** 2

        raw_sum    = np.array(phot_table["aperture_sum"], dtype=float)
        net_flux   = raw_sum - bkg_per_px * ap_area

        # CCD noise model in ADU²:
        #   sigma² = (source_ADU + sky_ADU) / gain   [Poisson, converted to ADU]
        #          + N_pix * (read_noise_e / gain)²   [read noise per pixel]
        # read_noise is in electrons; gain is e⁻/ADU.
        flux_var = (
            (np.maximum(net_flux, 0) + ap_area * bkg_per_px) / gain
            + ap_area * (read_noise / gain) ** 2
        )
        flux_errors = np.sqrt(np.maximum(flux_var, 1.0))

        return net_flux, flux_errors

    except Exception as exc:
        logger.error("Aperture photometry raised: %s", exc)
        return None, None


# ── Helper: BJD ───────────────────────────────────────────────────────────────

def _compute_bjd(header: dict, ra_deg: float, dec_deg: float, config: dict) -> float:
    """
    Return Barycentric Julian Date (BJD_TDB) for the exposure mid-point.

    Two steps are required for a real BJD, and the previous implementation did
    only the first:
      1. Convert the UTC timestamp to the TDB time scale.
      2. Add the light-travel time from the observatory to the solar-system
         barycenter for the target's direction (Time.light_travel_time).  This
         Rømer term is up to ±8.3 minutes and is what distinguishes BJD from a
         plain geocentric/topocentric JD.

    The observer's position on Earth contributes ≤21 ms to the correction, so
    when the observatory location is unknown we evaluate it at the geocenter —
    still far better than omitting the barycentric term entirely.  Falls back to
    JD(TDB) at the observer if the correction itself fails.
    """
    date_obs = header.get("DATE-OBS", "")
    t = None
    if date_obs:
        # Try multiple formats: "fits" handles timezone offsets, "isot" handles
        # the common ISO-8601 without timezone, "iso" catches the rest.
        for fmt in ("fits", "isot", "iso"):
            try:
                s = date_obs
                if fmt in ("isot", "iso") and (s.endswith("Z") or "+" in s[10:] or s[19:].startswith("-")):
                    s = s[:19]
                t = Time(s, format=fmt, scale="utc")
                break
            except Exception:
                continue
    if t is None:
        if date_obs:
            logger.warning("Could not parse DATE-OBS '%s' — using current time", date_obs)
        else:
            logger.debug("DATE-OBS missing — using current time for BJD")
        t = Time.now()

    location = _observer_location(config)
    try:
        coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
        ltt = t.light_travel_time(coord, kind="barycentric", location=location)
        return float((t.tdb + ltt).jd)
    except Exception as exc:
        logger.warning("Barycentric correction failed (%s) — using JD(TDB) without it", exc)
        return float(t.tdb.jd)


def _observer_location(config: dict) -> EarthLocation:
    """Observer EarthLocation from config, defaulting to the geocenter.

    Prefers observatory lat/lon (filled by geolocation enrichment), then
    safety.observer.  The barycentric Rømer term is dominated by Earth's
    position relative to the barycenter, so the geocenter fallback loses only
    the sub-21 ms observer-on-Earth contribution.
    """
    obs = config.get("observatory", {}) or {}
    lat = obs.get("latitude")
    lon = obs.get("longitude")
    if lat is None or lon is None:
        so = config.get("safety", {}).get("observer", {}) or {}
        lat = so.get("latitude")
        lon = so.get("longitude")
    try:
        lat = float(lat)
        lon = float(lon)
        if lat == 0.0 and lon == 0.0:
            raise ValueError("unset location")
        return EarthLocation(lat=lat * u.deg, lon=lon * u.deg,
                             height=float(obs.get("elevation", 0.0) or 0.0) * u.m)
    except (TypeError, ValueError):
        return EarthLocation.from_geocentric(0.0 * u.m, 0.0 * u.m, 0.0 * u.m)


# ── Helper: airmass ────────────────────────────────────────────────────────────

def _compute_airmass(header: dict, config: dict) -> float:
    """
    Return airmass.  Priority:
      1. AIRMASS keyword in FITS header
      2. Compute from target RA/Dec, DATE-OBS, and observer location in config
      3. Return 1.5 (moderate airmass fallback)
    """
    am = header.get("AIRMASS")
    if am is not None:
        try:
            return float(am)
        except (TypeError, ValueError):
            pass

    ra_deg  = header.get("RA")  or header.get("CRVAL1")
    dec_deg = header.get("DEC") or header.get("CRVAL2")
    date_obs = header.get("DATE-OBS", "")

    if not (ra_deg and dec_deg and date_obs):
        return 1.5

    obs_cfg = config.get("safety", {}).get("observer", {})
    lat = float(obs_cfg.get("latitude", 0.0))
    lon = float(obs_cfg.get("longitude", 0.0))
    if lat == 0.0 and lon == 0.0:
        logger.debug("Observer lat/lon not configured — airmass defaulting to 1.5")
        return 1.5

    try:
        location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)
        t        = Time(date_obs, format="isot", scale="utc")
        coord    = SkyCoord(ra=float(ra_deg) * u.deg, dec=float(dec_deg) * u.deg)
        altaz    = coord.transform_to(AltAz(obstime=t, location=location))
        alt_deg  = float(altaz.alt.deg)
        if alt_deg <= 5.0:
            return 5.76   # sec(85°)
        return float(1.0 / math.cos(math.radians(90.0 - alt_deg)))
    except Exception as exc:
        logger.warning("Airmass computation failed: %s", exc)
        return 1.5
