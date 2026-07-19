#!/usr/bin/env python3
"""
Digital-twin CLI.

    python -m sim list
        Show the scenario library.

    python -m sim run --scenario beta_5_nodes
        Simulate one scenario, all schedulers, 3 seed replicates, and write
        JSON/CSV/Markdown under sim_results/<scenario>/.

    python -m sim run --scenario launch_50_nodes --schedulers chorus,random \
        --seeds 42,43 --nights 7 --out /tmp/twin
        Everything is overridable; no network, no DB, deterministic per seed.

    python -m sim sweep-nodes --sizes 5,20,50,100 --nights 7
        Marginal-value-of-scale study: same catalog, growing fleet.
"""

import argparse
import json
import sys
import time

from sim import metrics as metrics_mod
from sim import report as report_mod
from sim import scenarios as scen_mod
from sim.engine import build_world, run_world
from sim.schedulers import SCHEDULERS

DEFAULT_SEEDS = (42, 43, 44)


def _parse_list(text, cast=str):
    return [cast(x) for x in str(text).split(",") if x != ""]


def run_scenario(scenario, schedulers, seeds, verbose=True) -> dict:
    """scheduler -> [run result per seed], all over identical worlds."""
    results = {s: [] for s in schedulers}
    for seed in seeds:
        world = build_world(scenario, seed)
        for sched in schedulers:
            t0 = time.monotonic()
            res = run_world(world, sched)
            dt = time.monotonic() - t0
            results[sched].append(res)
            if verbose:
                acc = sum(n["n_accepted"] for n in res["nights"])
                print(f"  seed {seed} {sched:14s} "
                      f"accepted={acc:5d}  ({dt:5.1f}s)", flush=True)
    return results


def cmd_list(_args) -> int:
    for name, sc in sorted(scen_mod.SCENARIOS.items()):
        print(f"{name:28s} {sc.n_nodes:5d} nodes  {sc.nights:3d} nights  "
              f"— {sc.description.splitlines()[0]}")
    return 0


def cmd_run(args) -> int:
    scenario = scen_mod.get(args.scenario)
    overrides = {}
    if args.nights:
        overrides["nights"] = args.nights
    if args.nodes:
        overrides["n_nodes"] = args.nodes
    if args.local_search_ms is not None:
        overrides["local_search_ms"] = args.local_search_ms
    if overrides:
        scenario = scenario.variant(**overrides)
    schedulers = _parse_list(args.schedulers) if args.schedulers \
        else list(SCHEDULERS)
    for s in schedulers:
        if s not in SCHEDULERS:
            print(f"unknown scheduler '{s}' (have: {', '.join(SCHEDULERS)})")
            return 2
    seeds = _parse_list(args.seeds, int) if args.seeds else list(DEFAULT_SEEDS)

    print(f"scenario {scenario.name}: {scenario.n_nodes} nodes, "
          f"{scenario.n_targets} targets, {scenario.nights} nights, "
          f"seeds {seeds}", flush=True)
    results = run_scenario(scenario, schedulers, seeds)
    payload = report_mod.write_scenario_results(args.out, scenario, results)
    print(f"\nwrote {args.out}/{scenario.name}/{{summary.json,nights.csv,"
          f"README.md}}\n")
    ordered = sorted(payload["aggregates"].values(),
                     key=lambda a: -(a.get("accepted_per_night", {})
                                     or {}).get("mean", 0.0))
    print(metrics_mod.comparison_table(ordered))
    return 0


def cmd_sweep_nodes(args) -> int:
    base = scen_mod.get(args.scenario)
    sizes = _parse_list(args.sizes, int)
    seeds = _parse_list(args.seeds, int) if args.seeds else list(DEFAULT_SEEDS)
    schedulers = _parse_list(args.schedulers) if args.schedulers else ["chorus"]
    rows = []
    for size in sizes:
        sc = base.variant(name=f"{base.name}_n{size}", n_nodes=size,
                          nights=args.nights or base.nights)
        print(f"— fleet size {size} —", flush=True)
        results = run_scenario(sc, schedulers, seeds)
        for sched, runs in results.items():
            agg = metrics_mod.aggregate(
                [metrics_mod.summarize_run(r) for r in runs])
            rows.append({"n_nodes": size, "scheduler": sched,
                         "accepted_per_night": agg["accepted_per_night"],
                         "realized_value_per_night":
                             agg["realized_value_per_night"],
                         "utc_hours_covered_per_night":
                             agg["utc_hours_covered_per_night"],
                         "transit_coverage_frac":
                             agg.get("transit_coverage_frac"),
                         "alert_latency_median_h":
                             agg.get("alert_latency_median_h")})
    out = f"{args.out}/node_sweep_{base.name}.json"
    import os
    os.makedirs(args.out, exist_ok=True)
    with open(out, "w") as f:
        json.dump({"disclaimer": report_mod.DISCLAIMER,
                   "base_scenario": base.name, "seeds": seeds,
                   "rows": rows}, f, indent=2)
    print(f"wrote {out}")
    for r in rows:
        print(f"  n={r['n_nodes']:5d} {r['scheduler']:12s} "
              f"accepted/night={r['accepted_per_night']['mean']:8.1f} "
              f"±{r['accepted_per_night']['sd']:.1f}  "
              f"value/night={r['realized_value_per_night']['mean']:8.2f}")
    return 0


def _fuzz_worker(job):
    from sim.fuzz import fuzz_one
    seed, scheduler, check = job
    return fuzz_one(seed, scheduler, check_determinism=check)


def cmd_fuzz(args) -> int:
    import multiprocessing as mp
    import os
    start, _, end = args.seeds.partition(":")
    seeds = range(int(start), int(end))
    every = max(1, args.determinism_every)
    jobs = [(s, args.scheduler, s % every == 0) for s in seeds]
    os.makedirs(args.out, exist_ok=True)
    out_path = f"{args.out}/fuzz_{seeds.start}_{seeds.stop}.jsonl"
    n_fail = total_nights = 0
    t0 = time.time()
    with mp.get_context("spawn").Pool(args.parallel) as pool, \
            open(out_path, "a") as fh:
        for i, res in enumerate(pool.imap_unordered(_fuzz_worker, jobs), 1):
            total_nights += res.get("nights", 0)
            if res["violations"]:
                n_fail += 1
                print(f"  SEED {res['seed']} FAILED: "
                      f"{'; '.join(res['violations'])[:300]}", flush=True)
                fh.write(json.dumps(res) + "\n")
                fh.flush()
            if i % 100 == 0 or i == len(jobs):
                dt = time.time() - t0
                print(f"  {i}/{len(jobs)} scenarios, {total_nights} nights "
                      f"({total_nights / max(dt, 1e-9) * 3600:.0f} nights/h), "
                      f"{n_fail} failing", flush=True)
    print(f"[sim fuzz] {len(jobs)} scenarios, {total_nights} simulated nights, "
          f"{n_fail} failing → {out_path}")
    return 1 if n_fail else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sim",
                                 description="Telescope Net fleet digital twin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list scenarios")

    rp = sub.add_parser("run", help="run one scenario")
    rp.add_argument("--scenario", required=True)
    rp.add_argument("--schedulers", default="",
                    help=f"comma list of {', '.join(SCHEDULERS)} (default all)")
    rp.add_argument("--seeds", default="", help="comma list (default 42,43,44)")
    rp.add_argument("--nights", type=int, default=0)
    rp.add_argument("--nodes", type=int, default=0)
    rp.add_argument("--local-search-ms", type=float, default=None,
                    help="enable the seeded local search (host-dependent!)")
    rp.add_argument("--out", default="sim_results")

    fp = sub.add_parser("fuzz", help="randomized-scenario scheduler fuzzing")
    fp.add_argument("--seeds", default="0:200", help="range START:END")
    fp.add_argument("--scheduler", default="chorus")
    fp.add_argument("--parallel", type=int, default=8)
    fp.add_argument("--determinism-every", type=int, default=17,
                    help="re-run every Nth seed twice to check determinism")
    fp.add_argument("--out", default="sim_results/fuzz_sim")

    sp = sub.add_parser("sweep-nodes", help="fleet-size sweep")
    sp.add_argument("--scenario", default="launch_50_nodes")
    sp.add_argument("--sizes", default="5,20,50,100")
    sp.add_argument("--nights", type=int, default=0)
    sp.add_argument("--seeds", default="")
    sp.add_argument("--schedulers", default="chorus")
    sp.add_argument("--out", default="sim_results")

    args = ap.parse_args(argv)
    return {"list": cmd_list, "run": cmd_run, "fuzz": cmd_fuzz,
            "sweep-nodes": cmd_sweep_nodes}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
