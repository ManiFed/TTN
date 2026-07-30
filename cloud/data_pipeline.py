#!/usr/bin/env python3
"""
Data pipeline — everything that happens after a node uploads a measurement.

    ingest_measurement()   — validate, store with capture-time conditions
    cross_validate()       — compare co-temporal measurements across nodes
    light_curve()          — aggregated light curve per target
    submit_pending_batch() — AAVSO Extended Format batch under the network
                             observer code, POSTed to WebObs
    store_raw_image() / prune_raw_images() — short-term FITS retention

Quality policy: only 'good'/'acceptable' measurements that cross-validation
did not flag as outliers go to AAVSO.  Single-node measurements (nothing to
compare against) are submitted after a configurable hold-back window.
"""

import json
import logging
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psycopg2.errors
from werkzeug.utils import secure_filename

from cloud import db, incidents
from src.shared_models import Measurement

logger = logging.getLogger("cloud.data_pipeline")

_WEBOBS_URL = "https://www.aavso.org/apps/webobs/submit/"
_SOFTWARE_ID = "The Telescope Net Cloud v1"

# Two measurements are "co-temporal" for cross-validation within this window
XVAL_WINDOW_DAYS = 0.03           # ≈ 43 minutes
XVAL_OUTLIER_MAG = 0.30           # flag if > this from the co-temporal median
                                  # and > 3× combined uncertainty


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Ingest ─────────────────────────────────────────────────────────────────────

def ingest_measurement(node_id: str, payload: dict,
                       conditions: Optional[dict] = None) -> dict:
    """
    Validate and store one uploaded measurement.
    Returns {"ok": True, "id": ...} or {"ok": False, "error": ...}.
    Duplicate uploads (same node/target/bjd/filter) are acknowledged idempotently.
    """
    m = Measurement.from_dict(payload)
    m.node_id = node_id
    if not m.is_valid():
        incidents.log(
            node_id,
            "measurement_validation_failed",
            severity="warning",
            target_name=m.target_name,
            detail={"reason": "bounds", "payload": payload},
        )
        return {"ok": False, "error": "measurement failed validation bounds"}

    try:
        row_id = db.execute(
            """INSERT INTO measurements
                   (node_id, target_name, bjd, hjd, magnitude, uncertainty, filter,
                    airmass, fwhm, snr, comparison_stars, quality_flag,
                    zero_point, zp_scatter, fits_file, sky_mag, conditions, received_at,
                    item_id, bundle_id, response_fingerprint, instrumental_magnitude)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (m.node_id, m.target_name, m.bjd, m.hjd, m.magnitude, m.uncertainty,
             m.filter, m.airmass, m.fwhm, m.snr, m.comparison_stars,
             m.quality_flag, m.zero_point, m.zp_scatter, m.fits_file,
             m.sky_mag, json.dumps(conditions or {}), _now(), m.item_id,
             m.bundle_id, m.response_fingerprint, m.instrumental_magnitude),
            returning_id=True,
        )
    except Exception as exc:
        if isinstance(exc, psycopg2.errors.UniqueViolation):
            logger.info("Duplicate measurement ignored: %s %s bjd=%.5f",
                        node_id, m.target_name, m.bjd)
            return {"ok": True, "id": None, "duplicate": True}
        logger.error("Measurement insert failed: %s", exc)
        incidents.log(
            node_id,
            "measurement_storage_failed",
            severity="error",
            target_name=m.target_name,
            detail={"error": str(exc)},
        )
        return {"ok": False, "error": "storage error"}

    logger.info("Measurement stored: %s %s mag=%.3f±%.3f quality=%s",
                node_id, m.target_name, m.magnitude, m.uncertainty, m.quality_flag)

    patrol_alerts = (payload or {}).get("patrol_alerts") or []
    if patrol_alerts and row_id:
        store_patrol_alerts(row_id, node_id, m.target_name, m.bjd, patrol_alerts)

    if m.quality_flag == "poor" or m.uncertainty > 0.3 or m.comparison_stars < 3:
        incidents.log(
            node_id,
            "borderline_photometry",
            severity="warning",
            target_name=m.target_name,
            measurement_id=row_id,
            detail={
                "quality_flag": m.quality_flag,
                "uncertainty": m.uncertainty,
                "comparison_stars": m.comparison_stars,
                "snr": m.snr,
            },
        )
    cross_validate(m.target_name, m.bjd, m.filter)
    if row_id and m.response_fingerprint:
        try:
            from cloud import calibration
            calibration.apply_to_measurement(row_id, payload, _active_config())
        except Exception as exc:
            logger.warning("Network calibration skipped for measurement %s: %s",
                           row_id, exc)
    if m.quality_flag in ("good", "acceptable") and row_id:
        _maybe_create_highlights(row_id, node_id, m)
    return {"ok": True, "id": row_id}


def _active_config() -> dict:
    """Read the Flask app config lazily without creating an import cycle."""
    try:
        from cloud import server
        return getattr(server, "_config", {}) or {}
    except Exception:
        return {}


_HIGHLIGHT_HEADLINES = {
    "SN":       "Your telescope caught a supernova!",
    "TDE":      "Your telescope observed a tidal disruption event!",
    "GRB":      "Your telescope captured a gamma-ray burst!",
    "NOVA":     "Your telescope detected a stellar nova outburst!",
    "CV":       "Your telescope caught a cataclysmic variable in outburst!",
    "EXOPLANET":"Your telescope observed a planetary transit!",
}


def _maybe_create_highlights(measurement_id: int, node_id: str,
                              m: "Measurement") -> None:
    """Create member_highlights records when a time_critical target is observed."""
    target = db.query_one(
        "SELECT target_type, time_critical FROM targets WHERE name = %s",
        (m.target_name,),
    )
    if not target or not target.get("time_critical"):
        return

    owners = db.query(
        "SELECT user_id FROM node_members WHERE node_id = %s", (node_id,)
    )
    if not owners:
        return

    ttype   = (target.get("target_type") or "unknown").upper()
    headline = _HIGHLIGHT_HEADLINES.get(ttype, "Notable observation by your telescope")
    detail   = (
        f"{m.target_name} — {ttype} at mag {m.magnitude:.3f}±{m.uncertainty:.3f} "
        f"(BJD {m.bjd:.4f})"
    )
    now = _now()
    for owner in owners:
        try:
            db.execute(
                """INSERT INTO member_highlights
                       (user_id, node_id, measurement_id, target_name, target_type,
                        bjd, magnitude, headline, detail, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (owner["user_id"], node_id, measurement_id, m.target_name, ttype,
                 m.bjd, m.magnitude, headline, detail, now),
            )
            logger.info("Highlight created for %s: %s", owner["user_id"], headline)
        except Exception as exc:
            logger.warning("Could not create highlight: %s", exc)


# ── Cross-validation ───────────────────────────────────────────────────────────

def cross_validate(target_name: str, bjd: float,
                   filter_name: Optional[str] = None) -> None:
    """
    Compare all measurements of a target within the co-temporal window around
    `bjd`, across nodes.  Marks each as consistent / outlier / single.

    Only measurements in the *same filter* are compared: a B-band point can
    legitimately sit several tenths of a magnitude from the V-band median
    purely from stellar colour, and must not be flagged as an outlier for it.
    """
    if filter_name:
        rows = db.query(
            """SELECT id, node_id, magnitude, uncertainty FROM measurements
               WHERE target_name = %s AND bjd BETWEEN %s AND %s AND filter = %s""",
            (target_name, bjd - XVAL_WINDOW_DAYS, bjd + XVAL_WINDOW_DAYS,
             filter_name),
        )
    else:
        rows = db.query(
            """SELECT id, node_id, magnitude, uncertainty FROM measurements
               WHERE target_name = %s AND bjd BETWEEN %s AND %s""",
            (target_name, bjd - XVAL_WINDOW_DAYS, bjd + XVAL_WINDOW_DAYS),
        )
    if len(rows) < 2:
        for r in rows:
            db.execute("UPDATE measurements SET validation_status='single' WHERE id=%s",
                       (r["id"],))
        return

    mags = [r["magnitude"] for r in rows]
    median = statistics.median(mags)
    for r in rows:
        dev = abs(r["magnitude"] - median)
        sigma = max(0.02, r["uncertainty"])
        status = ("outlier"
                  if dev > XVAL_OUTLIER_MAG and dev > 3.0 * sigma
                  else "consistent")
        db.execute("UPDATE measurements SET validation_status=%s WHERE id=%s",
                   (status, r["id"]))
        if status == "outlier":
            logger.warning("Cross-validation outlier: %s on %s — %.3f vs median %.3f",
                           r["node_id"], target_name, r["magnitude"], median)
            incidents.log(
                r["node_id"],
                "cross_validation_outlier",
                severity="warning",
                target_name=target_name,
                measurement_id=r["id"],
                detail={
                    "magnitude": r["magnitude"],
                    "median": median,
                    "deviation": dev,
                    "uncertainty": r["uncertainty"],
                },
            )


# ── Light curves ───────────────────────────────────────────────────────────────

def light_curve(target_name: str, days: float = 365.0) -> list:
    """All non-outlier measurements of a target, time-ordered, for the API."""
    rows = db.query(
        """SELECT node_id, bjd, magnitude, uncertainty, filter, airmass, snr,
                  quality_flag, validation_status, aavso_submitted, received_at,
                  network_magnitude, network_uncertainty, calibration_state,
                  calibration_model_version, magnitude_system
           FROM measurements
           WHERE target_name = %s AND validation_status != 'outlier'
           ORDER BY bjd""",
        (target_name,),
    )
    if days and rows:
        latest = max(r["bjd"] for r in rows)
        rows = [r for r in rows if latest - r["bjd"] <= days]
    for row in rows:
        applied = row.get("calibration_state") == "applied" and row.get("network_magnitude") is not None
        row["effective_magnitude"] = (row["network_magnitude"] if applied else row["magnitude"])
        row["effective_uncertainty"] = (row.get("network_uncertainty")
                                        if applied and row.get("network_uncertainty") is not None
                                        else row["uncertainty"])
    return rows


def compute_consensus(target_name: str, bjd_center: float,
                      filter_name: Optional[str] = None) -> Optional[dict]:
    """
    Inverse-variance-weighted consensus of all consistent co-temporal measurements.

    Returns a dict with bjd, magnitude, uncertainty, n_nodes, node_ids when 2+
    consistent measurements exist in the cross-validation window; else None.
    Pass filter_name to keep the average single-band — averaging magnitudes
    across different filters produces a value in no physical system.
    """
    sql = """SELECT node_id, bjd, magnitude, uncertainty FROM measurements
           WHERE target_name = %s
             AND bjd BETWEEN %s AND %s
             AND validation_status = 'consistent'
             AND quality_flag IN ('good', 'acceptable')"""
    params: tuple = (target_name,
                     bjd_center - XVAL_WINDOW_DAYS,
                     bjd_center + XVAL_WINDOW_DAYS)
    if filter_name:
        sql += " AND filter = %s"
        params = params + (filter_name,)
    rows = db.query(sql, params)
    if len(rows) < 2:
        return None

    weights  = [1.0 / max(r["uncertainty"] ** 2, 1e-6) for r in rows]
    w_total  = sum(weights)
    mag_mean = sum(w * r["magnitude"] for w, r in zip(weights, rows)) / w_total
    bjd_mean = sum(w * r["bjd"] for w, r in zip(weights, rows)) / w_total

    formal_unc  = 1.0 / math.sqrt(w_total)
    scatter     = statistics.stdev([r["magnitude"] for r in rows])
    uncertainty = max(formal_unc, scatter / math.sqrt(len(rows)))

    node_ids = sorted({r["node_id"] for r in rows})
    return {
        "bjd":         round(bjd_mean, 6),
        "magnitude":   round(mag_mean, 4),
        "uncertainty": round(uncertainty, 4),
        "n_nodes":     len(node_ids),
        "node_ids":    node_ids,
    }


def consensus_light_curve(target_name: str, days: float = 365.0) -> list:
    """
    Time-ordered list of consensus points for epochs where 2+ consistent
    measurements exist in the same co-temporal window (~43 min).
    """
    rows = db.query(
        """SELECT bjd, filter FROM measurements
           WHERE target_name = %s AND validation_status = 'consistent'
             AND quality_flag IN ('good', 'acceptable')
           ORDER BY filter, bjd""",
        (target_name,),
    )
    if not rows:
        return []
    if days:
        latest = max(r["bjd"] for r in rows)
        rows = [r for r in rows if latest - r["bjd"] <= days]

    # Cluster per filter so a consensus point never averages different bands.
    clusters: list[tuple[float, Optional[str]]] = []
    for r in rows:
        filt = r.get("filter")
        if (not clusters or clusters[-1][1] != filt
                or r["bjd"] - clusters[-1][0] > XVAL_WINDOW_DAYS):
            clusters.append((r["bjd"], filt))

    points = []
    for c_bjd, c_filt in clusters:
        p = compute_consensus(target_name, c_bjd, c_filt)
        if p:
            p["filter"] = c_filt
            points.append(p)
    points.sort(key=lambda p: p["bjd"])
    return points


# ── Patrol alert storage ───────────────────────────────────────────────────────

def store_patrol_alerts(measurement_id: int, node_id: str,
                        target_name: str, bjd: float,
                        alerts: list) -> None:
    """Persist transient patrol detections linked to an ingested measurement."""
    now = _now()
    for a in alerts:
        try:
            db.execute(
                """INSERT INTO patrol_detections
                       (measurement_id, node_id, target_name, bjd,
                        ra_deg, dec_deg, est_mag, catalog_mag, delta_mag,
                        alert_type, detected_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (measurement_id, node_id, target_name, bjd,
                 a.get("ra_deg"), a.get("dec_deg"),
                 a.get("est_mag"), a.get("catalog_mag"), a.get("delta_mag"),
                 a.get("alert_type", "unknown"), now),
            )
            logger.info(
                "Patrol alert stored: %s at (%.4f, %.4f) type=%s",
                target_name, a.get("ra_deg", 0), a.get("dec_deg", 0),
                a.get("alert_type"),
            )
        except Exception as exc:
            logger.warning("Could not store patrol alert: %s", exc)


# ── AAVSO batch submission ─────────────────────────────────────────────────────

def submit_pending_batch(config: dict) -> dict:
    """
    Collect every quality-filtered, validated, not-yet-submitted measurement,
    format them as one AAVSO Extended Format file under the network observer
    code, and POST to WebObs.

    Single-node measurements are held back for `single_node_holdback_hours`
    (default 6) to give other nodes a chance to confirm them first.
    """
    aavso_cfg = config.get("aavso", {})
    observer_code = (aavso_cfg.get("observer_code") or "").upper().strip()
    if not observer_code:
        return {"status": "skipped", "message": "aavso.observer_code not configured"}

    holdback_h = float(aavso_cfg.get("single_node_holdback_hours", 6.0))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=holdback_h)).isoformat()

    rows = db.query(
        """SELECT m.*, t.ra_deg AS target_ra_deg, t.dec_deg AS target_dec_deg
             FROM measurements m
             LEFT JOIN targets t ON t.name = m.target_name
            WHERE m.aavso_submitted = 0
              AND m.quality_flag IN ('good', 'acceptable')
              AND (m.validation_status = 'consistent'
                   OR (m.validation_status = 'single' AND m.received_at < %s))
            ORDER BY m.target_name, m.bjd LIMIT 500""",
        (cutoff,),
    )
    if not rows:
        return {"status": "empty", "message": "no pending measurements"}

    rows, undatable = _partition_by_reportable_date(rows)
    if undatable:
        logger.warning(
            "%d measurement(s) held back: no HJD and no target coordinates to "
            "convert one from", len(undatable))
    if not rows:
        return {"status": "empty",
                "message": "no pending measurements with a reportable date"}

    text = _format_batch(rows, observer_code, aavso_cfg)

    audit_dir = Path(aavso_cfg.get("audit_dir", "cloud_data/aavso_batches"))
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    file_path = audit_dir / f"batch_{stamp}.txt"
    file_path.write_text(text, encoding="utf-8")

    if aavso_cfg.get("dry_run", True):
        status, accepted, rejected, message = "dry_run", 0, 0, "dry_run: saved, not POSTed"
    else:
        status, accepted, rejected, message = _post_batch(
            text, aavso_cfg.get("username", ""), aavso_cfg.get("password", ""),
            aavso_cfg.get("submit_url", _WEBOBS_URL))

    if status in ("accepted", "dry_run"):
        db.executemany("UPDATE measurements SET aavso_submitted = 1 WHERE id = %s",
                       [(r["id"],) for r in rows])

    db.execute(
        """INSERT INTO aavso_batches
               (submitted_at, file_path, file_text, n_obs, status, accepted, rejected, message)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (_now(), str(file_path), text, len(rows), status, accepted, rejected, message),
    )
    logger.info("AAVSO batch: %d obs, status=%s (%s)", len(rows), status, message)
    return {"status": status, "n_obs": len(rows), "file_path": str(file_path),
            "accepted": accepted, "rejected": rejected, "message": message}


def _partition_by_reportable_date(rows: list) -> tuple:
    """Split rows into (reportable, undatable), filling in `_hjd` on the first.

    The Extended Format's #DATE= accepts JD, HJD or EXCEL — not the BJD_TDB the
    pipeline records. Nodes now upload an HJD with every measurement; anything
    older is converted here from its BJD using the target's coordinates. A row
    with neither is held back rather than submitted with a timestamp that is
    ~68 s wrong, and stays unsubmitted so it can be recovered later.
    """
    reportable, undatable = [], []
    for r in rows:
        hjd = r.get("hjd")
        if not hjd:
            ra, dec = r.get("target_ra_deg"), r.get("target_dec_deg")
            if r.get("bjd") and ra is not None and dec is not None:
                try:
                    from src.timescales import hjd_utc_from_bjd_tdb
                    hjd = hjd_utc_from_bjd_tdb(float(r["bjd"]), float(ra), float(dec))
                except Exception as exc:
                    logger.warning("HJD conversion failed for %s bjd=%s: %s",
                                   r.get("target_name"), r.get("bjd"), exc)
                    hjd = None
        if hjd:
            r["_hjd"] = float(hjd)
            reportable.append(r)
        else:
            undatable.append(r)
    return reportable, undatable


def _format_batch(rows: list, observer_code: str, aavso_cfg: dict) -> str:
    """AAVSO Extended File Format document for many observations.
    Mirrors aavso_submission._format_extended on the node, plus per-row node id.

    Rows must already carry `_hjd` (see _partition_by_reportable_date)."""
    chart_id = aavso_cfg.get("chart_id", "na") or "na"
    lines = [
        "#TYPE=Extended",
        f"#OBSCODE={observer_code}",
        f"#SOFTWARE={_SOFTWARE_ID}",
        "#DELIM=,",
        "#DATE=HJD",
        "#OBSTYPE=CCD",
        "#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES",
    ]
    for r in rows:
        name = str(r["target_name"]).replace(",", " ")
        amass = f"{r['airmass']:.2f}" if r["airmass"] is not None else "na"
        applied = (r.get("calibration_state") == "applied"
                   and r.get("network_magnitude") is not None)
        magnitude = r["network_magnitude"] if applied else r["magnitude"]
        uncertainty = (r.get("network_uncertainty")
                       if applied and r.get("network_uncertainty") is not None
                       else r["uncertainty"])
        notes = "|".join([
            f"node={r['node_id']}",
            f"snr={r['snr'] if r['snr'] is not None else 'na'}",
            f"comp={r['comparison_stars']}",
            f"zp_scatter={r['zp_scatter'] if r['zp_scatter'] is not None else 'na'}",
            f"xval={r['validation_status']}",
            f"quality={r['quality_flag']}",
            f"cal={r.get('calibration_model_version') or 'raw'}",
            # DATE is HJD_UTC because the format demands it; the barycentric
            # timestamp the science actually uses rides along in the notes.
            f"bjd_tdb={r['bjd']:.6f}",
        ])
        lines.append(",".join([
            name, f"{r['_hjd']:.6f}",
            f"{magnitude:.3f}", f"{uncertainty:.3f}",
            r["filter"] or "CV",
            "NO", "DIFF",
            "ENSEMBLE", "na", "na", "na",
            amass, "na", chart_id, notes,
        ]))
    return "\n".join(lines) + "\n"


def _post_batch(text: str, username: str, password: str, url: str) -> tuple:
    """POST a batch to WebObs. Returns (status, accepted, rejected, message)."""
    if not username or not password:
        return "skipped", 0, 0, "aavso credentials not configured"
    try:
        import requests
        resp = requests.post(url, data={
            "ftype": "EXTENDED", "fdata": text,
            "login": username, "password": password,
        }, timeout=60)
    except Exception as exc:
        logger.error("WebObs batch POST failed: %s", exc)
        return "error", 0, 0, f"POST failed: {exc}"

    if resp.status_code != 200:
        return "error", 0, 0, f"HTTP {resp.status_code}"

    # A success is only recognised from the explicit "N observation(s)" token.
    # An HTTP 200 with no such token is NOT assumed to be a success: the
    # apps.aavso.org stack (Auth0 + AWS WAF) returns 200 challenge/login pages
    # that contain none of the error keywords, and counting those as accepted
    # would mark the batch submitted (aavso_submitted=1) while it never reached
    # AAVSO.  Mirrors aavso_submission._parse_webobs_response on the node.
    m = re.search(r"(\d+)\s+observation", resp.text, re.IGNORECASE)
    accepted = int(m.group(1)) if m else 0
    has_error = bool(re.search(r"\b(error|reject|invalid|fail)\b",
                               resp.text, re.IGNORECASE))
    if accepted > 0:
        return "accepted", accepted, 0, f"accepted={accepted}"
    if has_error:
        return "rejected", 0, 1, "WebObs reported errors"
    logger.warning("Unrecognised WebObs response — treating as error. Raw: %.300s", resp.text)
    return "error", 0, 0, "unrecognised WebObs response (no success token)"


# ── Raw image storage ──────────────────────────────────────────────────────────

def store_raw_image(node_id: str, filename: str, data: bytes,
                    config: dict) -> Optional[str]:
    """Save an uploaded FITS under cloud_data/raw_images/<node>/<date>/.
    Returns the stored path, or None on failure / oversize."""
    max_mb = float(config.get("storage", {}).get("max_image_mb", 64))
    if len(data) > max_mb * 1024 * 1024:
        logger.warning("Raw image from %s rejected — %.1f MB exceeds limit",
                       node_id, len(data) / 1e6)
        incidents.log(
            node_id,
            "raw_image_rejected",
            severity="warning",
            detail={"filename": filename, "size_mb": len(data) / 1e6, "max_mb": max_mb},
        )
        return None
    safe = secure_filename(filename) or "image.fits"
    safe_node_id = secure_filename(node_id)
    if not safe_node_id:
        logger.warning("Rejected raw image with invalid node identifier")
        return None
    root = Path(config.get("storage", {}).get("raw_image_dir", "cloud_data/raw_images"))
    day_dir = root / safe_node_id / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / safe
        path.write_bytes(data)
        return str(path)
    except OSError as exc:
        logger.error("Could not store raw image: %s", exc)
        incidents.log(
            node_id,
            "raw_image_storage_failed",
            severity="error",
            detail={"filename": filename, "error": str(exc)},
        )
        return None


def prune_raw_images(config: dict) -> int:
    """Delete raw images older than the retention window. Returns files removed."""
    storage = config.get("storage", {})
    root = Path(storage.get("raw_image_dir", "cloud_data/raw_images"))
    days = float(storage.get("raw_image_retention_days", 14))
    if not root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for f in root.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Pruned %d raw images older than %.0f days", removed, days)
    return removed
