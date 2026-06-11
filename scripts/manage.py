#!/usr/bin/env python3
"""
Boundless Skies cloud — admin management CLI.

Run from the repo root:
    python scripts/manage.py [--config PATH] <command>

Commands
--------
    status          Show nodes, pending measurements, and AAVSO batch queue.
    ingest          Trigger alert ingestion, scoring, and plan generation now.
    batch           Preview the next AAVSO batch (dry-run, prints formatted output).
    submit          Submit the pending AAVSO batch to WebObs (sets dry_run=False
                    for this run only — does not modify config.yaml).
    check-aavso     Verify AAVSO credentials and format a single test observation.
    nights          Generate night summaries for all active nodes.

Examples
--------
    python scripts/manage.py status
    python scripts/manage.py ingest
    python scripts/manage.py batch
    python scripts/manage.py submit
    python scripts/manage.py check-aavso
    python scripts/manage.py --config /path/to/config.yaml status
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from the repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from cloud import alerts, data_pipeline, db, nights, registry, scheduler, scoring


_DEFAULT_CONFIG = Path(__file__).parent.parent / "cloud" / "config.yaml"


def load_config(path: str | None) -> dict:
    cfg_path = Path(path) if path else _DEFAULT_CONFIG
    try:
        with open(cfg_path) as fh:
            cfg = yaml.safe_load(fh) or {}
        print(f"Config: {cfg_path}")
        return cfg
    except FileNotFoundError:
        print(f"WARNING: config not found at {cfg_path} — using defaults")
        return {}


def _sep():
    print("-" * 60)


# ── status ─────────────────────────────────────────────────────────────────────

def cmd_status(config: dict) -> None:
    print("\n=== Boundless Skies Cloud Status ===\n")

    # Nodes
    nodes = registry.list_nodes()
    online = [n for n in nodes if registry.is_online(n)]
    print(f"Nodes: {len(nodes)} registered, {len(online)} online")
    for n in nodes:
        status_str = "ONLINE" if registry.is_online(n) else "offline"
        hb = n.get("last_heartbeat", "never")[:16] if n.get("last_heartbeat") else "never"
        print(f"  {n['node_id']:20s}  {status_str:8s}  last heartbeat: {hb}"
              f"  ({n.get('city','')}, {n.get('country','')})")

    _sep()

    # Measurements
    total = db.query_one("SELECT COUNT(*) AS n FROM measurements") or {"n": 0}
    pending = db.query_one(
        """SELECT COUNT(*) AS n FROM measurements
           WHERE aavso_submitted = 0
             AND quality_flag IN ('good', 'acceptable')
             AND validation_status IN ('consistent', 'single')"""
    ) or {"n": 0}
    submitted = db.query_one(
        "SELECT COUNT(*) AS n FROM measurements WHERE aavso_submitted = 1"
    ) or {"n": 0}
    print(f"Measurements: {total['n']} total  |  "
          f"{pending['n']} pending submission  |  {submitted['n']} submitted to AAVSO")

    _sep()

    # Recent AAVSO batches
    batches = db.query(
        "SELECT * FROM aavso_batches ORDER BY submitted_at DESC LIMIT 5")
    if batches:
        print("Recent AAVSO batches:")
        for b in batches:
            print(f"  {b['submitted_at'][:16]}  status={b['status']:10s}"
                  f"  n={b['n_obs']:4d}  accepted={b['accepted']}  rejected={b['rejected']}")
    else:
        print("No AAVSO batches yet.")

    _sep()

    # Active targets
    targets = db.query_one("SELECT COUNT(*) AS n FROM targets WHERE active = 1") or {"n": 0}
    print(f"Active targets in queue: {targets['n']}")

    # AAVSO config status
    aavso = config.get("aavso", {})
    print(f"\nAAVSO config:")
    print(f"  observer_code : {aavso.get('observer_code') or '(not set)'}")
    print(f"  username      : {aavso.get('username') or '(not set)'}")
    print(f"  password      : {'(set)' if aavso.get('password') else '(not set)'}")
    print(f"  dry_run       : {aavso.get('dry_run', True)}")
    print()


# ── ingest ─────────────────────────────────────────────────────────────────────

def cmd_ingest(config: dict) -> None:
    print("\n=== Alert Ingestion + Scoring + Planning ===\n")
    print("Ingesting alerts from all sources...")
    result = alerts.ingest_all(config)
    print(f"  new={result.get('new', 0)}  updated={result.get('updated', 0)}"
          f"  expired={result.get('expired', 0)}")
    for src, r in result.get("sources", {}).items():
        print(f"  {src:12s}: {r}")

    _sep()
    print("Scoring targets...")
    scored = scoring.score_all(config)
    print(f"  Scored {scored} node×target pairs.")

    _sep()
    print("Generating plans for all active nodes...")
    n_plans = scheduler.generate_all_plans(config)
    print(f"  Generated {n_plans} plan(s).")
    print()


# ── batch / submit ─────────────────────────────────────────────────────────────

def cmd_batch(config: dict, submit: bool = False) -> None:
    aavso_cfg = config.get("aavso", {})
    observer_code = (aavso_cfg.get("observer_code") or "").upper().strip()

    if not observer_code:
        print("ERROR: aavso.observer_code is not configured in config.yaml")
        sys.exit(1)

    if submit and not aavso_cfg.get("username"):
        print("ERROR: aavso.username is not configured in config.yaml")
        sys.exit(1)

    if submit:
        print("\n=== Submitting AAVSO Batch ===\n")
        # Override dry_run for this call only
        cfg = dict(config)
        cfg["aavso"] = dict(aavso_cfg)
        cfg["aavso"]["dry_run"] = False
        result = data_pipeline.submit_pending_batch(cfg)
    else:
        print("\n=== AAVSO Batch Preview (dry-run) ===\n")
        cfg = dict(config)
        cfg["aavso"] = dict(aavso_cfg)
        cfg["aavso"]["dry_run"] = True
        result = data_pipeline.submit_pending_batch(cfg)

    print(f"Status  : {result['status']}")
    print(f"Message : {result.get('message', '')}")
    print(f"n_obs   : {result.get('n_obs', 0)}")
    if result.get("file_path"):
        print(f"File    : {result['file_path']}")
        if not submit:
            print("\n--- Formatted batch (first 40 lines) ---")
            try:
                lines = Path(result["file_path"]).read_text().splitlines()
                for line in lines[:40]:
                    print(line)
                if len(lines) > 40:
                    print(f"... ({len(lines) - 40} more lines)")
            except OSError as exc:
                print(f"(could not read file: {exc})")
    print()


# ── check-aavso ────────────────────────────────────────────────────────────────

def cmd_check_aavso(config: dict) -> None:
    """
    Format a single synthetic test observation and optionally verify credentials
    by connecting to the AAVSO WebObs endpoint without submitting real data.
    """
    print("\n=== AAVSO Credential & Format Check ===\n")
    aavso_cfg = config.get("aavso", {})

    # Config checks
    issues = []
    if not aavso_cfg.get("observer_code"):
        issues.append("aavso.observer_code is not set")
    if not aavso_cfg.get("username"):
        issues.append("aavso.username is not set")
    if not aavso_cfg.get("password"):
        issues.append("aavso.password is not set")

    if issues:
        print("Configuration issues:")
        for i in issues:
            print(f"  ✗ {i}")
        print()
    else:
        print("Configuration: OK (observer_code, username, password all set)")
        print()

    # Format a synthetic test measurement
    now_bjd = 2451545.0 + (datetime.now(timezone.utc).timestamp() - 946728000) / 86400
    synthetic = {
        "target_name":    "BW Tau",       # known AAVSO variable
        "bjd":            round(now_bjd, 6),
        "magnitude":      12.345,
        "uncertainty":    0.05,
        "filter":         "CV",
        "airmass":        1.25,
        "fwhm":           3.1,
        "snr":            42.0,
        "comparison_stars": 6,
        "quality_flag":   "good",
        "node_id":        "test_node",
        "zero_point":     22.4,
        "zp_scatter":     0.02,
        "fits_file":      "test.fits",
    }

    from aavso_submission import _format_extended
    observer_code = (aavso_cfg.get("observer_code") or "XXXXX").upper()
    formatted = _format_extended(synthetic, observer_code, aavso_cfg)
    print("Formatted AAVSO Extended File Format (synthetic observation):")
    _sep()
    print(formatted)
    _sep()

    # Attempt a network connection to WebObs (without submitting)
    if not issues:
        print("Testing network connection to AAVSO WebObs...")
        try:
            import requests
            resp = requests.get("https://www.aavso.org/apps/webobs/submit/",
                                timeout=10)
            print(f"  AAVSO WebObs reachable: HTTP {resp.status_code}")
        except Exception as exc:
            print(f"  WARNING: could not reach AAVSO WebObs: {exc}")
    print()


# ── nights ─────────────────────────────────────────────────────────────────────

def cmd_nights(config: dict) -> None:
    print("\n=== Night Summary Generation ===\n")
    n = nights.generate_pending_summaries(config)
    print(f"Generated {n} new night summary/summaries.")
    recent = db.query(
        "SELECT node_id, night, n_observations, n_submitted FROM night_summaries"
        " ORDER BY night DESC LIMIT 10"
    )
    if recent:
        print("\nRecent summaries:")
        for r in recent:
            print(f"  {r['night']}  {r['node_id']:20s}"
                  f"  {r['n_observations']:3d} obs  {r['n_submitted']} submitted")
    print()


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Boundless Skies cloud admin CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="path to config.yaml (default: cloud/config.yaml)")
    parser.add_argument(
        "command",
        choices=["status", "ingest", "batch", "submit", "check-aavso", "nights"],
        help="command to run",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    db.init(config.get("database", {}).get("path", "cloud_data/cloud.db"))

    dispatch = {
        "status":      lambda: cmd_status(config),
        "ingest":      lambda: cmd_ingest(config),
        "batch":       lambda: cmd_batch(config, submit=False),
        "submit":      lambda: cmd_batch(config, submit=True),
        "check-aavso": lambda: cmd_check_aavso(config),
        "nights":      lambda: cmd_nights(config),
    }
    dispatch[args.command]()


if __name__ == "__main__":
    main()
