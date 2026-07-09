#!/usr/bin/env python3
"""Prepare and audit a reproducible beta-node validation corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from astropy.io import fits

CAMPAIGNS = {
    "C1": "header_semantics",
    "C2": "linearity_saturation",
    "C3": "standard_field",
    "C4": "known_variable",
    "C5": "crowded_field",
    "C6": "multi_node",
    "C7": "flats_vignetting",
}


def _manifest(root: Path) -> dict:
    path = root / "capture_manifest.json"
    return json.loads(path.read_text()) if path.exists() else {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "captures": [],
    }


def _write(root: Path, payload: dict) -> None:
    (root / "capture_manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for code, name in CAMPAIGNS.items():
        (root / "fits" / f"{code}_{name}").mkdir(parents=True, exist_ok=True)
    _write(root, _manifest(root))
    validation = root / "manifest.json"
    if not validation.exists():
        validation.write_text(json.dumps({
            "comparison_star_file": "frozen_catalog.json",
            "config_overrides": {"photometry": {"saturation_adu": 60000}},
            "targets": {},
        }, indent=2) + "\n")
    catalog = root / "frozen_catalog.json"
    if not catalog.exists():
        catalog.write_text("[]\n")
    print(f"Beta corpus ready: {root}")


def add(root: Path, campaign: str, source: Path, node_id: str,
        started_utc: str | None, ended_utc: str | None) -> None:
    if campaign not in CAMPAIGNS:
        raise SystemExit(f"Unknown campaign {campaign}; choose {', '.join(CAMPAIGNS)}")
    if not source.is_file():
        raise SystemExit(f"FITS file not found: {source}")
    init(root)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with fits.open(source, memmap=False) as hdul:
        hdr = hdul[0].header
        metadata = {
            "object": str(hdr.get("OBJECT", "")),
            "date_obs": str(hdr.get("DATE-OBS", "")),
            "exptime_s": hdr.get("EXPTIME", hdr.get("EXPOSURE")),
            "filter": str(hdr.get("FILTER", "")),
            "shape": list(hdul[0].data.shape) if hdul[0].data is not None else None,
        }
    destination = root / "fits" / f"{campaign}_{CAMPAIGNS[campaign]}" / source.name
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"Refusing to overwrite a different file: {destination}")
    shutil.copy2(source, destination)
    manifest = _manifest(root)
    manifest["captures"] = [
        item for item in manifest["captures"] if item.get("sha256") != digest
    ]
    manifest["captures"].append({
        "campaign": campaign,
        "file": str(destination.relative_to(root)),
        "node_id": node_id,
        "wall_start_utc": started_utc,
        "wall_end_utc": ended_utc,
        "sha256": digest,
        "fits": metadata,
    })
    _write(root, manifest)
    print(f"Captured {campaign}: {destination}")


def audit(root: Path) -> int:
    manifest = _manifest(root)
    captures = manifest.get("captures", [])
    failures = []
    for item in captures:
        path = root / item["file"]
        if not path.exists():
            failures.append(f"missing: {item['file']}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            failures.append(f"checksum mismatch: {item['file']}")
    present = {item["campaign"] for item in captures}
    print(f"{len(captures)} capture(s); campaigns present: {', '.join(sorted(present)) or 'none'}")
    for code in CAMPAIGNS:
        print(f"  {'PASS' if code in present else 'TODO'} {code} — {CAMPAIGNS[code]}")
    for failure in failures:
        print(f"  FAIL {failure}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("validation_corpus"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    add_p = sub.add_parser("add")
    add_p.add_argument("campaign", choices=CAMPAIGNS)
    add_p.add_argument("fits_file", type=Path)
    add_p.add_argument("--node-id", required=True)
    add_p.add_argument("--started-utc", help="Independent wall-clock start, ISO-8601")
    add_p.add_argument("--ended-utc", help="Independent wall-clock end, ISO-8601")
    sub.add_parser("audit")
    args = parser.parse_args()
    if args.command == "init":
        init(args.root)
        return 0
    if args.command == "add":
        add(args.root, args.campaign, args.fits_file, args.node_id,
            args.started_utc, args.ended_utc)
        return 0
    return audit(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
