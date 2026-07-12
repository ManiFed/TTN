#!/usr/bin/env python3
"""
Shared Astrometry.net `solve-field` wrapper.

The node's photometry pipeline already shells out to solve-field for its own
frames (src/photometry.py). Universal frame ingestion needs the *same* capability
cloud-side, where contributed images arrive with no WCS at all — including
blind solves (no RA/Dec hint) and reporting back the solved field centre and
pixel scale so arbitrary optics self-describe.

`solve()` returns a dict on success (and writes the WCS into the file in place):

    {"ra_deg": ..., "dec_deg": ..., "pixel_scale_arcsec": ...,
     "radius_deg": ..., "wcs_written": True}

or None on failure. It is deliberately dependency-light (subprocess + astropy)
so it runs identically on a node and on a cloud solver worker.
"""

import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger("plate_solve")


def solve(fits_path: str,
          hint_ra: float | None = None,
          hint_dec: float | None = None,
          hint_scale: float | None = None,
          solve_field_path: str = "solve-field",
          search_radius: float = 15.0,
          timeout_s: float = 180.0,
          downsample: int = 2,
          scratch_dir: str = "/tmp") -> dict | None:
    """Plate-solve `fits_path`, writing the WCS back in place.

    hint_ra/hint_dec — optional pointing hint; when both are given the solve is
    constrained to `search_radius` degrees around it (fast). Omitting them does
    a blind solve (slower, but the only option for unknown contributed frames).
    hint_scale — optional arcsec/px hint; brackets the scale search ±20 %.
    """
    base = os.path.splitext(os.path.basename(fits_path))[0]
    workdir = tempfile.mkdtemp(prefix="bs_solve_", dir=scratch_dir)
    solve_input = os.path.join(workdir, base + ".fits")
    try:
        shutil.copy2(fits_path, solve_input)
    except OSError as exc:
        logger.error("plate_solve: cannot stage input: %s", exc)
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    cmd = [
        solve_field_path, solve_input,
        "--overwrite", "--no-plots", "--no-verify",
        "--dir", workdir,
        "--new-fits", "none",
        "--corr", "none", "--rdls", "none", "--match", "none",
        "--solved", "none", "--index-xyls", "none",
        "--cpulimit", str(int(timeout_s)),
    ]
    if downsample and downsample > 1:
        cmd += ["--downsample", str(int(downsample))]
    if hint_ra is not None and hint_dec is not None:
        cmd += ["--ra", f"{float(hint_ra):.6f}", "--dec", f"{float(hint_dec):.6f}",
                "--radius", f"{float(search_radius):.3f}"]
    if hint_scale:
        ps = float(hint_scale)
        cmd += ["--scale-units", "arcsecperpix",
                "--scale-low", f"{ps * 0.8:.4f}", "--scale-high", f"{ps * 1.2:.4f}"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout_s + 30)
    except FileNotFoundError:
        logger.error("plate_solve: solve-field not found at '%s'", solve_field_path)
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    except subprocess.TimeoutExpired:
        logger.warning("plate_solve: timed out after %.0fs", timeout_s)
        shutil.rmtree(workdir, ignore_errors=True)
        return None
    except Exception as exc:
        logger.error("plate_solve: error: %s", exc)
        shutil.rmtree(workdir, ignore_errors=True)
        return None

    wcs_path = os.path.join(workdir, base + ".wcs")
    try:
        if result.returncode != 0 or not os.path.exists(wcs_path):
            logger.info("plate_solve: no solution (rc=%d): %s",
                        result.returncode,
                        (result.stderr or result.stdout or "")[:200])
            return None
        info = _read_solution(wcs_path)
        if not _write_wcs(fits_path, wcs_path):
            logger.error("plate_solve: solved but WCS write-back failed")
            return None
        info["wcs_written"] = True
        logger.info("plate_solve: solved %s → (%.4f, %.4f) %.3f\"/px",
                    os.path.basename(fits_path), info.get("ra_deg", 0.0),
                    info.get("dec_deg", 0.0), info.get("pixel_scale_arcsec", 0.0))
        return info
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _read_solution(wcs_path: str) -> dict:
    """Field centre + pixel scale from a solved .wcs header."""
    from astropy.io import fits
    from astropy.wcs import WCS
    try:
        hdr = fits.Header.fromfile(wcs_path, sep="", endcard=True, padding=True)
    except Exception:
        with fits.open(wcs_path) as h:
            hdr = h[0].header
    out: dict = {}
    try:
        w = WCS(hdr)
        nx = int(hdr.get("IMAGEW") or hdr.get("NAXIS1") or 0)
        ny = int(hdr.get("IMAGEH") or hdr.get("NAXIS2") or 0)
        if nx and ny:
            sky = w.pixel_to_world(nx / 2.0, ny / 2.0)
            out["ra_deg"] = float(sky.ra.deg)
            out["dec_deg"] = float(sky.dec.deg)
        try:
            from astropy.wcs.utils import proj_plane_pixel_scales
            import numpy as np
            scales = proj_plane_pixel_scales(w)      # deg/px
            out["pixel_scale_arcsec"] = float(np.mean(scales) * 3600.0)
            if nx:
                out["radius_deg"] = float(np.mean(scales) * max(nx, ny) / 2.0)
        except Exception:
            pass
    except Exception as exc:
        logger.debug("plate_solve: could not parse solution geometry: %s", exc)
    return out


def _write_wcs(fits_path: str, wcs_path: str) -> bool:
    """Delegate WCS injection to photometry's tested implementation."""
    try:
        from src.photometry import _inject_wcs
        return _inject_wcs(fits_path, wcs_path)
    except Exception as exc:
        logger.error("plate_solve: _inject_wcs failed: %s", exc)
        return False
