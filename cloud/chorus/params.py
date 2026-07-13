#!/usr/bin/env python3
"""
CHORUS hyperparameters — the Ring-1 tunable group.

These are the ~15 genuinely judgment-shaped knobs of the CHORUS objective
(CHORUS.md §7).  They are registered with cloud.tuning as the "chorus" scalar
group: seeded from config.yaml (scheduler.chorus_params), stored live in
tuning_state, trust-region clamped per run, and — unlike every other group —
gated by a deterministic counterfactual backtest before a change is applied.

This module is imported by cloud.tuning, so it must stay stdlib-only with no
cloud imports (same contract as cloud.objective's default dicts).
"""

DEFAULTS = {
    # ── information model ────────────────────────────────────────────────────
    "kernel_time_scale_h":     3.0,    # temporal locality of a sample outside its cell (h)
    "kernel_phase_scale":      0.04,   # phase locality for periodic classes (phase units)
    "value_scale_eb":          1.0,    # class value multipliers on nu_raw
    "value_scale_exoplanet":   1.0,    # was 1.4 — parity now that transit opps clear
                                       # the same eps eligibility gate as everything else
    "value_scale_cv":          1.0,
    "value_scale_transient":   1.3,
    "value_scale_lpv":         0.5,
    "value_scale_default":     0.8,
    # ── horizon / scarcity ───────────────────────────────────────────────────
    "scarcity_gamma":          0.93,   # nightly discount on future capture chances
    "scarcity_horizon_nights": 45.0,   # how far ahead the T1 sweep looks
    "scarcity_urgency_power":  1.0,    # >1 sharpens S's linear value-scaling into a convex
                                       # last-chance curve — a target at S=0.98 (days left)
                                       # separates further from one at S=0.7 (recapturable
                                       # for months) than the raw multiplier alone would.
                                       # 1.0 = today's linear behavior, unchanged.
    # ── uncertainty & coordination conservatism ──────────────────────────────
    "same_site_repeat_factor": 0.25,   # weather-correlation cap: same-node repeats on a
                                       # cell can't harvest weather-survival (§4.3)
    "weather_corr_radius_km":  50.0,   # ground distance below which a DIFFERENT node's
                                       # repeat on a cell is treated as increasingly
                                       # weather-correlated too (interpolates toward
                                       # same_site_repeat_factor as distance -> 0)
    "exploration_beta":        0.15,   # fleet-learning appetite (§4.2)
    # ── solver ───────────────────────────────────────────────────────────────
    "min_marginal":            0.02,   # greedy stop threshold (epsilon)
    "max_obs_per_target":      4.0,    # portfolio cap: placements per target per cycle
}

KEYS = tuple(DEFAULTS.keys())

# Safe bounds for the trust-region clamp (mirrors tuning.COORD_BOUNDS style).
BOUNDS = {
    "kernel_time_scale_h":     (0.5, 24.0),
    "kernel_phase_scale":      (0.01, 0.25),
    "value_scale_eb":          (0.1, 3.0),
    "value_scale_exoplanet":   (0.1, 3.0),
    "value_scale_cv":          (0.1, 3.0),
    "value_scale_transient":   (0.1, 3.0),
    "value_scale_lpv":         (0.05, 3.0),
    "value_scale_default":     (0.1, 3.0),
    "scarcity_gamma":          (0.70, 0.99),
    "scarcity_horizon_nights": (7.0, 90.0),
    "scarcity_urgency_power":  (1.0, 4.0),
    "same_site_repeat_factor": (0.0, 1.0),
    "weather_corr_radius_km":  (0.0, 300.0),
    "exploration_beta":        (0.0, 1.0),
    "min_marginal":            (0.0, 0.25),
    "max_obs_per_target":      (1.0, 12.0),
}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def merged(overrides: dict | None) -> dict:
    """DEFAULTS layered under a partial override dict, coerced to float."""
    out = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def value_scale(params: dict, family: str) -> float:
    """Class value multiplier for a template family (see cells.family_of)."""
    return float(params.get(f"value_scale_{family}",
                            params.get("value_scale_default",
                                       DEFAULTS["value_scale_default"])))
