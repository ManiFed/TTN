#!/usr/bin/env python3
"""
The Telescope Net cloud — entry point.

    python -m cloud.main            # uses cloud/config.yaml
    python -m cloud.main other.yaml

Starts the Flask API plus background loops:
    alert ingestion  → every alerts.interval_minutes (then rescoring)
    scoring + plans  → every scheduler.replan_interval_minutes
    AAVSO batches    → every aavso.batch_interval_minutes
    maintenance      → daily (image pruning, light pollution refresh monthly)
"""

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

import yaml

from cloud import alerts, crossmatch, data_pipeline, db, ingest_worker, live, nights, registry, scheduler, scoring, survey, tuning
from cloud.server import create_app

logger = logging.getLogger("cloud.main")

_DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"


def _expand_env(obj):
    """Recursively expand ${VAR} references in string config values."""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def load_config(path=None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    try:
        with open(cfg_path) as fh:
            raw = yaml.safe_load(fh) or {}
        return _expand_env(raw)
    except FileNotFoundError:
        logger.warning("Config %s not found — using defaults", cfg_path)
        return {}


def _loop(name: str, interval_s: float, fn) -> None:
    """Run fn forever on an interval; one failure never kills the loop."""
    def runner():
        time.sleep(5)   # let the server come up first
        while True:
            try:
                fn()
            except Exception as exc:
                logger.error("%s loop failed: %s", name, exc)
            time.sleep(interval_s)
    threading.Thread(target=runner, daemon=True, name=name).start()


def start_background_loops(config: dict) -> None:
    """Start all daemon threads. Called from main() and from cloud/wsgi.py."""
    alerts_cfg = config.get("alerts", {})
    sched_cfg = config.get("scheduler", {})
    aavso_cfg = config.get("aavso", {})

    def ingest_and_rescore():
        result = alerts.ingest_all(config)
        if result["new"] > 0:
            logger.info("New alerts (%d) — rescoring network", result["new"])
            scoring.score_all(config)

    def rescore_and_replan():
        scoring.score_all(config)
        scheduler.generate_all_plans(config)

    def maintenance():
        data_pipeline.prune_raw_images(config)
        survey.prune_survey_measurements(config)
        from cloud import moving_objects
        moving_objects.prune_detections(config)
        ingest_worker.prune_contrib_files(config)
        live.prune_dispatch_events()
        nights.generate_pending_summaries(config)
        registry.refresh_all_performance()
        if time.gmtime().tm_mday == 1:
            registry.refresh_light_pollution(
                config.get("light_pollution", {}).get("api_key", ""))
        if sched_cfg.get("reflow", False):
            # Self-healing: reconcile whether reflowed work got delivered so the
            # ledger join has a realization signal (algorithm untouched).
            try:
                from cloud.chorus import reflow
                reflow.reconcile_outcomes(config)
            except Exception as exc:
                logger.error("reflow reconcile failed: %s", exc)
        if sched_cfg.get("chorus") or sched_cfg.get("chorus_shadow"):
            # CHORUS T0 + Ring 0: reliability ledger, target state, weather
            # calibration, realization joins (best-effort, before tuning so
            # the backtest gate sees fresh realizations).
            try:
                from cloud.chorus import ledger as chorus_ledger
                chorus_ledger.run_nightly(config)
            except Exception as exc:
                logger.error("CHORUS ledger maintenance failed: %s", exc)
        tuning.run_nightly(config)
        if sched_cfg.get("chorus") or sched_cfg.get("chorus_shadow"):
            # Ring 2: backtest and apply/reject any pending structural class
            # template proposals — fully autonomous, gated the same way
            # Ring 1's scalar tuning already is.
            try:
                from cloud.chorus import ring2
                ring2.run_pending(config)
            except Exception as exc:
                logger.error("CHORUS Ring 2 maintenance failed: %s", exc)

    _loop("alert-ingest",
          float(alerts_cfg.get("interval_minutes", 60)) * 60, ingest_and_rescore)
    _loop("replan",
          float(sched_cfg.get("replan_interval_minutes", 120)) * 60, rescore_and_replan)
    _loop("aavso-batch",
          float(aavso_cfg.get("batch_interval_minutes", 360)) * 60,
          lambda: data_pipeline.submit_pending_batch(config))
    def crossmatch_pass():
        crossmatch.run_pending(config)
        crossmatch.run_pending_retro(config)
    _loop("crossmatch", 300, crossmatch_pass)
    def moving_objects_pass():
        from cloud import moving_objects
        moving_objects.link_tracklets(config)
        moving_objects.link_arcs(config)
        moving_objects.crossmatch_pending(config)
        moving_objects.dispatch_due_followups(config)
    _loop("moving-objects", 300, moving_objects_pass)
    # Solving/extraction (ingest_worker.process_pending) does NOT run here —
    # it needs the astrometry.net index volume, which only exists on the
    # dedicated solver-worker service (run via `python -m cloud.ingest_worker`).
    # Running it here would permanently fail any no-WCS contribution on first
    # attempt, since solve-field isn't installed on the API host.
    if sched_cfg.get("reflow", False):
        from cloud.chorus import reflow
        _loop("reflow", float(sched_cfg.get("reflow_interval_s", 60)),
              lambda: reflow.tick(config))
    _loop("maintenance", 86400, maintenance)


def main() -> None:
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else None)

    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )

    db.init(config.get("database", {}).get("url", ""))
    start_background_loops(config)

    # ── API server (dev only — production uses cloud/wsgi.py + gunicorn) ───────
    server_cfg = config.get("server", {})
    # Deploy config intentionally binds the cloud API publicly.
    host = server_cfg.get("host", "0.0.0.0")
    port = int(os.environ.get("PORT", server_cfg.get("port", 8800)))
    app = create_app(config)
    logger.info("The Telescope Net cloud starting on %s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
