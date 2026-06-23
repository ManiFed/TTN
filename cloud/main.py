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

from cloud import alerts, data_pipeline, db, nights, registry, scheduler, scoring, tuning
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


def main() -> None:
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else None)

    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=log_cfg.get("level", "INFO"),
        format=log_cfg.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )

    db.init(config.get("database", {}).get("url", ""))

    # ── Background loops ───────────────────────────────────────────────────────
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
        nights.generate_pending_summaries(config)
        registry.refresh_all_performance()
        # Light pollution drifts on month scales — refresh on day 1
        if time.gmtime().tm_mday == 1:
            registry.refresh_light_pollution(
                config.get("light_pollution", {}).get("api_key", ""))
        # Claude reviews recent outcomes and adjusts the scoring weights.
        # No-op unless tuning.enabled and an API key are configured.
        tuning.run_nightly(config)

    _loop("alert-ingest",
          float(alerts_cfg.get("interval_minutes", 60)) * 60, ingest_and_rescore)
    _loop("replan",
          float(sched_cfg.get("replan_interval_minutes", 120)) * 60, rescore_and_replan)
    _loop("aavso-batch",
          float(aavso_cfg.get("batch_interval_minutes", 360)) * 60,
          lambda: data_pipeline.submit_pending_batch(config))
    _loop("maintenance", 86400, maintenance)

    # ── API server ─────────────────────────────────────────────────────────────
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = int(os.environ.get("PORT", server_cfg.get("port", 8800)))
    app = create_app(config)
    logger.info("The Telescope Net cloud starting on %s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
