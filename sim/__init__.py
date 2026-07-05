"""
Fleet digital twin for The Telescope Net.

A deterministic, reproducible simulation and backtesting environment that
forecasts network performance at 5–1,000 nodes, compares CHORUS against
baseline schedulers on identical synthetic nights, and produces science-yield
projections.

Design contract:

  * The production CHORUS solver (cloud.chorus.assign.assign) and the legacy
    network optimizer (cloud.network_planner.assign_network) are used
    UNCHANGED — the twin only replaces their inputs (DB rows, live weather,
    astropy ephemerides) with synthetic, seeded equivalents.
  * No live API calls, no database, no telescope hardware, no LLM anywhere.
  * Same seed + same scenario → byte-identical results.
  * Every stochastic assumption lives in sim.world / sim.weather /
    sim.outcomes as an explicit, documented, configurable parameter.

Entry point:  python -m sim run --scenario beta_5_nodes --nights 14 --seed 42
"""

__all__ = ["skymath", "world", "weather", "scenarios", "engine",
           "schedulers", "outcomes", "metrics", "report"]
