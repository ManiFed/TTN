#!/usr/bin/env python3
"""
FITS export helper — writes science-ready copies of processed Seestar files.

Public API
----------
    from fits_export import export_enhanced_fits
    path = export_enhanced_fits(source_fits, result, config)

The function copies the source FITS to
    <export_dir>/<YYYY-MM-DD>/<original_basename>
then enriches the copy's primary HDU header with observatory, detector,
and processing metadata drawn from config and the photometry result dict.
Original Seestar headers are never overwritten; new keywords are only set
when they are absent from the source file.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from astropy.io import fits

logger = logging.getLogger("fits_export")


def export_enhanced_fits(
    source_fits: str,
    result: dict,
    config: dict,
    export_dir: Optional[str] = None,
) -> Optional[str]:
    """
    Copy source_fits into export_dir and enrich its headers.

    Returns the path to the exported file, or None on failure.
    """
    phot_cfg = config.get("photometry", {})
    obs_cfg  = config.get("observatory", {})

    if export_dir is None:
        export_dir = phot_cfg.get("fits_export", {}).get("export_dir", "fits_export")

    # Date sub-directory from DATE-OBS in result or source header, fallback to today
    date_str = _date_str_from_result(result, source_fits)
    dest_dir = os.path.join(export_dir, date_str)
    os.makedirs(dest_dir, exist_ok=True)

    dest_path = os.path.join(dest_dir, os.path.basename(source_fits))
    try:
        shutil.copy2(source_fits, dest_path)
    except OSError as exc:
        logger.error("Could not copy %s → %s: %s", source_fits, dest_path, exc)
        return None

    try:
        with fits.open(dest_path, mode="update") as hdul:
            hdr = hdul[0].header

            # ── Observatory block ─────────────────────────────────────────────
            _set_if_absent(hdr, "TELESCOP", obs_cfg.get("telescope", "ZWO Seestar S50"))
            _set_if_absent(hdr, "INSTRUME", obs_cfg.get("instrument", "ZWO Seestar S50 IMX462"))

            observer = (
                obs_cfg.get("observer")
                or config.get("aavso", {}).get("observer_code", "")
            )
            if observer:
                _set_if_absent(hdr, "OBSERVER", observer)

            site_name = obs_cfg.get("name", "")
            if site_name:
                _set_if_absent(hdr, "SITENAME", site_name)

            # Prefer observatory block; fall back to safety.observer block
            safety_obs = config.get("safety", {}).get("observer", {})
            lat = obs_cfg.get("latitude") or safety_obs.get("latitude")
            lon = obs_cfg.get("longitude") or safety_obs.get("longitude")
            elev = obs_cfg.get("elevation", 0.0)

            if lat is not None:
                _set_if_absent(hdr, "SITELAT",  float(lat))
                _set_if_absent(hdr, "OBSLAT",   float(lat))
            if lon is not None:
                _set_if_absent(hdr, "SITELONG", float(lon))
                _set_if_absent(hdr, "OBSLONG",  float(lon))
            _set_if_absent(hdr, "SITEELEV", float(elev))

            # ── Detector block ────────────────────────────────────────────────
            gain       = float(phot_cfg.get("gain", 1.0))
            read_noise = float(phot_cfg.get("read_noise", 5.0))
            _set_if_absent(hdr, "GAIN",    gain)
            _set_if_absent(hdr, "RDNOISE", read_noise)
            _set_if_absent(hdr, "IMAGETYP", "LIGHT")
            _set_if_absent(hdr, "RADESYS",  "ICRS")
            _set_if_absent(hdr, "EQUINOX",  2000.0)

            # ── Science block ─────────────────────────────────────────────────
            if result.get("airmass") is not None:
                _set_if_absent(hdr, "AIRMASS", float(result["airmass"]))
            if result.get("bjd") is not None:
                _set_if_absent(hdr, "BJD-OBS", float(result["bjd"]),
                               comment="Barycentric Julian Date (TCB)")
                _set_if_absent(hdr, "BARY-SYS", "TCB")

            # ── Processing block ──────────────────────────────────────────────
            node_id  = phot_cfg.get("node_id", "node_unknown")
            now_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _set_if_absent(hdr, "SWCREATE", f"Boundless Skies Node {node_id}")
            _set_if_absent(hdr, "DATE-BLD",  now_utc, comment="UTC timestamp of header enrichment")

            # HISTORY is always appended (additive, never overwrites)
            hdr["HISTORY"] = f"Processed by Boundless Skies Node v1 ({node_id})"
            if result.get("magnitude") is not None:
                mag = result["magnitude"]
                err = result.get("uncertainty", 0.0)
                snr = result.get("snr", 0.0)
                qf  = result.get("quality_flag", "")
                hdr["HISTORY"] = (
                    f"Differential photometry: mag={mag:.3f}+/-{err:.3f} "
                    f"snr={snr:.1f} quality={qf}"
                )
            if result.get("bjd") is not None:
                hdr["HISTORY"] = "BJD-OBS computed via astropy barycentric correction (TCB)"
            if result.get("comparison_stars") is not None:
                hdr["HISTORY"] = f"Comparison stars used: {result['comparison_stars']}"
            if result.get("zero_point") is not None:
                zp = result["zero_point"]
                zp_sc = result.get("zp_scatter", 0.0)
                hdr["HISTORY"] = f"Zero point: {zp:.3f}+/-{zp_sc:.3f} mag"

            # Note whether ASTAP was involved (inferred from WCS presence)
            wcs_keys = {"CD1_1", "CDELT1"}
            if any(k in hdr for k in wcs_keys):
                hdr["HISTORY"] = "Astrometric solution: WCS present (Seestar or ASTAP)"

            hdul.flush(output_verify="fix")

        logger.info("Exported enhanced FITS: %s", dest_path)
        return dest_path

    except Exception as exc:
        logger.error("Failed to enrich FITS headers in %s: %s", dest_path, exc)
        return None


# ── Utilities ──────────────────────────────────────────────────────────────────

def _set_if_absent(hdr, keyword: str, value, comment: str = "") -> None:
    if keyword not in hdr:
        if comment:
            hdr[keyword] = (value, comment)
        else:
            hdr[keyword] = value


def _date_str_from_result(result: dict, fits_path: str) -> str:
    """Return 'YYYY-MM-DD' from the result BJD, or by peeking at the FITS header."""
    try:
        if result.get("bjd"):
            from astropy.time import Time
            t = Time(result["bjd"], format="jd", scale="tcb")
            return t.utc.datetime.strftime("%Y-%m-%d")
    except Exception:
        pass

    try:
        with fits.open(fits_path, memmap=False, ignore_missing_simple=True) as hdul:
            date_obs = hdul[0].header.get("DATE-OBS", "")
        if date_obs:
            return date_obs[:10]
    except Exception:
        pass

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
