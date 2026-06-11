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
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cloud import db
from shared_models import Measurement

logger = logging.getLogger("cloud.data_pipeline")

_WEBOBS_URL = "https://www.aavso.org/apps/webobs/submit/"
_SOFTWARE_ID = "Boundless Skies Cloud v1"

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
        return {"ok": False, "error": "measurement failed validation bounds"}

    try:
        row_id = db.execute(
            """INSERT INTO measurements
                   (node_id, target_name, bjd, magnitude, uncertainty, filter,
                    airmass, fwhm, snr, comparison_stars, quality_flag,
                    zero_point, zp_scatter, fits_file, conditions, received_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (m.node_id, m.target_name, m.bjd, m.magnitude, m.uncertainty,
             m.filter, m.airmass, m.fwhm, m.snr, m.comparison_stars,
             m.quality_flag, m.zero_point, m.zp_scatter, m.fits_file,
             json.dumps(conditions or {}), _now()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            logger.info("Duplicate measurement ignored: %s %s bjd=%.5f",
                        node_id, m.target_name, m.bjd)
            return {"ok": True, "id": None, "duplicate": True}
        logger.error("Measurement insert failed: %s", exc)
        return {"ok": False, "error": "storage error"}

    logger.info("Measurement stored: %s %s mag=%.3f±%.3f quality=%s",
                node_id, m.target_name, m.magnitude, m.uncertainty, m.quality_flag)
    cross_validate(m.target_name, m.bjd)
    return {"ok": True, "id": row_id}


# ── Cross-validation ───────────────────────────────────────────────────────────

def cross_validate(target_name: str, bjd: float) -> None:
    """
    Compare all measurements of a target within the co-temporal window around
    `bjd`, across nodes.  Marks each as consistent / outlier / single.
    """
    rows = db.query(
        """SELECT id, node_id, magnitude, uncertainty FROM measurements
           WHERE target_name = ? AND bjd BETWEEN ? AND ?""",
        (target_name, bjd - XVAL_WINDOW_DAYS, bjd + XVAL_WINDOW_DAYS),
    )
    if len(rows) < 2:
        for r in rows:
            db.execute("UPDATE measurements SET validation_status='single' WHERE id=?",
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
        db.execute("UPDATE measurements SET validation_status=? WHERE id=?",
                   (status, r["id"]))
        if status == "outlier":
            logger.warning("Cross-validation outlier: %s on %s — %.3f vs median %.3f",
                           r["node_id"], target_name, r["magnitude"], median)


# ── Light curves ───────────────────────────────────────────────────────────────

def light_curve(target_name: str, days: float = 365.0) -> list:
    """All non-outlier measurements of a target, time-ordered, for the API."""
    rows = db.query(
        """SELECT node_id, bjd, magnitude, uncertainty, filter, airmass, snr,
                  quality_flag, validation_status, received_at
           FROM measurements
           WHERE target_name = ? AND validation_status != 'outlier'
           ORDER BY bjd""",
        (target_name,),
    )
    if days and rows:
        latest = max(r["bjd"] for r in rows)
        rows = [r for r in rows if latest - r["bjd"] <= days]
    return rows


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
        """SELECT * FROM measurements
           WHERE aavso_submitted = 0
             AND quality_flag IN ('good', 'acceptable')
             AND (validation_status = 'consistent'
                  OR (validation_status = 'single' AND received_at < ?))
           ORDER BY target_name, bjd LIMIT 500""",
        (cutoff,),
    )
    if not rows:
        return {"status": "empty", "message": "no pending measurements"}

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
        db.executemany("UPDATE measurements SET aavso_submitted = 1 WHERE id = ?",
                       [(r["id"],) for r in rows])

    db.execute(
        """INSERT INTO aavso_batches
               (submitted_at, file_path, n_obs, status, accepted, rejected, message)
           VALUES (?,?,?,?,?,?,?)""",
        (_now(), str(file_path), len(rows), status, accepted, rejected, message),
    )
    logger.info("AAVSO batch: %d obs, status=%s (%s)", len(rows), status, message)
    return {"status": status, "n_obs": len(rows), "file_path": str(file_path),
            "accepted": accepted, "rejected": rejected, "message": message}


def _format_batch(rows: list, observer_code: str, aavso_cfg: dict) -> str:
    """AAVSO Extended File Format document for many observations.
    Mirrors aavso_submission._format_extended on the node, plus per-row node id."""
    chart_id = aavso_cfg.get("chart_id", "na") or "na"
    lines = [
        "#TYPE=Extended",
        f"#OBSCODE={observer_code}",
        f"#SOFTWARE={_SOFTWARE_ID}",
        "#DELIM=,",
        "#DATE=BJD",
        "#OBSTYPE=CCD",
        "#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES",
    ]
    for r in rows:
        name = str(r["target_name"]).replace(",", " ")
        amass = f"{r['airmass']:.2f}" if r["airmass"] is not None else "na"
        notes = "|".join([
            f"node={r['node_id']}",
            f"snr={r['snr'] if r['snr'] is not None else 'na'}",
            f"comp={r['comparison_stars']}",
            f"zp_scatter={r['zp_scatter'] if r['zp_scatter'] is not None else 'na'}",
            f"xval={r['validation_status']}",
            f"quality={r['quality_flag']}",
        ])
        lines.append(",".join([
            name, f"{r['bjd']:.6f}",
            f"{r['magnitude']:.3f}", f"{r['uncertainty']:.3f}",
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

    m = re.search(r"(\d+)\s+observation", resp.text, re.IGNORECASE)
    accepted = int(m.group(1)) if m else 0
    has_error = bool(re.search(r"\b(error|reject|invalid|fail)\b",
                               resp.text, re.IGNORECASE))
    if has_error and accepted == 0:
        return "rejected", 0, 1, "WebObs reported errors"
    if accepted == 0:
        accepted = 1   # HTTP 200, no error keywords — assume accepted
    return "accepted", accepted, 0, f"accepted={accepted}"


# ── Raw image storage ──────────────────────────────────────────────────────────

def store_raw_image(node_id: str, filename: str, data: bytes,
                    config: dict) -> Optional[str]:
    """Save an uploaded FITS under cloud_data/raw_images/<node>/<date>/.
    Returns the stored path, or None on failure / oversize."""
    max_mb = float(config.get("storage", {}).get("max_image_mb", 64))
    if len(data) > max_mb * 1024 * 1024:
        logger.warning("Raw image from %s rejected — %.1f MB exceeds limit",
                       node_id, len(data) / 1e6)
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name) or "image.fits"
    root = Path(config.get("storage", {}).get("raw_image_dir", "cloud_data/raw_images"))
    day_dir = root / node_id / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / safe
        path.write_bytes(data)
        return str(path)
    except OSError as exc:
        logger.error("Could not store raw image: %s", exc)
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
