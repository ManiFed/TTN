#!/usr/bin/env python3
"""
Synthetic weather for the digital twin: correlated regional truth plus an
imperfect forecast — the two quantities production gets from Open-Meteo/7timer
(forecast) and from what actually happened (truth, seen only in outcomes).

Model (all knobs explicit, all draws seeded):

  night driver    z = √ρ·z_region + √(1−ρ)·z_site,   z_* ~ N(0,1)
                  ρ = regional_correlation (bad_weather_week raises it)
  night clearness p_night = logistic(logit(climatology) + spread·z)
  slot truth      2-state Markov chain, stationary prob p_night,
                  persistence 0.85 (≈2 h weather coherence at 15-min slots)
  forecast        f_slot = clamp(skill·truth_local_mean + (1−skill)·clim
                                 + N(0, 0.18·(1−skill)) + bias, 0.02, 0.98)

`skill` is the node's forecast_skill truth parameter: 1 → the forecast is a
smoothed version of what will happen; 0 → the forecast is climatology plus
noise.  `bias` lets scenarios model chronically optimistic forecasts
(the coastal-microclimate failure mode from CHORUS.md §9.5).

Weather for a node depends only on (seed, night, region, node_id) — adding or
removing other nodes never changes it, which the guardrail tests rely on.
"""

import math
import random
from dataclasses import dataclass

from sim.world import region_clear_prob, sub_rng

SLOT_PERSISTENCE = 0.85     # P(next slot keeps this slot's state)
NIGHT_SPREAD = 1.6          # logit-scale spread of nightly clearness draws
FORECAST_NOISE = 0.18
FORECAST_SMOOTH_SLOTS = 4   # ±1 h local mean the forecast "sees"


@dataclass
class NightWeather:
    truth_clear: list          # bool per slot — what the sky actually did
    forecast_clear: list       # float per slot — raw clear prob the planner sees
    p_night: float             # latent nightly clearness (diagnostics)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


def _logistic(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def night_weather(seed: int, night: int, node, n_slots: int, month: int, *,
                  regional_correlation: float = 0.5,
                  climate_overrides: dict = None,
                  forecast_skill_scale: float = 1.0,
                  forecast_bias: float = 0.0) -> NightWeather:
    """Truth + forecast for one node on one night."""
    clim = region_clear_prob(node.region, month, climate_overrides)
    rho = min(max(regional_correlation, 0.0), 0.99)

    rng_region = sub_rng(seed, "wx-region", night, node.region)
    rng_site = sub_rng(seed, "wx-site", night, node.node_id)
    z = (math.sqrt(rho) * rng_region.gauss(0, 1)
         + math.sqrt(1.0 - rho) * rng_site.gauss(0, 1))
    p_night = _logistic(_logit(clim) + NIGHT_SPREAD * z)

    # Slot-level truth: Markov chain with stationary prob p_night.
    # stay-clear and stay-cloudy transition probs chosen so the stationary
    # distribution is exactly p_night at persistence SLOT_PERSISTENCE.
    truth = []
    state = rng_site.random() < p_night
    for _ in range(max(1, n_slots)):
        truth.append(state)
        flip_base = 1.0 - SLOT_PERSISTENCE
        p_to_clear = flip_base * p_night * 2.0
        p_to_cloud = flip_base * (1.0 - p_night) * 2.0
        r = rng_site.random()
        state = (r >= p_to_cloud) if state else (r < p_to_clear)

    # Forecast: what the planner is told.
    skill = min(max(node.forecast_skill * forecast_skill_scale, 0.0), 1.0)
    forecast = []
    for s in range(len(truth)):
        lo = max(0, s - FORECAST_SMOOTH_SLOTS)
        hi = min(len(truth), s + FORECAST_SMOOTH_SLOTS + 1)
        local = sum(1.0 for c in truth[lo:hi] if c) / (hi - lo)
        f = (skill * local + (1.0 - skill) * clim
             + rng_site.gauss(0.0, FORECAST_NOISE * (1.0 - skill))
             + forecast_bias)
        forecast.append(min(max(f, 0.02), 0.98))
    return NightWeather(truth_clear=truth, forecast_clear=forecast,
                        p_night=round(p_night, 4))
