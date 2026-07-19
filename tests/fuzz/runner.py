"""Parallel fuzz runner for the node harness.

One seed = one subprocess (dashboard state is module-global and cannot be
re-instantiated in-process). Results are JSON-lines; failing seeds keep
their full artifact dir (fault plan, node log, final state) for replay.

  venv/bin/python -m tests.fuzz.runner --seeds 0:1000 --parallel 8
  venv/bin/python -m tests.fuzz.runner --replay 137 --profile mixed
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CHILD_TIMEOUT_S = 180


def _run_one_child(seed: int, profile: str, scenario_s: float) -> dict:
    """Executed inside the child process (--one)."""
    from tests.fuzz.faults import FaultPlan
    from tests.fuzz.harness import NodeHarness
    plan = FaultPlan.generate(seed, scenario_s=scenario_s, profile=profile)
    harness = NodeHarness(plan, scenario_s=scenario_s)
    try:
        result = harness.run()
    except Exception as exc:
        import traceback
        result = {"violations": [f"harness crashed: {type(exc).__name__}: {exc}"],
                  "traceback": traceback.format_exc()[-2000:],
                  "plan": json.loads(plan.to_json())}
    result["seed"] = seed
    result["profile"] = profile
    result["workdir"] = str(harness.workdir)
    return result


def _spawn(seed: int, profile: str, scenario_s: float, out_dir: Path) -> dict:
    """Parent side: run one seed in a subprocess with a hard timeout."""
    # Absolute: the child chdirs into its scratch workdir before writing.
    result_path = (out_dir / f"seed_{seed}.json").resolve()
    cmd = [sys.executable, "-m", "tests.fuzz.runner", "--one", str(seed),
           "--profile", profile, "--scenario-s", str(scenario_s),
           "--result-file", str(result_path)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True,
                              timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"seed": seed, "profile": profile, "elapsed_s": CHILD_TIMEOUT_S,
                "violations": [f"child process hung beyond {CHILD_TIMEOUT_S}s "
                               "(deadlock or wedged scenario)"]}
    if result_path.exists():
        result = json.loads(result_path.read_text())
    else:
        result = {"seed": seed, "profile": profile,
                  "violations": [f"child exited {proc.returncode} without a result"],
                  "stderr": proc.stderr.decode(errors="replace")[-2000:]}
    result["elapsed_s"] = round(time.time() - t0, 1)
    return result


def run_campaign(seeds: range, profile: str, scenario_s: float,
                 parallel: int, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "results.jsonl"
    n_fail = 0
    t0 = time.time()
    with results_file.open("a") as fh, \
            ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_spawn, s, profile, scenario_s, out_dir): s
                   for s in seeds}
        done = 0
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            keep = bool(result.get("violations"))
            if not keep:
                # clean run: drop bulky fields and its workdir
                wd = result.pop("workdir", "")
                result.pop("thread_exceptions", None)
                if wd:
                    shutil.rmtree(wd, ignore_errors=True)
                (out_dir / f"seed_{result['seed']}.json").unlink(missing_ok=True)
            else:
                n_fail += 1
                print(f"  SEED {result['seed']} FAILED: "
                      f"{'; '.join(result['violations'])[:300]}")
            fh.write(json.dumps(result, default=str) + "\n")
            fh.flush()
            if done % 25 == 0 or done == len(futures):
                rate = done / max(time.time() - t0, 1e-9) * 3600
                print(f"  {done}/{len(futures)} seeds "
                      f"({rate:.0f}/h), {n_fail} failing")
    print(f"[fuzz_node] {len(seeds)} seeds, {n_fail} failing → {results_file}")
    return 1 if n_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="0:100", help="range START:END")
    ap.add_argument("--profile", default="mixed",
                    choices=["none", "transport", "protocol", "semantic",
                             "behavioral", "mixed", "heavy"])
    ap.add_argument("--scenario-s", type=float, default=25.0)
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--out", type=Path,
                    default=Path("sim_results/fuzz_node") / time.strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--replay", type=int, help="re-run one seed verbosely")
    # internal: child mode
    ap.add_argument("--one", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one is not None:
        result = _run_one_child(args.one, args.profile, args.scenario_s)
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        args.result_file.write_text(json.dumps(result, indent=2, default=str))
        return 0

    if args.replay is not None:
        out = Path(tempfile.mkdtemp(prefix="fuzzreplay_"))
        result = _spawn(args.replay, args.profile, args.scenario_s, out)
        print(json.dumps(result, indent=2, default=str))
        return 1 if result.get("violations") else 0

    start, _, end = args.seeds.partition(":")
    return run_campaign(range(int(start), int(end)), args.profile,
                        args.scenario_s, args.parallel, args.out)


if __name__ == "__main__":
    sys.exit(main())
