"""
MPC (Minor Planet Center) astrometry report generation.

Formats a confirmed cloud.moving_objects tracklet into MPC's ADES PSV
(pipe-separated values) format and writes it to disk for an operator to
manually submit via MPC's web upload — the same "generate file, human
uploads" pattern already used for AAVSO (cloud/data_pipeline.py), since
automated submission needs a registered observatory code and submitter
account (config mpc.observatory_code, a manual one-time step) and this
project has already learned that headless submission to astronomy-org web
portals is unreliable (AAVSO WAF blocks).

ADES PSV is used instead of MPC's legacy fixed 80-column format — it's what
MPC now recommends, and it avoids fixed-width column-offset bugs that are a
common source of malformed 80-col submissions.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cloud import db

logger = logging.getLogger("cloud.mpc_report")

_ADES_BAND = {"CV": "C", "C": "C", "CLEAR": "C", "V": "V", "R": "R",
             "B": "B", "I": "I", "G": "G", "GAIA": "G"}


def _cfg(config: dict, key: str, default):
    return (config.get("mpc") or {}).get(key, default)


def _ades_band(filter_name: str) -> str:
    return _ADES_BAND.get(str(filter_name or "").strip().upper(), "C")


def _bjd_to_utc_approx(bjd_tdb: float, ra_deg: float, dec_deg: float) -> str:
    """Approximate inverse of the BJD_TDB barycentric correction — used only
    when a detection has no recorded date_obs_utc. Iterates the same
    Time.light_travel_time correction applied forward in
    src/photometry.py:_compute_bjd_ex (evaluated at the geocenter, like that
    function's no-site-config fallback — the observer-on-Earth term is
    ≤21 ms, negligible here) to recover UTC; the correction varies slowly
    enough over a night that 3 iterations converge to well under a second."""
    from astropy.time import Time
    from astropy.coordinates import EarthLocation, SkyCoord
    import astropy.units as u

    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    geocenter = EarthLocation.from_geocentric(0.0 * u.m, 0.0 * u.m, 0.0 * u.m)
    t_tdb = Time(bjd_tdb, format="jd", scale="tdb")
    guess_utc = t_tdb.utc
    for _ in range(3):
        ltt = guess_utc.light_travel_time(target, kind="barycentric", location=geocenter)
        guess_utc = (t_tdb - ltt).utc
    return guess_utc.isot + "Z"


def _obs_time_iso(det: dict) -> tuple:
    """(iso_utc_string, approximated: bool) for one detection row. Prefers
    the recorded exposure-start UTC (date_obs_utc); only falls back to
    inverting the barycentric-corrected BJD for older rows recorded before
    that column existed."""
    raw = str(det.get("date_obs_utc") or "").strip()
    if raw:
        from astropy.time import Time
        for fmt in ("fits", "isot", "iso"):
            try:
                s = raw[:19] if fmt in ("isot", "iso") else raw
                t = Time(s, format=fmt, scale="utc")
                return t.isot + "Z", False
            except Exception:
                continue
    try:
        return _bjd_to_utc_approx(float(det["bjd"]), float(det["ra_deg"]),
                                  float(det["dec_deg"])), True
    except Exception:
        return "", True


def _format_ades_psv(candidate: dict, detections: list,
                     observatory_code: str, observer_name: str) -> str:
    """MPC ADES PSV: one header line, one row per detection. Column set
    follows MPC's minimal-astrometry submission profile — see
    https://www.minorplanetcenter.net/iau/info/ADES.html"""
    trk_sub = candidate.get("designation") or f"BSMO{candidate['id']:05d}"
    cols = ["permID", "provID", "trkSub", "mode", "stn", "obsTime",
           "ra", "dec", "mag", "band", "photCat", "notes", "remarks"]
    lines = ["|".join(cols)]
    remark_bits = [f"observer={observer_name}"] if observer_name else []
    if candidate.get("object_type") == "comet_candidate":
        # Heuristic flag only (src/photometry.py sharpness-based coma signal,
        # not a confirmed coma/tail model) — worth an operator's attention
        # before MPC submission, since comets use different designation
        # conventions than minor planets.
        remark_bits.append("possible_comet")
    remarks = ";".join(remark_bits)

    for d in sorted(detections, key=lambda x: x["bjd"]):
        obs_time, approx = _obs_time_iso(d)
        notes = "approx_time" if approx else ""
        row = [
            "",                                  # permID — unassigned
            "",                                  # provID — unassigned
            trk_sub,
            "CCD",
            observatory_code,
            obs_time,
            f"{float(d['ra_deg']):.6f}",
            f"{float(d['dec_deg']):+.6f}",
            f"{float(d['mag']):.2f}" if d.get("mag") is not None else "",
            _ades_band(d.get("filter")),
            "",
            notes,
            remarks,
        ]
        lines.append("|".join(row))
    return "\n".join(lines) + "\n"


def generate_report(cand_id: int, config: dict) -> Optional[dict]:
    """Write a confirmed candidate's ADES PSV report to disk and record it
    in mpc_reports. Returns {"file_path", "n_observations"}, or None when
    reporting is disabled, the candidate/detections can't be found, or no
    observatory code is configured yet."""
    if not bool(_cfg(config, "enabled", False)):
        logger.info("MPC reporting disabled (mpc.enabled=false) — "
                    "skipping report for candidate #%s", cand_id)
        return None

    cand = db.query_one("SELECT * FROM asteroid_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None:
        return None

    detail = db.loads(cand.get("detail"), {})
    detection_ids = detail.get("detection_ids") or []
    if detection_ids:
        detections = db.query(
            "SELECT * FROM moving_object_detections WHERE id = ANY(%s)",
            (detection_ids,))
    else:
        detections = db.query(
            "SELECT * FROM moving_object_detections WHERE tracklet_id = %s",
            (cand_id,))
    if not detections:
        logger.warning("No detections found for candidate #%s — "
                       "skipping MPC report", cand_id)
        return None

    observatory_code = str(_cfg(config, "observatory_code", "") or "")
    observer_name = str(_cfg(config, "observer_name", "") or "")
    if not observatory_code:
        logger.info("MPC report for candidate #%s skipped: no "
                    "observatory_code configured", cand_id)
        return None

    text = _format_ades_psv(cand, detections, observatory_code, observer_name)

    report_dir = Path(str(_cfg(config, "report_dir", "cloud_data/mpc_reports")))
    date_dir = report_dir / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    designation = cand.get("designation") or f"candidate_{cand_id}"
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in designation)
    dest = date_dir / f"{safe_name}.psv"
    dest.write_text(text)

    db.execute(
        "INSERT INTO mpc_reports (candidate_id, file_path, format, "
        "n_observations, created_at) VALUES (%s,%s,%s,%s,%s)",
        (cand_id, str(dest), "ades_psv", len(detections),
         datetime.now(timezone.utc).isoformat()))

    logger.info("MPC report written for candidate #%s: %s (%d observations)",
               cand_id, dest, len(detections))
    return {"file_path": str(dest), "n_observations": len(detections)}
