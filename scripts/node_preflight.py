#!/usr/bin/env python3
"""Beta-node readiness check.

Checks split into two tiers:
  * static  — file/config/dependency presence (always run, always read-only)
  * active  — actually exercises the integration points that fail in practice
              (cloud auth round-trip, real directory writes, solver launch)
              rather than just checking they *look* configured.

Exit 0 only when required checks pass.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml


def _check_cloud_auth(url: str, node_id: str, api_key: str) -> tuple[bool, str]:
    """Real authenticated round-trip against the cloud API (read-only)."""
    try:
        import requests
    except ImportError as exc:
        return False, f"requests not installed: {exc}"
    try:
        resp = requests.get(
            url.rstrip("/") + "/api/v1/nodes/me",
            headers={"X-Node-Id": node_id, "X-Api-Key": api_key},
            timeout=10,
        )
    except requests.RequestException:
        # Don't echo the exception text back to the report: it can carry the
        # request object (including the X-Api-Key header) in its repr, and
        # this credential must never end up in printed/logged output.
        return False, "request failed — could not reach cloud API"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    return True, "authenticated"


def _check_writable(path: Path, must_exist: bool = False) -> tuple[bool, str]:
    """Attempt an actual write+delete, not just an existence/permission-bit check."""
    if not path.exists():
        if must_exist:
            return False, f"{path} does not exist"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"could not create {path}: {exc}"
    fd, tmp_name = tempfile.mkstemp(prefix=".preflight_write_test_", dir=str(path))
    os.close(fd)
    probe = Path(tmp_name)
    try:
        probe.write_text("preflight")
        probe.unlink()
    except OSError as exc:
        return False, f"{path} not writable: {exc}"
    return True, str(path)


def _check_solver_launches(solver: str) -> tuple[bool, str]:
    """Confirm the solver binary actually executes here (not just present on PATH).

    Being on PATH doesn't mean it runs — wrong architecture, missing shared
    libraries, or a stale/broken install all show up only when you try to
    launch it.
    """
    try:
        subprocess.run(
            [solver, "-h"], capture_output=True, timeout=10, check=False,
        )
    except FileNotFoundError as exc:
        return False, f"could not launch {solver!r}: {exc}"
    except subprocess.TimeoutExpired:
        return True, f"{solver} launched (timed out waiting for exit, which is fine)"
    except OSError as exc:
        return False, f"could not launch {solver!r}: {exc}"
    return True, f"{solver} launched"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=5.0)
    parser.add_argument(
        "--active", action="store_true",
        help="Also run active checks: real cloud auth round-trip, real "
             "directory writes, and a real solver-binary launch. Slower, "
             "and touches the network, but catches the failures that a "
             "presence-only check misses.",
    )
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
    node_id = str(cloud.get("node_id") or "")
    api_key = str(cloud.get("api_key") or "")
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
        check("cloud_identity", bool(node_id) or state.exists(),
              "configured" if node_id else str(state))

        if args.active:
            if node_id and api_key:
                check("cloud_auth", *_check_cloud_auth(url, node_id, api_key))
            else:
                check("cloud_auth", True,
                      "no node_id/api_key yet — first-run auto-registration, skipped",
                      required=False)
    else:
        check("cloud_enabled", False, "cloud.enabled is false")

    watch_path = Path(str(config.get("image_watcher", {}).get("watch_path", ""))).expanduser()
    check("image_watch_path", bool(str(watch_path)) and watch_path.is_dir(), str(watch_path))

    solver = config.get("photometry", {}).get("astap_path", "astap")
    solver_ok = Path(str(solver)).expanduser().is_file() or shutil.which(str(solver)) is not None
    check("plate_solver", solver_ok, str(solver))

    if args.active:
        check("solver_launch", *_check_solver_launches(str(solver)))
        for name, path in (
            ("data", Path("data")),
            ("logs", Path("logs")),
            ("fits_export", Path("fits_export")),
            ("aavso_submissions", Path("aavso_submissions")),
        ):
            check(f"writable:{name}", *_check_writable(path))
        if str(watch_path):
            check("writable:image_watch_path", *_check_writable(watch_path, must_exist=True))

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
