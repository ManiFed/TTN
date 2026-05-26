#!/usr/bin/env python3
"""
Local photometry pipeline — Layer 5 in the Boundless Skies architecture.

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

    # Handle 3-D cubes (C, H, W) → (H, W) by taking first plane
    if data.ndim == 3:
        data = data[0]
    if data.ndim != 2:
        logger.error("Unexpected data shape %s in %s", data.shape, fits_path)
        return None

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
    astap_path    = phot_cfg.get("astap_path", "astap")
    search_radius = float(phot_cfg.get("astap_search_radius", 10))
    if not _ensure_wcs(fits_path, ra_deg, dec_deg, astap_path, search_radius):
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
    fwhm_px = _estimate_fwhm(data)
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

    comp_stars = _get_comparison_stars_aavso(
        target_name, ra_deg, dec_deg, field_radius_deg, mag_limit
    )
    if len(comp_stars) < 3:
        logger.info("AAVSO returned %d comp stars — querying Gaia DR3", len(comp_stars))
        gaia_stars = _get_comparison_stars_gaia(ra_deg, dec_deg, field_radius_deg, mag_limit)
        # Merge: AAVSO preferred, Gaia fills in
        existing_coords = [(c["ra_deg"], c["dec_deg"]) for c in comp_stars]
        for gs in gaia_stars:
            # Avoid duplicates (within 5 arcsec)
            duplicate = any(
                abs(gs["ra_deg"] - er) < 0.0014 and abs(gs["dec_deg"] - ed) < 0.0014
                for er, ed in existing_coords
            )
            if not duplicate:
                comp_stars.append(gs)
                existing_coords.append((gs["ra_deg"], gs["dec_deg"]))

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
            comp_in_field.append(cs)

    logger.info("Comparison stars in field: %d / %d", len(comp_in_field), len(comp_stars))
    if len(comp_in_field) < 2:
        logger.error("Too few comparison stars in image field (%d)", len(comp_in_field))
        return None

    # ── Step 5: Aperture photometry ───────────────────────────────────────────
    positions = [(tx, ty)] + [(cs["x_px"], cs["y_px"]) for cs in comp_in_field]
    read_noise = float(phot_cfg.get("read_noise", header.get("RDNOISE", 5.0)))
    fluxes, flux_errors = _aperture_photometry(data, positions, ap_r, ann_in, ann_out, read_noise)
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
        weight = 1.0 / max(sigma_instr ** 2, 1e-6)
        zero_points.append(zp)
        zp_weights.append(weight)

    if not zero_points:
        logger.error("Could not compute zero point — no valid comparison stars with known magnitudes")
        return None

    zp_arr  = np.array(zero_points)
    w_arr   = np.array(zp_weights)
    zero_point = float(np.average(zp_arr, weights=w_arr))
    zp_scatter = float(np.std(zp_arr)) if len(zp_arr) > 1 else 0.05

    target_mag = target_instr + zero_point

    # Uncertainty: quadrature sum of target Poisson noise + zero-point scatter
    sigma_poisson = 1.0857 * (target_flux_err / target_flux) if target_flux_err > 0 else 0.05
    uncertainty   = float(math.sqrt(sigma_poisson ** 2 + zp_scatter ** 2))

    # ── Step 7: Ancillary quantities ──────────────────────────────────────────
    snr     = float(target_flux / target_flux_err) if target_flux_err > 0 else 0.0
    airmass = _compute_airmass(header, config)
    bjd     = _compute_bjd(header)

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
                astap_path: str, search_radius: float) -> bool:
    """Return True if the FITS file has a valid WCS, running ASTAP if needed."""
    # Check for existing WCS (Seestar plate-solves onboard)
    try:
        with fits.open(fits_path, memmap=False, ignore_missing_simple=True) as hdul:
            hdr = hdul[0].header
            if "CRVAL1" in hdr and "CRVAL2" in hdr and "CD1_1" in hdr:
                logger.info("WCS already in FITS header — skipping plate solve")
                return True
            # Also accept CDELT-style WCS
            if "CRVAL1" in hdr and "CRVAL2" in hdr and "CDELT1" in hdr:
                logger.info("WCS (CDELT) already in FITS header — skipping plate solve")
                return True
    except Exception as exc:
        logger.warning("Could not inspect FITS header: %s", exc)

    logger.info("No WCS found — running ASTAP plate solver")
    return _run_astap(fits_path, ra_deg, dec_deg, astap_path, search_radius)


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

def _estimate_fwhm(data: np.ndarray) -> float:
    """
    Estimate image FWHM in pixels using DAOStarFinder on bright, non-saturated
    stars, fitting second-moment Gaussians on 21×21 stamps.

    Falls back to 4.0 px (typical for Seestar f/5 at good seeing) on failure.
    """
    try:
        from photutils.detection import DAOStarFinder
        from astropy.stats import sigma_clipped_stats

        _, median, std = sigma_clipped_stats(data, sigma=3.0)
        if std <= 0:
            return 4.0

        daofind = DAOStarFinder(fwhm=5.0, threshold=7.0 * std, exclude_border=True)
        sources = daofind(data - median)
        if sources is None or len(sources) == 0:
            logger.debug("DAOStarFinder found no sources — using default FWHM 4.0 px")
            return 4.0

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
            return 4.0
        return float(np.median(fwhms))

    except Exception as exc:
        logger.warning("FWHM estimation failed: %s — using default 4.0 px", exc)
        return 4.0


# ── Step 4 helpers: comparison star queries ───────────────────────────────────

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
    params = {
        "star":     target_name,
        "ra":       ra_deg,
        "dec":      dec_deg,
        "fov":      fov_arcmin,
        "maglimit": mag_limit,
        "format":   "json",
    }
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

    comp_stars = []
    for star in payload.get("photometry", []):
        bands = {b["band"]: b for b in star.get("bands", [])}
        # Prefer V, fall back to B, then R
        for band_key in ("V", "B", "R"):
            if band_key in bands:
                try:
                    mag = float(bands[band_key]["magnitude"])
                    mag_err = float(bands[band_key].get("uncertainty") or 0.05)
                except (TypeError, ValueError):
                    continue
                if mag > mag_limit:
                    break
                comp_stars.append({
                    "auid":    star.get("auid", ""),
                    "ra_deg":  float(star["ra"]),
                    "dec_deg": float(star["dec"]),
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
            comp_stars.append({
                "auid":    str(row["source_id"]),
                "ra_deg":  float(row["ra"]),
                "dec_deg": float(row["dec"]),
                "mag_v":   float(row["phot_g_mean_mag"]),
                "mag_err": 0.05,   # conservative; G→V transform uncertainty dominates
                "source":  "gaia_dr3",
            })
        except Exception:
            continue

    logger.info("Gaia DR3: %d comparison stars", len(comp_stars))
    return comp_stars


# ── Step 5 helpers: aperture photometry ───────────────────────────────────────

def _aperture_photometry(
    data: np.ndarray,
    positions: list,          # [(x, y), ...]  pixel coords
    ap_radius: float,
    ann_inner: float,
    ann_outer: float,
    read_noise: float = 5.0,
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

        # Noise model: shot noise (net signal + sky) + read noise per pixel
        read_noise_adu = read_noise
        flux_var = (
            np.maximum(net_flux, 0)               # shot noise from source
            + ap_area * bkg_per_px                # shot noise from sky
            + ap_area * read_noise_adu ** 2       # read noise
        )
        flux_errors = np.sqrt(np.maximum(flux_var, 1.0))

        return net_flux, flux_errors

    except Exception as exc:
        logger.error("Aperture photometry raised: %s", exc)
        return None, None


# ── Helper: BJD ───────────────────────────────────────────────────────────────

def _compute_bjd(header: dict) -> float:
    """
    Return Barycentric Julian Date (BJD_TCB) from FITS header DATE-OBS.
    Falls back to current time if the header entry is missing or unparseable.
    """
    date_obs = header.get("DATE-OBS", "")
    if not date_obs:
        logger.debug("DATE-OBS missing — using current time for BJD")
        return float(Time.now().tcb.jd)
    try:
        t = Time(date_obs, format="isot", scale="utc")
        return float(t.tcb.jd)
    except Exception as exc:
        logger.warning("Could not parse DATE-OBS '%s': %s", date_obs, exc)
        return float(Time.now().tcb.jd)


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
