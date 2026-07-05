#!/usr/bin/env python3
"""
Named scenarios — the digital twin's experiment library.

Each scenario is a full, explicit parameterization of the synthetic world.
Nothing here is tuned to favor any scheduler: scenarios shape the WORLD
(geography, reliability, weather, target load), and every scheduler faces the
identical world.  Scaling scenarios (beta → 50 → 200 → 1000) hold the target
catalog constant so yield changes are attributable to the network, not the
science program.
"""

from dataclasses import dataclass, field, replace
from typing import Optional

from sim.world import NORTHERN_REGIONS, SOUTHERN_REGIONS


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    n_nodes: int = 5
    n_targets: int = 120
    nights: int = 14
    epoch: str = "2026-03-21T00:00:00"      # equinox: fair to both hemispheres
    # fleet shape
    regions: Optional[list] = None           # None → all regions
    region_weights: Optional[dict] = None
    hardware_mix: Optional[dict] = None
    reliability_scale: float = 1.0
    forecast_skill_range: tuple = (0.55, 0.85)
    p_night_up_range: tuple = (0.80, 0.97)
    kappa_scale: float = 1.0
    # catalog shape
    class_mix: Optional[dict] = None
    dec_bias_north: float = 0.5
    transient_rate_per_night: float = 0.15
    alert_storm_night: Optional[int] = None
    alert_storm_count: int = 6
    # weather
    regional_correlation: float = 0.5
    climate_overrides: Optional[dict] = None
    forecast_skill_scale: float = 1.0
    forecast_bias: float = 0.0
    # QC / photometry
    qc_sigma_max: float = 0.25
    catalog_failure_prob: float = 0.0
    # planner knobs (identical for all schedulers)
    max_targets_per_night: int = 12
    max_candidates_per_node: int = 60
    local_search_ms: float = 0.0             # 0 = deterministic across hosts
    chorus_overrides: Optional[dict] = None

    def variant(self, **kw) -> "Scenario":
        return replace(self, **kw)


_BAD_WEATHER_CLIMATE = {r: {"clear": 0.18, "amp": 0.02}
                        for r in ("na_southwest", "na_east", "na_west_coast",
                                  "europe_north", "europe_south", "east_asia",
                                  "india", "australia", "south_america",
                                  "southern_africa", "hawaii", "middle_east")}


SCENARIOS = {s.name: s for s in [
    Scenario(
        name="beta_5_nodes",
        description=("Early network: 5 hobbyist nodes, sparse geography "
                     "(two US regions + one European), mixed reliability. "
                     "The world The Telescope Net operates in today."),
        n_nodes=5, n_targets=120, nights=14,
        regions=["na_southwest", "na_east", "europe_north"],
        reliability_scale=0.85,
        forecast_skill_range=(0.45, 0.75),
    ),
    Scenario(
        name="launch_50_nodes",
        description=("Phase-2 target: 50 nodes, moderate global coverage, "
                     "mixed reliability, mostly Seestar-class."),
        n_nodes=50, n_targets=200, nights=14,
    ),
    Scenario(
        name="growth_200_nodes",
        description=("Mature network: 200 nodes across many longitudes; "
                     "tests coordination quality and redundancy control."),
        n_nodes=200, n_targets=300, nights=7,
    ),
    Scenario(
        name="global_1000_nodes",
        description=("Stress test: 1,000 nodes; measures scalability and "
                     "diminishing returns per node."),
        n_nodes=1000, n_targets=400, nights=3,
        max_candidates_per_node=40,
    ),
    Scenario(
        name="bad_weather_week",
        description=("A week of correlated regional cloud: climatology "
                     "collapses to ~18% clear, storms span whole regions "
                     "(correlation 0.85), and forecasts lose half their "
                     "skill.  Tests weather hedging."),
        n_nodes=50, n_targets=200, nights=7,
        climate_overrides=_BAD_WEATHER_CLIMATE,
        regional_correlation=0.85,
        forecast_skill_scale=0.5,
        forecast_bias=0.10,        # forecasts chronically optimistic in storms
    ),
    Scenario(
        name="alert_storm",
        description=("Six time-critical transients arrive on night 3 of 10, "
                     "on top of the normal alert rate.  Tests triage and "
                     "response latency."),
        n_nodes=50, n_targets=200, nights=10,
        alert_storm_night=3, alert_storm_count=6,
        transient_rate_per_night=0.2,
    ),
    Scenario(
        name="southern_gap",
        description=("All 50 nodes in the northern hemisphere; the catalog "
                     "remains all-sky.  Measures the cost of the southern "
                     "blind spot."),
        n_nodes=50, n_targets=200, nights=14,
        regions=NORTHERN_REGIONS,
    ),
    Scenario(
        name="unreliable_fleet",
        description=("50 nodes whose true execution/acceptance reliability "
                     "is scaled to 60% of normal and nightly uptime drops "
                     "toward 65%.  Tests reliability learning and hedging."),
        n_nodes=50, n_targets=200, nights=14,
        reliability_scale=0.60,
        p_night_up_range=(0.60, 0.85),
    ),
    Scenario(
        name="exoplanet_campaign",
        description=("Transit-heavy program: 30% of the catalog are transit "
                     "hosts; success needs baselines and contact coverage."),
        n_nodes=50, n_targets=200, nights=14,
        class_mix={"EXOPLANET": 0.30, "EB": 0.15, "CV": 0.12, "LPV": 0.13,
                   "VAR": 0.30},
    ),
    Scenario(
        name="photometry_quality_crisis",
        description=("Plans execute but photometry degrades: every node's "
                     "true κ (delivered-vs-physics variance ratio) triples "
                     "and 15% of measurements fail on catalog/comparison "
                     "problems.  Tests whether schedulers keep wasting "
                     "prime slots on measurements that cannot pass QC."),
        n_nodes=50, n_targets=200, nights=14,
        kappa_scale=3.0,
        catalog_failure_prob=0.15,
    ),
]}


# Southern-hemisphere-balanced reference used in comparisons/tests.
SCENARIOS["launch_50_nodes_global"] = SCENARIOS["launch_50_nodes"].variant(
    name="launch_50_nodes_global",
    description="launch_50_nodes with weight tilted toward a balanced "
                "north/south split (counterfactual for southern_gap).",
    region_weights={r: 2.0 for r in SOUTHERN_REGIONS},
)


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(f"unknown scenario '{name}' — choose from: "
                       + ", ".join(sorted(SCENARIOS)))
