"""Group fuzz-run failures by signature and print a markdown report.

  venv/bin/python -m tests.fuzz.triage sim_results/fuzz_node/<run>
  venv/bin/python -m tests.fuzz.triage sim_results/fuzz_node/<run> --known known_failures.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def signature(violation: str) -> str:
    """Stable signature: strip numbers/paths so edits don't split groups."""
    s = re.sub(r"0x[0-9a-f]+", "ADDR", violation)
    s = re.sub(r"\d+(\.\d+)?", "N", s)
    s = re.sub(r"/[\w./-]+", "PATH", s)
    return s[:160]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--known", type=Path,
                    help="file of known signatures to suppress")
    args = ap.parse_args()

    results_file = args.run_dir / "results.jsonl"
    if not results_file.exists():
        results_file = args.run_dir / "failures.jsonl"
    if not results_file.exists():
        print(f"no results.jsonl/failures.jsonl in {args.run_dir}")
        return 2

    known = set()
    if args.known and args.known.exists():
        known = {ln.strip() for ln in args.known.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}

    groups: dict[str, list] = defaultdict(list)
    total = failing = 0
    for line in results_file.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        total += 1
        vs = rec.get("violations") or ([rec.get("invariant")] if rec.get("invariant") else [])
        if rec.get("status", 0) and rec["status"] >= 500:
            vs = vs or [f"HTTP {rec['status']} {rec.get('method','')} {rec.get('rule','')}"]
        if not vs:
            continue
        failing += 1
        for v in vs:
            groups[signature(str(v))].append(rec)

    fresh = {k: v for k, v in groups.items() if k not in known}
    print(f"# Fuzz triage: {args.run_dir.name}")
    print(f"{total} records, {failing} failing, "
          f"{len(groups)} signatures ({len(fresh)} new)\n")
    for sig, recs in sorted(fresh.items(), key=lambda kv: -len(kv[1])):
        seeds = sorted({r.get("seed", "?") for r in recs})[:10]
        print(f"## ×{len(recs)}  `{sig}`")
        print(f"   seeds: {seeds}")
        example = recs[0]
        for key in ("profile", "rule", "path", "sched_state"):
            if example.get(key) is not None:
                print(f"   {key}: {json.dumps(example[key], default=str)[:200]}")
        print()
    if not fresh:
        print("No new failure signatures.")
    return 1 if fresh else 0


if __name__ == "__main__":
    sys.exit(main())
