#!/usr/bin/env python3
"""
Deterministic synthetic FITS scene generator for photometry validation.

Produces star-field images with *known ground truth* — every photon comes from
a magnitude we chose, through a zero point we chose, with noise from a seeded
RNG — so tests can assert that the pipeline recovers the truth within its own
reported uncertainty, or rejects/flags the frame for the right reason.

No network, no plate solver: scenes are written with a valid TAN WCS so
run_pipeline() takes the "WCS already in header" path, and the comparison-star
catalog is written to a frozen JSON file consumed by the pipeline's "file"
catalog backend (photometry.comparison_star_file).

Physics model
-------------
    flux_ADU(m)   = 10 ** (-0.4 * (m - zero_point))          (total in aperture)
    PSF           = circular Gaussian, sigma = fwhm / 2.355
    noise         = Poisson((source + sky) * gain) / gain  +  N(0, read_noise/gain)
    saturation    = optional hard clip at `clip_adu` (models a clipped PSF core)

Usage
-----
    scene = make_scene(tmpdir, target_mag=11.5, seed=3)
    result = run_pipeline(scene.fits_path, scene.config())
    assert abs(result["magnitude"] - scene.target_mag) < 3 * result["uncertainty"]
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


# ── Scene container ────────────────────────────────────────────────────────────

@dataclass
class Scene:
    fits_path: str
    catalog_path: str
    target_name: str
    target_mag: float
    target_ra: float
    target_dec: float
    zero_point: float
    fwhm_px: float
    pixel_scale: float
    gain: float
    read_noise: float
    sky_adu: float
    exptime_s: float
    date_obs: str
    comp_catalog: list = field(default_factory=list)   # frozen catalog entries
    comp_true_flux: dict = field(default_factory=dict) # auid -> true flux (ADU)
    target_xy: tuple = (0.0, 0.0)                      # true pixel position

    def config(self, **overrides) -> dict:
        """A photometry config wired for a fully offline run on this scene.

        The scene's WCS is already in the header, the comparison catalog is
        the frozen file written next to the FITS, and the solver paths point
        at binaries that don't exist (they must never be reached).
        """
        phot = {
            "node_id":              "validation-node",
            "filter_name":          "CV",
            "solver":               "astap",
            "astap_path":           "/nonexistent/astap",
            "solve_field_path":     "/nonexistent/solve-field",
            "force_plate_solve":    False,
            "pixel_scale":          self.pixel_scale,
            "gain":                 self.gain,
            "read_noise":           self.read_noise,
            "fwhm_fallback_px":     4.0,
            "aperture_factor":      1.5,
            "annulus_inner":        4.0,
            "annulus_outer":        6.0,
            "field_radius":         0.5,
            "mag_limit":            15.0,
            "mag_min":              10.0,
            "comparison_catalogs":  ["file"],
            "comparison_star_file": self.catalog_path,
            "comparison_target_count": 25,
            "min_comparison_stars": 3,
            "snr_threshold":        20,
            "max_uncertainty":      0.3,
            "max_airmass":          3.0,
            "saturation_adu":       60000,
        }
        phot.update(overrides)
        return {
            "photometry": phot,
            "observatory": {"latitude": 40.0, "longitude": -105.0, "elevation": 1600},
            "safety": {"observer": {"latitude": 40.0, "longitude": -105.0}},
        }


# ── Rendering helpers ──────────────────────────────────────────────────────────

def _render_star(img: np.ndarray, x: float, y: float, flux: float,
                 sigma: float) -> None:
    """Add a 2-D Gaussian of total ``flux`` at (x, y), in place."""
    h, w = img.shape
    r = int(math.ceil(5 * sigma))
    x0, x1 = max(0, int(x) - r), min(w, int(x) + r + 1)
    y0, y1 = max(0, int(y) - r), min(h, int(y) + r + 1)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    g = np.exp(-(((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * sigma ** 2)))
    img[y0:y1, x0:x1] += flux * g / (2.0 * math.pi * sigma ** 2)


def _tan_wcs(shape: tuple, ra0: float, dec0: float, pixel_scale_arcsec: float,
             crpix_shift: tuple = (0.0, 0.0)) -> WCS:
    """TAN WCS centred on (ra0, dec0); crpix_shift injects astrometric error."""
    h, w = shape
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = [ra0, dec0]
    wcs.wcs.crpix = [w / 2.0 + 0.5 + crpix_shift[0], h / 2.0 + 0.5 + crpix_shift[1]]
    scale_deg = pixel_scale_arcsec / 3600.0
    wcs.wcs.cd = [[-scale_deg, 0.0], [0.0, scale_deg]]
    return wcs


# ── Scene factory ──────────────────────────────────────────────────────────────

def make_scene(
    out_dir: str,
    *,
    name: str = "scene",
    seed: int = 0,
    shape: tuple = (480, 480),
    pixel_scale: float = 2.4,
    ra0: float = 150.0,
    dec0: float = 20.0,
    target_name: str = "VAL TARGET",
    target_mag: float = 11.5,
    target_offset_px: tuple = (0.0, 0.0),
    zero_point: float = 21.0,
    fwhm_px: float = 4.0,
    sky_adu: float = 150.0,
    read_noise_e: float = 5.0,
    gain: float = 1.0,
    n_comp: int = 8,
    comp_mags: list | None = None,          # explicit mags (overrides n_comp/range)
    comp_mag_range: tuple = (10.5, 13.5),
    comp_cat_err: float = 0.02,
    comp_mag_offsets: dict | None = None,   # auid -> catalog error to inject (mag)
    n_field_stars: int = 12,
    clip_adu: float | None = None,          # hard-clip the image (saturation)
    wcs_offset_px: tuple = (0.0, 0.0),      # header-WCS error vs. true sky
    imagetyp: str = "LIGHT",
    date_obs: str = "2026-01-15T06:30:00",
    exptime_s: float = 300.0,
    cube3d: bool = False,
    header_extra: dict | None = None,
    noise: bool = True,
) -> Scene:
    """
    Build one synthetic scene: FITS image + frozen comparison catalog + truth.

    ``comp_mag_offsets`` injects *catalog* errors: the star's light is rendered
    at its true magnitude but the catalog file lists mag_v + offset, modelling
    a wrong/variable comparison star.

    ``wcs_offset_px`` shifts the WCS written to the header while catalog
    coordinates stay true — every catalog position then lands offset in pixel
    space, exactly like the Seestar's onboard pointing error.
    """
    rng = np.random.default_rng(seed)
    h, w = shape
    sigma = fwhm_px / 2.355

    true_wcs   = _tan_wcs(shape, ra0, dec0, pixel_scale)
    header_wcs = _tan_wcs(shape, ra0, dec0, pixel_scale, crpix_shift=wcs_offset_px)

    img = np.full(shape, float(sky_adu), dtype=np.float64)

    # Target at frame centre (+ optional offset), position jittered sub-pixel
    tx = w / 2.0 + target_offset_px[0] + float(rng.uniform(-0.5, 0.5))
    ty = h / 2.0 + target_offset_px[1] + float(rng.uniform(-0.5, 0.5))
    target_flux = 10.0 ** (-0.4 * (target_mag - zero_point))
    _render_star(img, tx, ty, target_flux, sigma)
    t_sky = true_wcs.pixel_to_world(tx, ty)

    # Comparison stars on a jittered ring/grid, away from target and edges
    margin = 40
    comp_catalog: list = []
    comp_true_flux: dict = {}
    placed = [(tx, ty)]
    want = len(comp_mags) if comp_mags is not None else n_comp
    i = 0
    attempts = 0
    while i < want and attempts < 200:
        attempts += 1
        cx = float(rng.uniform(margin, w - margin))
        cy = float(rng.uniform(margin, h - margin))
        if any((cx - px) ** 2 + (cy - py) ** 2 < (8 * fwhm_px) ** 2
               for px, py in placed):
            continue
        mag = (float(comp_mags[i]) if comp_mags is not None
               else float(rng.uniform(*comp_mag_range)))
        flux = 10.0 ** (-0.4 * (mag - zero_point))
        _render_star(img, cx, cy, flux, sigma)
        placed.append((cx, cy))
        sky = true_wcs.pixel_to_world(cx, cy)
        auid = f"SYN-{i:03d}"
        cat_mag = mag + float((comp_mag_offsets or {}).get(auid, 0.0))
        comp_catalog.append({
            "auid":    auid,
            "ra_deg":  float(sky.ra.deg),
            "dec_deg": float(sky.dec.deg),
            "mag_v":   round(cat_mag, 4),
            "mag_err": comp_cat_err,
            "source":  "synthetic_truth",
        })
        comp_true_flux[auid] = flux
        i += 1

    # Anonymous field stars so FWHM estimation has sources to measure
    for _ in range(n_field_stars):
        fx = float(rng.uniform(margin, w - margin))
        fy = float(rng.uniform(margin, h - margin))
        if any((fx - px) ** 2 + (fy - py) ** 2 < (6 * fwhm_px) ** 2
               for px, py in placed):
            continue
        fmag = float(rng.uniform(11.0, 13.0))
        _render_star(img, fx, fy, 10.0 ** (-0.4 * (fmag - zero_point)), sigma)
        placed.append((fx, fy))

    # Noise: Poisson on (source+sky) electrons, then Gaussian read noise
    if noise:
        electrons = np.maximum(img * gain, 0.0)
        img = rng.poisson(electrons).astype(np.float64) / gain
        img += rng.normal(0.0, read_noise_e / gain, size=shape)

    if clip_adu is not None:
        np.clip(img, None, float(clip_adu), out=img)

    data = img.astype(np.float32)
    if cube3d:
        # 3-plane one-shot-colour cube whose plane average equals the 2-D image
        data = np.stack([data * 0.9, data * 1.0, data * 1.1], axis=0)

    hdr = header_wcs.to_header()
    # Convert PC/CDELT representation to CD matrix (what plate solvers write
    # and what _ensure_wcs looks for).
    hdr["CD1_1"] = -pixel_scale / 3600.0
    hdr["CD1_2"] = 0.0
    hdr["CD2_1"] = 0.0
    hdr["CD2_2"] = pixel_scale / 3600.0
    for k in ("PC1_1", "PC1_2", "PC2_1", "PC2_2", "CDELT1", "CDELT2"):
        if k in hdr:
            del hdr[k]
    hdr["OBJECT"]   = target_name
    hdr["RA"]       = float(t_sky.ra.deg)
    hdr["DEC"]      = float(t_sky.dec.deg)
    if date_obs:
        hdr["DATE-OBS"] = date_obs
    hdr["EXPTIME"]  = float(exptime_s)
    hdr["IMAGETYP"] = imagetyp
    hdr["EGAIN"]    = float(gain)
    hdr["RDNOISE"]  = float(read_noise_e)
    for k, v in (header_extra or {}).items():
        hdr[k] = v

    os.makedirs(out_dir, exist_ok=True)
    fits_path = os.path.join(out_dir, f"{name}.fits")
    fits.PrimaryHDU(data=data, header=hdr).writeto(fits_path, overwrite=True)

    catalog_path = os.path.join(out_dir, f"{name}_catalog.json")
    with open(catalog_path, "w", encoding="utf-8") as fh:
        json.dump(comp_catalog, fh, indent=1)

    return Scene(
        fits_path=fits_path,
        catalog_path=catalog_path,
        target_name=target_name,
        target_mag=target_mag,
        target_ra=float(t_sky.ra.deg),
        target_dec=float(t_sky.dec.deg),
        zero_point=zero_point,
        fwhm_px=fwhm_px,
        pixel_scale=pixel_scale,
        gain=gain,
        read_noise=read_noise_e,
        sky_adu=sky_adu,
        exptime_s=exptime_s,
        date_obs=date_obs,
        comp_catalog=comp_catalog,
        comp_true_flux=comp_true_flux,
        target_xy=(tx, ty),
    )
