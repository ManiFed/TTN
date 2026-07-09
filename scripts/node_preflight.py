#!/usr/bin/env python3
"""Read-only beta-node readiness check. Exit 0 only when required checks pass."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=5.0)
    args = parser.parse_args()
    checks = []

    def check(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    try:
        config = yaml.safe_load(args.config.read_text())
        check("config", isinstance(config, dict), str(args.config))
        config = config if isinstance(config, dict) else {}
    except Exception as exc:
        check("config", False, str(exc))
        config = {}

    for module in ("numpy", "astropy", "photutils", "watchdog", "requests"):
        check(f"dependency:{module}", importlib.util.find_spec(module) is not None, module)

    free_gb = shutil.disk_usage(Path.cwd()).free / (1024 ** 3)
    check("free_disk", free_gb >= args.min_free_gb, f"{free_gb:.1f} GiB free")

    observer = config.get("safety", {}).get("observer", {})
    lat, lon = observer.get("latitude"), observer.get("longitude")
    check("observer_location", lat not in (None, 0, 0.0) or lon not in (None, 0, 0.0),
          f"lat={lat!r}, lon={lon!r}")

    cloud = config.get("cloud", {})
    if cloud.get("enabled", False):
        url = str(cloud.get("url", ""))
        parsed = urlparse(url)
        check("cloud_url", parsed.scheme == "https" and bool(parsed.hostname), url)
        try:
            socket.getaddrinfo(parsed.hostname, parsed.port or 443)
            check("cloud_dns", True, parsed.hostname or "")
        except OSError as exc:
            check("cloud_dns", False, str(exc))
        state = Path("data/cloud_state.json")
        check("cloud_identity", bool(cloud.get("node_id")) or state.exists(),
              "configured" if cloud.get("node_id") else str(state))
    else:
        check("cloud_enabled", False, "cloud.enabled is false")

    watch_path = Path(str(config.get("image_watcher", {}).get("watch_path", ""))).expanduser()
    check("image_watch_path", bool(str(watch_path)) and watch_path.is_dir(), str(watch_path))

    solver = config.get("photometry", {}).get("astap_path", "astap")
    solver_ok = Path(str(solver)).expanduser().is_file() or shutil.which(str(solver)) is not None
    check("plate_solver", solver_ok, str(solver))

    required_failures = [c for c in checks if c["required"] and not c["ok"]]
    report = {"ready": not required_failures, "checks": checks}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in checks:
            print(f"{'PASS' if item['ok'] else 'FAIL'} {item['name']}: {item['detail']}")
        print("READY" if report["ready"] else f"NOT READY ({len(required_failures)} failed)")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
