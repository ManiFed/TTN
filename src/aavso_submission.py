#!/usr/bin/env python3
"""
AAVSO submission module — The Telescope Net Node v1.

Takes a measurement dict from photometry.run_pipeline(), formats it into
AAVSO Extended File Format, POSTs it to the WebObs API, and writes a full
audit trail to disk.

Public API
----------
    from aavso_submission import submit
    result = submit(measurement, config)   # returns dict

Result dict
-----------
    {
        "status":        "accepted" | "rejected" | "skipped" | "dry_run" | "error",
        "accepted":      int,
        "rejected":      int,
        "file_path":     str | None,
        "response_path": str | None,
        "record_path":   str | None,
        "message":       str,
    }

Config keys (config["aavso"])
------------------------------
    observer_code:         AAVSO observer code (OBSCODE), e.g. "MXYZ"  [required]
    username:              AAVSO website login username                  [required to POST]
    password:              AAVSO website login password                  [required to POST]
    audit_dir:             local directory for audit trail (default: "aavso_submissions")
    dry_run:               if true, format and save but do not POST (default: false)
    submit_poor_quality:   if true, submit even when quality_flag=="poor" (default: false)
    chart_id:              VSP chart ID to include in submission (default: "na")
    submit_url:            WebObs endpoint override (default: AAVSO production URL)
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.shared_models import expand_env

logger = logging.getLogger("aavso_submission")

_WEBOBS_URL  = "https://www.aavso.org/apps/webobs/submit/"
_SOFTWARE_ID = "The Telescope Net Node v1"


# ── Public entry point ─────────────────────────────────────────────────────────

def submit(measurement: dict, config: dict) -> dict:
    """
    Format measurement as AAVSO Extended File Format, POST to WebObs, record locally.

    Never raises — returns a result dict with status="error" on failure so the
    caller can continue processing additional observations.
    """
    aavso_cfg     = config.get("aavso", {})
    observer_code = aavso_cfg.get("observer_code", "").upper().strip()
    username      = expand_env(aavso_cfg.get("username", ""))
    password      = expand_env(aavso_cfg.get("password", ""))
    audit_dir     = Path(aavso_cfg.get("audit_dir", "aavso_submissions"))
    dry_run       = bool(aavso_cfg.get("dry_run", False))
    submit_poor   = bool(aavso_cfg.get("submit_poor_quality", False))
    submit_url    = aavso_cfg.get("submit_url", _WEBOBS_URL)

    # When the cloud is enabled it owns AAVSO submission (one network batch under
    # a single OBSCODE), so the node must not also POST — that would send every
    # observation to WebObs twice.  The node still formats and saves the file for
    # the local audit trail and uploads it to the cloud.  submit_from_node
    # defaults to True only for a standalone node with no cloud.
    cloud_enabled    = bool(config.get("cloud", {}).get("enabled", False))
    submit_from_node = bool(aavso_cfg.get("submit_from_node", not cloud_enabled))

    if not observer_code:
        logger.error("aavso.observer_code not set in config — cannot submit")
        return _error_result("observer_code not configured")

    quality = measurement.get("quality_flag", "poor")
    if quality == "poor" and not submit_poor:
        logger.info(
            "Skipping submission: quality=poor "
            "(set aavso.submit_poor_quality: true to override)"
        )
        return {
            "status":        "skipped",
            "accepted":      0,
            "rejected":      0,
            "file_path":     None,
            "response_path": None,
            "record_path":   None,
            "message":       "quality=poor, submission skipped",
        }

    submission_text = _format_extended(measurement, observer_code, aavso_cfg)

    # ── Determine audit file paths ─────────────────────────────────────────────
    target_slug = re.sub(r"[^A-Za-z0-9_-]", "_", measurement.get("target_name", "unknown"))
    bjd_str     = f"{measurement.get('bjd', 0.0):.6f}"
    stem        = f"{target_slug}_{bjd_str}"
    date_dir    = audit_dir / datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        date_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create audit directory %s: %s", date_dir, exc)
        return _error_result(f"audit directory error: {exc}")

    file_path     = date_dir / f"{stem}.txt"
    response_path = date_dir / f"{stem}_response.txt"
    record_path   = date_dir / f"{stem}_record.json"

    try:
        file_path.write_text(submission_text, encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot write submission file %s: %s", file_path, exc)
        return _error_result(f"file write error: {exc}")

    logger.info("Submission file saved: %s", file_path)

    # ── POST or skip ───────────────────────────────────────────────────────────
    if dry_run:
        logger.info("Dry run — skipping POST to WebObs")
        result = _make_result("dry_run", 0, 0, str(file_path), None, "dry_run: file saved, no POST")

    elif not submit_from_node:
        logger.info("Cloud owns AAVSO submission — node saved audit file only, no POST")
        result = _make_result("audit", 0, 0, str(file_path), None,
                              "cloud owns submission; node saved audit file only")

    elif not username or not password:
        logger.warning("aavso.username/password not configured — file saved but not submitted")
        result = _make_result("skipped", 0, 0, str(file_path), None, "credentials not configured")

    else:
        result = _post_to_webobs(
            submission_text, username, password, submit_url,
            str(file_path), str(response_path),
        )

    # ── Write audit record ─────────────────────────────────────────────────────
    record = {
        "submitted_at":  datetime.now(timezone.utc).isoformat(),
        "measurement":   measurement,
        "observer_code": observer_code,
        "dry_run":       dry_run,
        "status":        result["status"],
        "accepted":      result["accepted"],
        "rejected":      result["rejected"],
        "message":       result["message"],
        "file_path":     result["file_path"],
        "response_path": result.get("response_path"),
    }
    try:
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        result["record_path"] = str(record_path)
    except OSError as exc:
        logger.warning("Could not write audit record %s: %s", record_path, exc)
        result["record_path"] = None

    logger.info(
        "Submission complete — status=%s accepted=%d rejected=%d",
        result["status"], result["accepted"], result["rejected"],
    )
    return result


# ── AAVSO Extended File Format ─────────────────────────────────────────────────

def _format_extended(measurement: dict, observer_code: str, aavso_cfg: dict) -> str:
    """
    Build an AAVSO Extended File Format document for a single observation.

    Spec: https://www.aavso.org/aavso-extended-file-format

    Column order: NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES
    """
    filter_name = measurement.get("filter", "CV")
    chart_id    = aavso_cfg.get("chart_id", "na") or "na"

    header = "\n".join([
        "#TYPE=Extended",
        f"#OBSCODE={observer_code}",
        f"#SOFTWARE={_SOFTWARE_ID}",
        "#DELIM=,",
        "#DATE=BJD",
        "#OBSTYPE=CCD",
        "#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,AMASS,GROUP,CHART,NOTES",
    ])

    # Target name must not contain commas — replace with space if present
    name = measurement.get("target_name", "UNKNOWN").replace(",", " ")
    date = f"{measurement.get('bjd', 0.0):.6f}"
    mag  = f"{measurement.get('magnitude', 99.999):.3f}"
    merr = f"{measurement.get('uncertainty', 9.999):.3f}"

    airmass_val = measurement.get("airmass")
    amass = f"{airmass_val:.2f}" if airmass_val is not None else "na"

    # Ensemble differential photometry — no single comparison star to name
    cname = "ENSEMBLE"
    cmag  = "na"
    kname = "na"
    kmag  = "na"

    notes = "|".join([
        f"fwhm={measurement.get('fwhm', 'na')}",
        f"snr={measurement.get('snr', 'na')}",
        f"comp={measurement.get('comparison_stars', 'na')}",
        f"zp_scatter={measurement.get('zp_scatter', 'na')}",
        f"node={measurement.get('node_id', 'na')}",
        f"quality={measurement.get('quality_flag', 'na')}",
        f"fits={measurement.get('fits_file', 'na')}",
    ])

    row = ",".join([
        name, date, mag, merr,
        filter_name,
        "NO",        # TRANS: not transformed to standard system
        "DIFF",      # MTYPE: differential photometry
        cname, cmag,
        kname, kmag,
        amass,
        "na",        # GROUP
        chart_id,
        notes,
    ])

    return header + "\n" + row + "\n"


# ── WebObs POST ────────────────────────────────────────────────────────────────

def _post_to_webobs(
    submission_text: str,
    username: str,
    password: str,
    url: str,
    file_path: str,
    response_path: str,
) -> dict:
    """POST to AAVSO WebObs and return a result dict."""
    try:
        import requests
    except ImportError:
        logger.error("requests library not available — cannot POST to WebObs")
        return _error_result("requests not installed")

    payload = {
        "ftype":    "EXTENDED",
        "fdata":    submission_text,
        "login":    username,
        "password": password,
    }

    logger.info("POSTing observation to AAVSO WebObs: %s", url)
    try:
        resp = requests.post(url, data=payload, timeout=30)
        response_text = resp.text
        http_status   = resp.status_code
    except requests.exceptions.Timeout:
        logger.error("WebObs POST timed out after 30 s")
        return _error_result("POST timed out")
    except requests.exceptions.ConnectionError as exc:
        logger.error("WebObs POST connection error: %s", exc)
        return _error_result(f"connection error: {exc}")
    except Exception as exc:
        logger.error("WebObs POST failed: %s", exc)
        return _error_result(f"POST failed: {exc}")

    # Save raw response regardless of status
    try:
        Path(response_path).write_text(response_text, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save response file: %s", exc)
        response_path = None

    accepted, rejected = _parse_webobs_response(response_text, http_status)

    if http_status != 200:
        status  = "error"
        message = f"HTTP {http_status}"
    elif rejected > 0 and accepted == 0:
        status  = "rejected"
        message = f"rejected={rejected}"
    elif accepted > 0:
        status  = "accepted"
        message = f"accepted={accepted} rejected={rejected}"
    else:
        # Cannot determine outcome from response text
        status  = "error"
        message = f"unrecognised WebObs response (HTTP {http_status})"
        logger.warning("Could not parse WebObs response — treating as error. Raw: %.300s", response_text)

    return _make_result(status, accepted, rejected, file_path, response_path, message)


def _parse_webobs_response(text: str, http_status: int) -> tuple:
    """
    Extract accepted/rejected counts from AAVSO WebObs HTML response.

    AAVSO success text: "Thanks! N observation(s) were uploaded successfully."
    Rejection indicators: words like 'error', 'reject', 'invalid', 'fail'.

    A success is only recognised from the explicit "N observation(s)" token.
    An HTTP 200 with no such token is NOT assumed to be a success: the new
    apps.aavso.org stack (Auth0 + AWS WAF) returns 200 challenge/login pages
    that contain none of the error keywords, and counting those as accepted
    would silently mask blocked submissions.  Unrecognised bodies return
    (0, 0) so the caller classifies them as an error.
    """
    accepted = 0
    rejected = 0

    if http_status == 200:
        m = re.search(r"(\d+)\s+observation", text, re.IGNORECASE)
        if m:
            accepted = int(m.group(1))

    has_error = bool(re.search(r"\b(error|reject|invalid|fail)\b", text, re.IGNORECASE))

    if has_error and accepted == 0:
        rejected = 1

    return accepted, rejected


# ── Result helpers ─────────────────────────────────────────────────────────────

def _make_result(
    status: str,
    accepted: int,
    rejected: int,
    file_path: Optional[str],
    response_path: Optional[str],
    message: str,
) -> dict:
    return {
        "status":        status,
        "accepted":      accepted,
        "rejected":      rejected,
        "file_path":     file_path,
        "response_path": response_path,
        "record_path":   None,   # filled in by submit() after writing
        "message":       message,
    }


def _error_result(message: str) -> dict:
    return _make_result("error", 0, 0, None, None, message)
