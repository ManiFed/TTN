"""
Ingestion worker — turns any contributed frame into survey science.

Generalizes the old contrib_worker: instead of assuming a WCS is already
present, it runs a staged pipeline and records progress per row:

    triage → solve → extract → ingest

  * triage  — cheap gate (heuristics; a real image is worth the solver's time)
  * solve   — if the frame has no WCS, cloud/solver plate-solves it in place
  * extract — the node's run_survey_pipeline measures every catalog-matched star
  * ingest  — survey.ingest_batch folds it into the network's baselines (tier 0)

Jobs are claimed with `FOR UPDATE SKIP LOCKED` so several ingest-worker
replicas can share the queue without double-processing. Solving/extraction only
ever run here (never on the web VM), and only on the solver service that has the
astrometry.net index volume.
"""

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from cloud import db, survey

logger = logging.getLogger("cloud.ingest_worker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim_one() -> dict | None:
    """Atomically claim the oldest pending contribution (SKIP LOCKED)."""
    conn = db.connect()
    try:
        with conn:
            cur = conn.cursor(cursor_factory=_dict_cursor())
            cur.execute(
                """UPDATE contributions SET status = 'processing', stage = 'triage'
                   WHERE id = (
                       SELECT id FROM contributions WHERE status = 'pending'
                       ORDER BY created_at ASC LIMIT 1
                       FOR UPDATE SKIP LOCKED)
                   RETURNING *""")
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        db.release(conn)


def _dict_cursor():
    import psycopg2.extras
    return psycopg2.extras.RealDictCursor


def process_pending(config: dict, max_frames: int = 5) -> int:
    """Process up to max_frames pending contributions. Returns count done."""
    processed = 0
    for _ in range(max_frames):
        row = _claim_one()
        if row is None:
            break
        try:
            _process_one(row, config)
            processed += 1
        except Exception as exc:
            logger.error("Contribution #%s failed at stage %s: %s",
                         row["id"], row.get("stage"), exc)
            db.execute(
                "UPDATE contributions SET status = 'failed', error = %s, "
                "processed_at = %s WHERE id = %s",
                (f"[{row.get('stage')}] {str(exc)[:280]}", _now(), row["id"]))
    return processed


def _set_stage(cid: int, stage: str) -> None:
    db.execute("UPDATE contributions SET stage = %s WHERE id = %s", (stage, cid))


def _process_one(row: dict, config: dict) -> None:
    from src.photometry import run_survey_pipeline

    cid = row["id"]
    path = row.get("stored_path") or ""
    if not path or not os.path.exists(path):
        raise RuntimeError("stored file missing")

    # ── triage ────────────────────────────────────────────────────────────────
    _set_stage(cid, "triage")
    verdict = _triage(path, config)
    if verdict and not verdict.get("ok", True):
        import json
        db.execute(
            "UPDATE contributions SET status = 'failed', stage = 'triage', "
            "triage = %s, error = %s, processed_at = %s WHERE id = %s",
            (json.dumps(verdict), verdict.get("reason", "rejected by triage"),
             _now(), cid))
        return

    # ── solve (only if the frame arrived without a WCS) ────────────────────────
    _set_stage(cid, "solve")
    solved_info = None
    if not row.get("wcs_present"):
        from cloud import solver
        solved_info = solver.solve(path, config,
                                   hint_scale=row.get("pixel_scale_arcsec"))
        if solved_info is None:
            raise RuntimeError("plate solve failed (no index match / blind solve "
                               "unsuccessful)")
        db.execute(
            "UPDATE contributions SET solved = 1, pixel_scale_arcsec = %s "
            "WHERE id = %s",
            (solved_info.get("pixel_scale_arcsec"), cid))

    # ── extract ────────────────────────────────────────────────────────────────
    _set_stage(cid, "extract")
    survey_cfg = config.get("survey") or {}
    phot_config = {
        "photometry": {
            "node_id": row["node_id"],
            "survey_enabled": True,
            "survey_snr_min": float(survey_cfg.get("contrib_snr_min", 5.0)),
            "survey_max_sources":
                int(survey_cfg.get("max_sources_per_batch", 800)),
            "survey_max_zp_scatter":
                float(survey_cfg.get("contrib_max_zp_scatter", 0.15)),
        }
    }
    result = run_survey_pipeline(path, phot_config)
    if result is None:
        raise RuntimeError("survey pipeline produced no result "
                           "(bad frame, sparse field, or scattered zero point)")

    # ── ingest ────────────────────────────────────────────────────────────────
    _set_stage(cid, "ingest")
    sources = result.pop("survey_sources", [])
    # Archive frames (DATE-OBS older than survey.historical_days) route to the
    # retrospective path: they enrich baselines but never fire live alerts.
    historical = _is_historical(row, config)
    ingest = survey.ingest_batch(
        row["node_id"],
        {"frame": {**result, "fits_file": row["filename"]},
         "sources": sources},
        config,
        node_tier=0,
        historical=historical,
        provenance={"contribution_id": cid, "user_id": row.get("user_id")},
    )
    if not ingest.get("ok"):
        raise RuntimeError(f"survey ingest rejected: {ingest.get('error')}")

    db.execute(
        "UPDATE contributions SET status = 'done', stage = 'ingest', "
        "n_sources = %s, processed_at = %s WHERE id = %s",
        (int(ingest.get("accepted") or 0), _now(), cid))
    logger.info("Contribution #%s done: %d sources (%d deviants)%s from %s",
                cid, ingest.get("accepted", 0), ingest.get("deviants", 0),
                " [solved]" if solved_info else "", row["filename"])


def _is_historical(row: dict, config: dict) -> bool:
    """True when the frame's DATE-OBS is older than survey.historical_days."""
    days = float((config.get("survey") or {}).get("historical_days", 7.0))
    date_obs = (row.get("date_obs") or "").strip()
    if not date_obs:
        return False
    try:
        from datetime import datetime, timezone
        # DATE-OBS is ISO-ish; tolerate a trailing 'Z' and missing tz.
        d = datetime.fromisoformat(date_obs.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - d).total_seconds() / 86400.0
        return age_days > days
    except (ValueError, TypeError):
        return False


def _triage(path: str, config: dict) -> dict | None:
    """Cheap pre-solve gate. Delegates to cloud.triage when present; otherwise
    accepts (triage is a Phase 3 hardening — the pipeline is correct without
    it, just less efficient)."""
    try:
        from cloud import triage
    except Exception:
        return None
    try:
        return triage.classify(path, config)
    except Exception as exc:
        logger.debug("triage skipped (%s)", exc)
        return None


def prune_contrib_files(config: dict, days: float = 14.0) -> int:
    """Delete stored FITS for processed contributions past the window; the
    extracted measurements are the science, the pixels were always temporary."""
    cutoff = datetime.fromtimestamp(
        time.time() - days * 86400, tz=timezone.utc).isoformat()
    rows = db.query(
        "SELECT id, stored_path FROM contributions "
        "WHERE status IN ('done', 'failed') AND stored_path != '' "
        "  AND processed_at < %s",
        (cutoff,))
    removed = 0
    for r in rows:
        try:
            p = Path(r["stored_path"])
            if p.exists():
                p.unlink()
                removed += 1
            db.execute("UPDATE contributions SET stored_path = '' WHERE id = %s",
                       (r["id"],))
        except OSError as exc:
            logger.warning("Could not remove contribution file %s: %s",
                           r["stored_path"], exc)
    if removed:
        logger.info("Pruned %d contributed FITS files", removed)
    return removed


# CLI: run the worker standalone on the solver service.
def main() -> None:
    import sys
    from cloud.main import load_config
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db.init(config.get("database", {}).get("url", ""))
    logger.info("Ingest worker started")
    interval = float(config.get("survey", {}).get("ingest_interval_s", 5))
    while True:
        try:
            process_pending(config)
        except Exception as exc:
            logger.error("ingest loop error: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
