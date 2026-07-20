"""Scheduler fuzzing: randomized extreme scenarios vs. the real CHORUS stack.

Each seed generates a Scenario with knobs sampled across (and beyond) the
ranges the curated library uses — degenerate fleets, brutal weather, alert
storms, hostile catalogs — then runs the production solver via the twin.

Invariants per run:
  S1 the engine never raises (run_world already hard-validates every pick:
     no double-booked slots, no unobservable placements, no over-long dwells
     — a ValueError from there is a solver bug, not a fuzz artifact)
  S2 summary metrics are finite and internally consistent
     (accepted ≤ executed ≤ planned, fractions in [0,1], nothing negative)
  S3 determinism: a sampled subset of seeds is run twice and must produce
     identical summaries

  python -m sim fuzz --seeds 0:1000 --parallel 8
"""

from __future__ import annotations

import json
import math
import random
import traceback

from sim import metrics as metrics_mod
from sim.engine import build_world, run_world
from sim.scenarios import Scenario

def random_scenario(seed: int) -> Scenario:
    rng = random.Random(seed * 2654435761 % 2**32)
    n_nodes = rng.choice([1, 1, 2, 3, 5, 8, 15, 30, 60, 120, 200])
    nights = rng.choice([1, 2, 3, 5, 7, 10])
    storm = rng.random() < 0.25
    return Scenario(
        name=f"fuzz_{seed}",
        description="randomized fuzz scenario",
        n_nodes=n_nodes,
        n_targets=rng.choice([1, 5, 30, 120, 400]),
        nights=nights,
        reliability_scale=rng.choice([0.2, 0.5, 1.0, 1.0]),
        forecast_skill_range=tuple(sorted((rng.uniform(0.0, 1.0),
                                           rng.uniform(0.0, 1.0)))),
        p_night_up_range=tuple(sorted((rng.uniform(0.05, 1.0),
                                       rng.uniform(0.05, 1.0)))),
        kappa_scale=rng.choice([0.1, 0.5, 1.0, 2.0]),
        dec_bias_north=rng.choice([0.0, 0.5, 1.0]),
        transient_rate_per_night=rng.choice([0.0, 0.15, 1.0, 5.0]),
        alert_storm_night=rng.randrange(nights) if storm else None,
        alert_storm_count=rng.choice([1, 6, 40]) if storm else 6,
        regional_correlation=rng.choice([0.0, 0.5, 1.0]),
        forecast_skill_scale=rng.choice([0.0, 0.5, 1.0, 1.5]),
        forecast_bias=rng.choice([-0.4, 0.0, 0.4]),
        qc_sigma_max=rng.choice([0.05, 0.25, 2.0]),
        catalog_failure_prob=rng.choice([0.0, 0.15, 0.5, 0.9]),
        max_targets_per_night=rng.choice([1, 4, 12, 40]),
        max_candidates_per_node=rng.choice([3, 20, 60]),
    )


def _scan_summary(s: dict) -> list[str]:
    problems = []
    for k, v in s.items():
        if isinstance(v, float) and not math.isfinite(v):
            problems.append(f"non-finite metric {k}={v}")
        if k in ("planned_per_night", "executed_per_night", "accepted_per_night",
                 "wasted_minutes_per_night", "realized_value_per_night") \
                and isinstance(v, (int, float)) and v < 0:
            problems.append(f"negative metric {k}={v}")
        if k in ("acceptance_rate", "value_capture_frac", "wasted_time_frac",
                 "transit_coverage_frac", "alert_response_frac") \
                and isinstance(v, (int, float)) and not -1e-9 <= v <= 1.0 + 1e-9:
            problems.append(f"fraction out of range {k}={v}")
    if s.get("accepted_per_night", 0) > s.get("executed_per_night", 0) + 1e-9:
        problems.append("accepted > executed")
    if s.get("executed_per_night", 0) > s.get("planned_per_night", 0) + 1e-9:
        problems.append("executed > planned")
    return problems


def _night_scan(result: dict) -> list[str]:
    problems = []
    for rec in result["nights"]:
        if rec["n_accepted"] > rec["n_executed"]:
            problems.append(
                f"night {rec.get('night')}: accepted {rec['n_accepted']} > "
                f"executed {rec['n_executed']}")
        if rec["n_executed"] > rec["n_planned"]:
            problems.append(
                f"night {rec.get('night')}: executed > planned")
        for k, v in rec.items():
            if isinstance(v, float) and not math.isfinite(v):
                problems.append(f"night {rec.get('night')}: non-finite {k}")
    return problems


def fuzz_one(seed: int, scheduler: str = "chorus",
             check_determinism: bool = False) -> dict:
    """Run one randomized scenario; returns {seed, violations, nights, ...}."""
    sc = random_scenario(seed)
    out = {"seed": seed, "n_nodes": sc.n_nodes, "nights": sc.nights,
           "violations": []}
    try:
        world = build_world(sc, seed)
        result = run_world(world, scheduler)
        summary = metrics_mod.summarize_run(result)
        out["accepted_per_night"] = summary.get("accepted_per_night")
        out["violations"] += _scan_summary(summary)
        out["violations"] += _night_scan(result)
        if check_determinism:
            world2 = build_world(sc, seed)
            summary2 = metrics_mod.summarize_run(run_world(world2, scheduler))
            if json.dumps(summary, sort_keys=True) != json.dumps(
                    summary2, sort_keys=True):
                diff = [k for k in summary
                        if summary.get(k) != summary2.get(k)]
                out["violations"].append(
                    f"non-deterministic replay (differs in: {diff[:8]})")
    except Exception as exc:
        out["violations"].append(f"engine raised {type(exc).__name__}: {exc}")
        out["traceback"] = traceback.format_exc()[-2500:]
    return out
