#!/usr/bin/env python3
"""Decide whether a change may merge without a person reading it.

An agent that finds its own bugs and opens its own pull requests is only safe
if something independent decides which of those may land unattended. That is
this file, and it is deliberately dumb: a path-based policy, not a judgement
about the change. A clever gate that can be argued out of its own rules is not
a gate.

Three things must never merge without a human:

  mount and hardware control   a bad merge points somebody's telescope at the
                               sun, or drives it into its own pier
  photometry and timing        wrong magnitudes are worse than no magnitudes,
                               and they are published under one obscode for the
                               whole network
  identity and credentials     the class of bug that silently orphans a node
                               and loses its entire observation history

Everything else — app screens, docs, dashboards, the MCP surface, tests — can
land on green CI, because the blast radius is a bug rather than a telescope or
a corrupted scientific record.

Usage:
    python scripts/merge_policy.py --base origin/main
    python scripts/merge_policy.py --files src/photometry.py cloud/db.py
    python scripts/merge_policy.py --base origin/main --require-auto   # CI gate
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys

#: (glob, why a person must read changes here).
#: Order matters only for reporting; every match is reported.
PROTECTED: tuple[tuple[str, str], ...] = (
    # -- mount, camera and enclosure control --------------------------------
    ("alpaca/telescope.py",       "drives the mount directly"),
    ("alpaca/camera.py",          "drives the camera directly"),
    ("alpaca/covercalibrator.py", "opens and closes the enclosure"),
    ("alpaca/focuser.py",         "drives the focuser"),
    ("alpaca/filterwheel.py",     "drives the filter wheel"),
    ("alpaca/safety_manager.py",  "decides when it is safe to open — the last "
                                  "thing standing between a mount and the sun"),
    ("alpaca/autofocus.py",       "moves the focuser unattended"),
    ("src/commissioning.py",      "first-light checks on unproven hardware"),

    # -- photometry, astrometry and time ------------------------------------
    ("src/photometry.py",         "produces the magnitudes the network publishes"),
    ("src/timescales.py",         "BJD/HJD conversion — a timing error is "
                                  "invisible and corrupts every light curve"),
    ("src/plate_solve.py",        "astrometric solution behind every measurement"),
    ("alpaca/platesolve.py",      "astrometric solution behind every measurement"),
    ("src/calibration_identity.py", "ties frames to their calibration"),
    ("src/stacking.py",           "combines frames into measured data"),
    ("cloud/calibration.py",      "network-wide photometric calibration"),
    ("cloud/objective.py",        "scoring that decides what the fleet observes"),
    ("cloud/transit_windows.py",  "timing windows for time-series targets"),

    # -- anything published outside this project ----------------------------
    ("src/aavso_submission.py",   "formats what is submitted to AAVSO under the "
                                  "network's obscode"),
    ("cloud/data_pipeline.py",    "assembles and submits AAVSO batches"),

    # -- identity, credentials and the fleet register -----------------------
    ("cloud/auth.py",             "member authentication"),
    ("cloud/registry.py",         "node identity and credentials — the orphaning "
                                  "class of bug lives here"),
    ("src/cloud_communicator.py", "node credential lifecycle and rekey"),

    # -- schema and deploy --------------------------------------------------
    ("cloud/db.py",               "database schema and migrations"),
    (".github/workflows/*",       "CI and release automation — including this gate"),
    ("scripts/merge_policy.py",   "this policy itself"),
)

AUTO = "auto_mergeable"
HUMAN = "human_required"


def changed_files(base: str) -> list[str]:
    """Files this branch changes relative to `base`."""
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", base],
            capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{merge_base}...HEAD"],
            capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Could not diff against {base}: {exc.stderr.strip()}")
    return [line.strip() for line in out.splitlines() if line.strip()]


def match(path: str) -> list[str]:
    """Every reason this path is protected. Empty means it is not."""
    return [why for glob, why in PROTECTED if fnmatch.fnmatch(path, glob)]


def classify(paths: list[str]) -> dict:
    """The verdict for a set of changed files."""
    blocking: list[dict] = []
    clear: list[str] = []
    for path in sorted(set(paths)):
        reasons = match(path)
        if reasons:
            blocking.append({"path": path, "reasons": reasons})
        else:
            clear.append(path)

    verdict = HUMAN if blocking else AUTO
    if not paths:
        # An empty diff is not an achievement; treat it as needing a look
        # rather than as trivially safe.
        verdict = HUMAN
        blocking = [{"path": "(no files)",
                     "reasons": ["the change appears to be empty"]}]

    return {
        "verdict": verdict,
        "blocking": blocking,
        "clear": clear,
        "summary": _summary(verdict, blocking, clear),
    }


def _summary(verdict: str, blocking: list[dict], clear: list[str]) -> str:
    if verdict == AUTO:
        return (f"Auto-mergeable: {len(clear)} file(s) changed, none in a "
                f"protected area. Green CI is sufficient.")
    lines = ["A person must read this before it merges:"]
    for item in blocking:
        for why in item["reasons"]:
            lines.append(f"  {item['path']} — {why}")
    if clear:
        lines.append(f"({len(clear)} other file(s) are not protected.)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="origin/main",
                        help="branch to diff against (default: origin/main)")
    parser.add_argument("--files", nargs="*",
                        help="classify these paths instead of diffing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--require-auto", action="store_true",
                        help="exit non-zero unless the change is auto-mergeable; "
                             "this is the CI gate for agent-authored PRs")
    args = parser.parse_args()

    paths = args.files if args.files is not None else changed_files(args.base)
    result = classify(paths)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["summary"])

    if args.require_auto and result["verdict"] != AUTO:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
