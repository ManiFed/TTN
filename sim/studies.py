#!/usr/bin/env python3
"""
The full digital-twin study battery behind the fleet report.

Chunks (each independently runnable, all offline, all seeded):

    python -m sim.studies core       scenario library with the fast schedulers
    python -m sim.studies legacy     legacy-solver comparisons (slow: the
                                     legacy greedy is quadratic in fleet size)
    python -m sim.studies scale      200/1,000-node runs + fleet-size sweep
    python -m sim.studies strategy   recruitment-geography, reliability,
                                     photometry, and CHORUS-parameter probes

Outputs land in sim_results/ (JSON/CSV/Markdown, all stamped as synthetic
projections).  Everything derives from seeds 42/43/44 (42/43 for the
1,000-node stress test to keep runtime sane).
"""

import dataclasses
import json
import os
import sys
import time

from sim import metrics as metrics_mod
from sim import report as report_mod
from sim import scenarios as scen_mod
from sim.cli import run_scenario
from sim.engine import build_world, run_world
from sim.world import generate_fleet

SEEDS = (42, 43, 44)
OUT = "sim_results"
FAST = ("chorus", "greedy_value", "greedy_nearest", "random")


def _agg(runs: list) -> dict:
    return metrics_mod.aggregate([metrics_mod.summarize_run(r) for r in runs])


def _run_and_write(scenario, schedulers, seeds=SEEDS, notes="") -> dict:
    print(f"\n=== {scenario.name} ({scenario.n_nodes} nodes, "
          f"{scenario.nights} nights, {list(schedulers)}) ===", flush=True)
    results = run_scenario(scenario, list(schedulers), list(seeds))
    return report_mod.write_scenario_results(OUT, scenario, results,
                                             notes=notes)


# ── Chunk: core scenarios ─────────────────────────────────────────────────────

def chunk_core() -> None:
    notes_legacy = ("The production legacy solver is omitted here for "
                    "runtime (it is quadratic in fleet size — see the "
                    "legacy chunk and the report's scalability section).")
    _run_and_write(scen_mod.get("beta_5_nodes"),
                   ("chorus", "legacy", "greedy_value", "greedy_nearest",
                    "random"))
    _run_and_write(scen_mod.get("launch_50_nodes"), FAST, notes=notes_legacy)
    for name in ("bad_weather_week", "alert_storm", "southern_gap",
                 "unreliable_fleet", "exoplanet_campaign",
                 "photometry_quality_crisis", "launch_50_nodes_global"):
        _run_and_write(scen_mod.get(name), FAST, notes=notes_legacy)


# ── Chunk: legacy-solver focus ────────────────────────────────────────────────

def chunk_legacy() -> None:
    sc = scen_mod.get("launch_50_nodes").variant(
        name="launch_50_nodes_legacy7", nights=7,
        description="launch_50_nodes shortened to 7 nights so the legacy "
                    "solver (~2 min per simulated night at 50 nodes) can be "
                    "compared on identical nights with every scheduler.")
    _run_and_write(sc, ("chorus", "legacy", "greedy_value", "random"))


# ── Chunk: scale ──────────────────────────────────────────────────────────────

def chunk_scale() -> None:
    _run_and_write(scen_mod.get("growth_200_nodes"), FAST)
    _run_and_write(scen_mod.get("global_1000_nodes"),
                   ("chorus", "greedy_value", "random"), seeds=(42, 43))
    # Fleet-size sweep: identical catalog, growing fleet (diminishing returns).
    base = scen_mod.get("launch_50_nodes")
    rows = []
    for size in (5, 10, 20, 50, 100, 200):
        sc = base.variant(name=f"sweep_n{size}", n_nodes=size, nights=7)
        res = run_scenario(sc, ["chorus"], list(SEEDS), verbose=True)
        agg = _agg(res["chorus"])
        rows.append({"n_nodes": size, **{k: agg[k] for k in (
            "accepted_per_night", "realized_value_per_night",
            "value_capture_frac", "utc_hours_covered_per_night",
            "transit_coverage_frac", "alert_latency_median_h",
            "node_load_gini") if k in agg}})
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "node_scaling.json"), "w") as f:
        json.dump({"disclaimer": report_mod.DISCLAIMER,
                   "base_scenario": base.name, "nights": 7,
                   "seeds": list(SEEDS), "scheduler": "chorus",
                   "rows": rows}, f, indent=2)
    print("wrote", os.path.join(OUT, "node_scaling.json"), flush=True)


# ── Chunk: strategy probes ────────────────────────────────────────────────────

def _world_with_extra_nodes(base_scenario, seed: int, region: str,
                            n_extra: int):
    """Base world plus n_extra nodes in one region — the recruitment
    counterfactual.  The base fleet, catalog, weather draws, and target truth
    are bit-identical to the base world; only the new nodes differ."""
    world = build_world(base_scenario, seed)
    extra = generate_fleet(n_extra, seed + 1_000_003, regions=[region])
    for i, node in enumerate(extra):
        node.node_id = f"new_{region}_{i:02d}"
    world.fleet = world.fleet + extra
    return world


def chunk_strategy() -> None:
    os.makedirs(OUT, exist_ok=True)

    # 1. Where should the next 10 nodes go?  beta fleet + 10 nodes per region.
    base = scen_mod.get("beta_5_nodes")
    placement = {"base": [], "regions": {}}
    for seed in SEEDS:
        r = run_world(build_world(base, seed), "chorus")
        placement["base"].append(metrics_mod.summarize_run(r))
    for region in ("na_southwest", "europe_south", "east_asia", "hawaii",
                   "australia", "south_america", "southern_africa"):
        runs = []
        for seed in SEEDS:
            world = _world_with_extra_nodes(base, seed, region, 10)
            runs.append(metrics_mod.summarize_run(run_world(world, "chorus")))
        placement["regions"][region] = metrics_mod.aggregate(runs)
        a = placement["regions"][region]["accepted_per_night"]
        print(f"  +10 in {region:16s} accepted/night "
              f"{a['mean']:6.1f} ± {a['sd']:.1f}", flush=True)
    placement["base_agg"] = metrics_mod.aggregate(placement["base"])
    with open(os.path.join(OUT, "recruitment_geography.json"), "w") as f:
        json.dump({"disclaimer": report_mod.DISCLAIMER,
                   "study": "beta_5_nodes + 10 recruits per candidate region, "
                            "chorus, 14 nights", **placement},
                  f, indent=2, default=str)

    # 2. Reliability vs geography at 50 nodes: matched-size counterfactuals.
    base50 = scen_mod.get("launch_50_nodes")
    variants = {
        "base": base50,
        "reliability_push": base50.variant(
            name="launch_50_reliability_push", reliability_scale=1.15,
            p_night_up_range=(0.92, 0.99)),
        "southern_rebalance": scen_mod.get("launch_50_nodes_global"),
        "better_photometry": base50.variant(
            name="launch_50_better_photometry", kappa_scale=0.6),
    }
    lever = {}
    for tag, sc in variants.items():
        runs = [metrics_mod.summarize_run(
                    run_world(build_world(sc, seed), "chorus"))
                for seed in SEEDS]
        lever[tag] = metrics_mod.aggregate(runs)
        a = lever[tag]["accepted_per_night"]
        print(f"  lever {tag:20s} accepted/night {a['mean']:6.1f} "
              f"± {a['sd']:.1f}", flush=True)
    with open(os.path.join(OUT, "improvement_levers.json"), "w") as f:
        json.dump({"disclaimer": report_mod.DISCLAIMER,
                   "study": "launch_50_nodes counterfactual levers, chorus, "
                            "14 nights", "levers": lever},
                  f, indent=2, default=str)

    # 3. CHORUS utilization probe: is min_marginal leaving capacity idle?
    probes = {
        "default": None,
        "eager": {"min_marginal": 0.005, "max_obs_per_target": 8.0},
        "very_eager": {"min_marginal": 0.001, "max_obs_per_target": 12.0},
    }
    util = {}
    for tag, overrides in probes.items():
        sc = base50.variant(name=f"launch_50_chorus_{tag}",
                            chorus_overrides=overrides)
        runs = [metrics_mod.summarize_run(
                    run_world(build_world(sc, seed), "chorus"))
                for seed in SEEDS]
        util[tag] = metrics_mod.aggregate(runs)
        a, v = util[tag]["accepted_per_night"], util[tag]["realized_value_per_night"]
        print(f"  chorus[{tag:10s}] accepted/night {a['mean']:6.1f} "
              f"value/night {v['mean']:6.1f}", flush=True)
    with open(os.path.join(OUT, "chorus_utilization_probe.json"), "w") as f:
        json.dump({"disclaimer": report_mod.DISCLAIMER,
                   "study": "Ring-1 hyperparameter probe: min_marginal / "
                            "max_obs_per_target vs fleet utilization, "
                            "launch_50_nodes, chorus, 14 nights",
                   "probes": {t: dict(agg,
                                      overrides=probes[t])
                              for t, agg in util.items()}},
                  f, indent=2, default=str)


CHUNKS = {"core": chunk_core, "legacy": chunk_legacy,
          "scale": chunk_scale, "strategy": chunk_strategy}


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in CHUNKS:
        print(f"usage: python -m sim.studies [{'|'.join(CHUNKS)}]")
        return 2
    t0 = time.monotonic()
    CHUNKS[args[0]]()
    print(f"\nchunk '{args[0]}' done in {time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
