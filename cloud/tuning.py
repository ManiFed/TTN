#!/usr/bin/env python3
"""
Scoring weight monitor — the one place Claude touches the cloud.

Everything in the hot path (alert ingestion, scoring, scheduling) is plain
procedural code with fixed formulas.  Claude is used *only* here, and *only*
as an advisor: once a night it reviews aggregated observing outcomes and
proposes new values for the six observability sub-weights.  It never scores a
target, ingests an alert, or builds a plan.

Pipeline (run_nightly):

    gather_evidence(config)        pure procedural — aggregate the last N nights
                                   of outcomes into a compact factual brief
    propose_weights(evidence, …)   the single Claude call — returns 6 weights
    apply_and_notify(...)          clamp to ±max_delta, renormalize to sum 1.0,
                                   persist to the DB, audit, notify admins

The *active* weights live in the `tuning_state` table, seeded from
`config.yaml` on first use.  scoring.score_all() reads them via
active_obs_weights() on every run, so a change takes effect on the next
rescore with no process restart.

This module must not import cloud.scoring (scoring imports this) — keep the
read path (active_obs_weights / DEFAULT_OBS_WEIGHTS) dependency-free so there
is no import cycle.

Disabled by default: with tuning.enabled false (or no API key) every entry
point is a clean no-op and the weights never change.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from cloud import db, objective

logger = logging.getLogger("cloud.tuning")

# Canonical default observability sub-weights.  scoring.py imports these.
# Keep in sync with cloud/config.yaml scoring.observability_weights (that file
# is only the seed; this dict is the fallback when no seed and no DB row exist).
DEFAULT_OBS_WEIGHTS = {
    "light_pollution": 0.20,
    "weather":         0.25,
    "moon":            0.15,
    "airmass":         0.15,
    "window":          0.15,
    "telescope":       0.10,
}

OBS_KEYS = tuple(DEFAULT_OBS_WEIGHTS.keys())

# Composite (top-level) score weights — previously fixed in scoring.DEFAULT_WEIGHTS.
DEFAULT_COMPOSITE_WEIGHTS = {
    "brightness": 0.20,
    "science":    0.25,
    "time":       0.15,
    "coverage":   0.15,
    "observe":    0.25,
    "roi":        0.10,
}
COMPOSITE_KEYS = tuple(DEFAULT_COMPOSITE_WEIGHTS.keys())

# Slot-quality and coordination defaults are owned by cloud.objective (the hot
# path that consumes them); tuning re-exports them so all params share one source.
DEFAULT_SLOT_WEIGHTS = objective.DEFAULT_SLOT_WEIGHTS
SLOT_KEYS = tuple(DEFAULT_SLOT_WEIGHTS.keys())
DEFAULT_COORD_PARAMS = objective.DEFAULT_COORD_PARAMS
COORD_KEYS = tuple(DEFAULT_COORD_PARAMS.keys())

# The three weight groups are normalized (sum to 1, trust-region step). The
# coordination group is free scalars, clamped per-key within max_delta_coord and
# the bounds below.
WEIGHT_GROUPS = {
    "composite":     (DEFAULT_COMPOSITE_WEIGHTS, COMPOSITE_KEYS),
    "observability": (DEFAULT_OBS_WEIGHTS, OBS_KEYS),
    "slot_quality":  (DEFAULT_SLOT_WEIGHTS, SLOT_KEYS),
}
SCALAR_GROUP = "coordination"

COORD_BOUNDS = {
    "redundancy_decay":            (0.10, 0.95),
    "redundancy_diversity_sep":    (10.0, 180.0),
    "cadence_bonus_strength":      (0.0, 1.0),
    "slew_cost_weight":            (0.0, 5.0),
    "slew_deg_per_s":              (0.5, 10.0),
    "slew_settle_s":               (0.0, 120.0),
    "filter_change_cost_s":        (0.0, 600.0),
    "meridian_flip_cost_s":        (0.0, 600.0),
    "preferred_target_boost":      (0.0, 0.5),
    "robustness_cloud_relax":      (0.0, 0.5),
    "slot_quality_floor":          (0.0, 0.95),
    "local_search_aggressiveness": (0.0, 4.0),
}

_MAX_DELTA_COORD_DEFAULT = 0.15

_MODEL_DEFAULT = "claude-opus-4-8"
_LOOKBACK_DEFAULT = 14
_MAX_DELTA_DEFAULT = 0.05
_MIN_NIGHTS_DEFAULT = 7
_MIN_MEAS_DEFAULT = 30      # don't tune on fewer measurements than this
_MIN_CHANGE_DEFAULT = 0.005  # skip apply/notify if no weight moves more than this
_FAINT_MAG = 14.0          # split point for light-pollution analysis
_HIGH_AIRMASS = 1.5        # split point for airmass analysis


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Active param source-of-truth (read path — keep dependency-free) ──────────────

def _default_params(config: dict) -> dict:
    """All parameter groups seeded from config.yaml layered over the canonical
    defaults — the state the system runs with before the monitor changes
    anything (behavior identical to the pre-tuning system)."""
    sc = config.get("scoring", {})
    sch = config.get("scheduler", {})
    raw = {
        "composite":     {**DEFAULT_COMPOSITE_WEIGHTS, **(sc.get("weights") or {})},
        "observability": {**DEFAULT_OBS_WEIGHTS, **(sc.get("observability_weights") or {})},
        "slot_quality":  {**DEFAULT_SLOT_WEIGHTS, **(sc.get("slot_quality_weights") or {})},
        "coordination":  {**DEFAULT_COORD_PARAMS, **(sch.get("coordination") or {})},
    }
    return _canonical(raw)


def _canonical(params: dict) -> dict:
    """Keep only the known keys per group, coerced to float, filling any gap
    from the defaults."""
    out: dict = {}
    for group, (defaults, keys) in WEIGHT_GROUPS.items():
        src = params.get(group, {}) or {}
        out[group] = {k: float(src.get(k, defaults[k])) for k in keys}
    coord_src = params.get(SCALAR_GROUP, {}) or {}
    out[SCALAR_GROUP] = {k: float(coord_src.get(k, DEFAULT_COORD_PARAMS[k]))
                         for k in COORD_KEYS}
    return out


def _load_params() -> dict | None:
    row = db.query_one("SELECT params, obs_weights FROM tuning_state WHERE id = 1")
    if not row:
        return None
    stored = db.loads(row.get("params"), None)
    if stored:
        return stored
    # Continuity: an existing obs_weights-only row from the pre-network system.
    legacy = db.loads(row.get("obs_weights"), None)
    if legacy:
        return {"observability": legacy}
    return None


def _write_params(params: dict) -> None:
    """Persist all groups; mirror observability into the legacy column so any
    old reader still works."""
    db.execute(
        """INSERT INTO tuning_state (id, params, obs_weights, updated_at)
           VALUES (1, %s, %s, %s)
           ON CONFLICT(id) DO UPDATE SET
               params      = excluded.params,
               obs_weights = excluded.obs_weights,
               updated_at  = excluded.updated_at""",
        (json.dumps(params), json.dumps(params.get("observability", {})), _now()),
    )


def active_params(config: dict) -> dict:
    """
    The live tunable parameters (all groups), read fresh from the DB.

    On first call the row is seeded from config.yaml layered over the canonical
    defaults, so behavior is identical to the pre-tuning system until the monitor
    changes anything.  Stored values are merged over the seed so a newly-added
    param key picks up its default without a manual migration.
    """
    seed = _default_params(config)
    stored = _load_params()
    if stored is None:
        _write_params(seed)
        logger.info("Seeded tuning_state params from config")
        return seed
    # Merge stored over seed (per group/key), then canonicalize.
    merged = {g: {**seed.get(g, {}), **(stored.get(g, {}) or {})}
              for g in ("composite", "observability", "slot_quality", SCALAR_GROUP)}
    return _canonical(merged)


def active_obs_weights(config: dict) -> dict:
    """The live observability sub-weights (one group of active_params)."""
    return active_params(config)["observability"]


def active_composite_weights(config: dict) -> dict:
    """The live composite (top-level) score weights."""
    return active_params(config)["composite"]


# ── Evidence gathering (pure procedural, no LLM) ─────────────────────────────────

def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def gather_evidence(config: dict) -> dict:
    """
    Aggregate the last `tuning.lookback_nights` of observing outcomes into a
    compact, factual brief.  This is the only input Claude sees.  Pure SQL +
    arithmetic — no scoring or LLM logic here.

    The brief is structured so each tunable weight has a directly relevant
    outcome signal under `per_factor`, each with its own sample count, so the
    monitor can reason factor-by-factor and ignore signals backed by too little
    data.
    """
    cfg = config.get("tuning", {})
    lookback = int(cfg.get("lookback_nights", _LOOKBACK_DEFAULT))
    since = (datetime.now(timezone.utc) - timedelta(days=lookback)).isoformat()

    meas = db.query(
        """SELECT target_name, node_id, magnitude, uncertainty, airmass, fwhm,
                  snr, quality_flag, validation_status, conditions, received_at
           FROM measurements
           WHERE received_at >= %s""",
        (since,),
    )
    n_total = len(meas)
    nights = {m["received_at"][:10] for m in meas if m.get("received_at")}
    observed_targets = {m["target_name"] for m in meas if m.get("target_name")}

    quality = {"good": 0, "acceptable": 0, "poor": 0}
    for m in meas:
        q = m.get("quality_flag", "poor")
        quality[q] = quality.get(q, 0) + 1
    n_outlier = sum(1 for m in meas if m.get("validation_status") == "outlier")

    # Node tier + camera lookup, for the telescope-match and thermal signals.
    node_rows = db.query("SELECT node_id, tier, cooled_camera FROM nodes")
    tiers = {n["node_id"]: int(n.get("tier", 1) or 1) for n in node_rows}
    cooled = {n["node_id"]: bool(n.get("cooled_camera")) for n in node_rows}

    # ── Split helpers: bucket measurements by the factor each weight governs ──
    def split_outlier(predicate):
        on = [m for m in meas if predicate(m)]
        n_out = sum(1 for m in on if m.get("validation_status") == "outlier")
        return _rate(n_out, len(on)), len(on)

    def split_uncertainty(predicate):
        vals = [m["uncertainty"] for m in meas
                if predicate(m) and m.get("uncertainty") is not None]
        return _mean(vals), len(vals)

    def moon_illum(m):
        return db.loads(m.get("conditions"), {}).get("moon_illumination")

    faint_out, n_faint = split_outlier(
        lambda m: m.get("magnitude") is not None and m["magnitude"] >= _FAINT_MAG)
    bright_out, n_bright = split_outlier(
        lambda m: m.get("magnitude") is not None and m["magnitude"] < _FAINT_MAG)

    unc_bright_moon, n_bm = split_uncertainty(
        lambda m: moon_illum(m) is not None and float(moon_illum(m)) >= 0.5)
    unc_dark_moon, n_dm = split_uncertainty(
        lambda m: moon_illum(m) is not None and float(moon_illum(m)) < 0.5)

    unc_high_am, n_ha = split_uncertainty(
        lambda m: m.get("airmass") is not None and m["airmass"] >= _HIGH_AIRMASS)
    unc_low_am, n_la = split_uncertainty(
        lambda m: m.get("airmass") is not None and m["airmass"] < _HIGH_AIRMASS)

    # Plan vs. observed completion (per night), the main weather/window proxy.
    plans = db.query(
        "SELECT plan_json, night FROM plans WHERE generated_at >= %s", (since,))
    planned_targets: set = set()
    per_night_completion: list = []
    for p in plans:
        items = db.loads(p.get("plan_json"), {}).get("items", [])
        names = {(it or {}).get("target") for it in items if (it or {}).get("target")}
        planned_targets |= names
        if names:
            per_night_completion.append(len(names & observed_targets) / len(names))
    completion_rate = _rate(len(planned_targets & observed_targets), len(planned_targets))
    low_completion_nights = sum(1 for c in per_night_completion if c < 0.5)

    # Telescope match: good-data fraction by node tier.
    good_by_tier: dict[int, list] = {}
    for m in meas:
        t = tiers.get(m.get("node_id"), 1)
        good_by_tier.setdefault(t, []).append(1 if m.get("quality_flag") == "good" else 0)
    tier_good_fraction = {
        str(t): {"good_fraction": round(sum(v) / len(v), 4), "n": len(v)}
        for t, v in sorted(good_by_tier.items())}

    # Observability context from the live scores table.
    scores = db.query("SELECT components FROM scores")
    vis_minutes, observe_scores = [], []
    for s in scores:
        comp = db.loads(s.get("components"), {})
        if comp.get("visibility_minutes") is not None:
            vis_minutes.append(float(comp["visibility_minutes"]))
        if comp.get("observe") is not None:
            observe_scores.append(float(comp["observe"]))

    # Good-data fraction by camera cooling — the thermal slot-quality signal.
    cooled_good: dict[bool, list] = {}
    for m in meas:
        c = cooled.get(m.get("node_id"), False)
        cooled_good.setdefault(c, []).append(1 if m.get("quality_flag") == "good" else 0)
    cooled_good_fraction = {
        ("cooled" if c else "uncooled"): {
            "good_fraction": round(sum(v) / len(v), 4), "n": len(v)}
        for c, v in cooled_good.items()}

    # Network coordination outcomes from recent optimizer runs (plan_runs).
    runs = db.query("SELECT * FROM plan_runs WHERE ran_at >= %s", (since,))
    network = {
        "n_runs": len(runs),
        "mean_redundancy_rate": _mean([r.get("redundancy_rate") for r in runs]),
        "mean_cadence_fill": _mean([r.get("cadence_fill") for r in runs]),
        "mean_objective": _mean([r.get("objective_value") for r in runs]),
        "mean_greedy_objective": _mean([r.get("greedy_objective") for r in runs]),
        "mean_assignments": _mean([r.get("n_assignments") for r in runs]),
    }

    return {
        "lookback_nights": lookback,
        "n_nights_with_data": len(nights),
        "n_measurements": n_total,
        "n_targets_observed": len(observed_targets),
        "overall": {
            "quality_counts": quality,
            "outlier_rate": _rate(n_outlier, n_total) or 0.0,
            "mean_uncertainty": _mean([m.get("uncertainty") for m in meas]),
            "mean_fwhm": _mean([m.get("fwhm") for m in meas]),
            "mean_airmass": _mean([m.get("airmass") for m in meas]),
            "mean_snr": _mean([m.get("snr") for m in meas]),
        },
        # Each entry is the outcome signal for the like-named weight, with its
        # own sample size so weak evidence can be discounted.
        "per_factor": {
            "light_pollution": {
                "faint_outlier_rate": faint_out, "n_faint": n_faint,
                "bright_outlier_rate": bright_out, "n_bright": n_bright,
                "faint_mag_threshold": _FAINT_MAG},
            "weather": {
                "plan_completion_rate": completion_rate,
                "n_planned_targets": len(planned_targets),
                "low_completion_nights": low_completion_nights,
                "n_nights_planned": len(per_night_completion)},
            "moon": {
                "uncertainty_bright_moon": unc_bright_moon, "n_bright_moon": n_bm,
                "uncertainty_dark_moon": unc_dark_moon, "n_dark_moon": n_dm},
            "airmass": {
                "uncertainty_high_airmass": unc_high_am, "n_high": n_ha,
                "uncertainty_low_airmass": unc_low_am, "n_low": n_la,
                "airmass_threshold": _HIGH_AIRMASS},
            "window": {
                "mean_visibility_minutes": _mean(vis_minutes),
                "mean_observability_score": _mean(observe_scores),
                "plan_completion_rate": completion_rate},
            "telescope": {
                "good_fraction_by_tier": tier_good_fraction},
            "thermal": {
                "good_fraction_by_cooling": cooled_good_fraction},
        },
        # Fleet-level coordination outcomes, for the composite weights and the
        # coordination knobs (redundancy / cadence / slew model).
        "network": network,
    }


# ── Claude proposal (the only LLM call in the cloud) ─────────────────────────────

_SYSTEM_PROMPT = (
    "You tune the parameters of an autonomous-telescope network scheduler for a "
    "volunteer astronomy charity. The hot path is procedural; you are an advisor "
    "that, once a night, proposes small adjustments to four parameter groups:\n\n"
    "composite — relative importance of each target's score components "
    "(brightness, science, time, coverage, observe, roi).\n"
    "observability — sub-weights blended into the 'observe' component "
    "(light_pollution, weather, moon, airmass, window, telescope).\n"
    "slot_quality — weights for the time-resolved within-night placement of an "
    "observation (altitude, cloud, seeing, transparency, moon, sky_brightness, "
    "thermal).\n"
    "coordination — fleet-wide knobs: redundancy_decay (lower = punish redundant "
    "double-coverage of a target harder), cadence_bonus_strength (higher = spread "
    "samples in time more), slew/filter/meridian overhead costs, "
    "preferred_target_boost, robustness_cloud_relax, slot_quality_floor, "
    "local_search_aggressiveness, plus slew_deg_per_s and "
    "redundancy_diversity_sep.\n\n"
    "You are given the current params and an evidence brief. Under 'per_factor' "
    "each observability/slot weight has a directly relevant outcome signal with "
    "its own sample count (n_*); 'network' has fleet outcomes (redundancy_rate, "
    "cadence_fill, objective vs greedy). Reason group by group, factor by factor:\n"
    "- Raise a weight/knob only when its signal is poor AND well-sampled (e.g. "
    "high faint-target outlier rate -> light_pollution; low plan_completion -> "
    "weather; worse uncertainty under bright moon -> moon or slot_quality.moon; "
    "worse good_fraction for uncooled cameras -> slot_quality.thermal; high "
    "redundancy_rate with low cadence_fill -> lower redundancy_decay and/or raise "
    "cadence_bonus_strength).\n"
    "- Discount any signal with a small sample. If evidence is weak or mixed for a "
    "group, return that group unchanged.\n"
    "Make small, evidence-justified moves only. Weight groups need not sum to 1 — "
    "the system renormalizes each and caps every per-run move at max_delta. "
    "Coordination knobs are clamped to max_delta_coord (fractional) and to safe "
    "bounds. Explain your reasoning in 2-4 sentences citing the signals you acted on."
)


def _group_schema(keys) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": "number"} for k in keys},
        "required": list(keys),
        "additionalProperties": False,
    }


_PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "params": {
            "type": "object",
            "properties": {
                "composite":     _group_schema(COMPOSITE_KEYS),
                "observability": _group_schema(OBS_KEYS),
                "slot_quality":  _group_schema(SLOT_KEYS),
                "coordination":  _group_schema(COORD_KEYS),
            },
            "required": ["composite", "observability", "slot_quality", "coordination"],
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["params", "rationale"],
    "additionalProperties": False,
}


def _resolve_api_key(cfg: dict) -> str:
    return str(cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")).strip()


def propose_weights(evidence: dict, current_params: dict, config: dict):
    """
    Ask Claude for adjusted params (all four groups).  Returns
    (proposed_params, rationale) or None if tuning can't run (no API key).
    Raises on a genuine API failure so the caller's guard logs it and leaves the
    params unchanged.
    """
    cfg = config.get("tuning", {})
    api_key = _resolve_api_key(cfg)
    if not api_key:
        logger.warning("Tuning skipped — no API key (tuning.api_key / ANTHROPIC_API_KEY)")
        return None

    import anthropic  # lazy: only needed when tuning is enabled and keyed

    model = str(cfg.get("model", _MODEL_DEFAULT))
    max_delta = float(cfg.get("max_delta", _MAX_DELTA_DEFAULT))
    max_delta_coord = float(cfg.get("max_delta_coord", _MAX_DELTA_COORD_DEFAULT))

    brief = {
        "current_params": current_params,
        "max_delta": max_delta,
        "max_delta_coord": max_delta_coord,
        "coord_bounds": COORD_BOUNDS,
        "evidence": evidence,
    }
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=3000,
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(brief, indent=2)}],
        output_config={"format": {"type": "json_schema", "schema": _PARAMS_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    return data["params"], data.get("rationale", "")


# ── Apply + audit + notify ───────────────────────────────────────────────────────

def _clamp_and_normalize(current: dict, proposed: dict, keys, defaults: dict,
                         max_delta: float) -> dict:
    """
    Take a bounded step from `current` toward `proposed` for one weight group,
    returning weights that sum to 1.0 with **every** weight within max_delta of
    its current value.

    Uniform trust-region step rather than per-key clamping:
      1. Normalize current and proposed to sum 1 (so an un-normalized model
         response like {weather: 40, ...} is read on the right scale).
      2. direction = proposed − current  (sums to 0).
      3. Scale the whole direction by factor = min(1, max_delta / max|direction|).
      4. new = current + factor · direction.

    Because the direction sums to zero, the result still sums to 1 with no
    renormalization drift, and new_k = (1−factor)·cur_k + factor·prop_k is a
    convex combination, so it is always ≥ 0 and within max_delta of current.
    max_delta is therefore a true hard cap on the per-run change to any weight.
    """
    def _norm(d):
        s = sum(max(0.0, float(d.get(k, 0.0))) for k in keys)
        if s <= 0:
            return None
        return {k: max(0.0, float(d.get(k, 0.0))) / s for k in keys}

    cur = _norm({k: current.get(k, defaults[k]) for k in keys})
    prop = _norm(proposed)
    if cur is None:
        cur = {k: float(defaults[k]) for k in keys}
        s = sum(cur.values()) or 1.0
        cur = {k: v / s for k, v in cur.items()}
    if prop is None:
        return {k: round(cur[k], 4) for k in keys}

    direction = {k: prop[k] - cur[k] for k in keys}
    max_abs = max(abs(d) for d in direction.values())
    factor = min(1.0, max_delta / max_abs) if max_abs > 0 else 0.0
    new = {k: round(cur[k] + factor * direction[k], 4) for k in keys}
    # Absorb the 4-dp rounding residual into the largest weight so the applied
    # weights sum to exactly 1.0 (the residual is <= 6e-4, well under max_delta).
    residual = round(1.0 - sum(new.values()), 4)
    if residual:
        kmax = max(keys, key=lambda k: new[k])
        new[kmax] = round(new[kmax] + residual, 4)
    return new


def _clamp_scalars(current: dict, proposed: dict, max_delta_coord: float) -> dict:
    """
    Bounded per-key step for the coordination knobs (free scalars, not
    normalized).  Each knob moves at most max_delta_coord × |current| (with a
    small absolute floor so near-zero knobs can still move) toward the proposal,
    then is clamped to its safe bound in COORD_BOUNDS.
    """
    out: dict = {}
    for k in COORD_KEYS:
        cur = float(current.get(k, DEFAULT_COORD_PARAMS[k]))
        prop = float(proposed.get(k, cur))
        lo, hi = COORD_BOUNDS[k]
        span = (hi - lo)
        step_cap = max(max_delta_coord * abs(cur), max_delta_coord * span * 0.1)
        delta = max(-step_cap, min(step_cap, prop - cur))
        out[k] = round(max(lo, min(hi, cur + delta)), 4)
    return out


def _clamp_params(current: dict, proposed: dict, max_delta: float,
                  max_delta_coord: float) -> dict:
    """Apply the trust-region step to every group → a full canonical params dict."""
    new: dict = {}
    for group, (defaults, keys) in WEIGHT_GROUPS.items():
        new[group] = _clamp_and_normalize(
            current.get(group, {}), proposed.get(group, {}), keys, defaults, max_delta)
    new[SCALAR_GROUP] = _clamp_scalars(
        current.get(SCALAR_GROUP, {}), proposed.get(SCALAR_GROUP, {}), max_delta_coord)
    return new


def _params_material(old: dict, new: dict, eps: float) -> bool:
    """True if any weight (any group) moved more than eps. Coordination knobs use
    a per-key relative epsilon so a tiny absolute move on a large knob still
    counts only when proportionally meaningful."""
    for group, (defaults, keys) in WEIGHT_GROUPS.items():
        og, ng = old.get(group, {}), new.get(group, {})
        if any(abs(float(ng.get(k, defaults[k])) - float(og.get(k, defaults[k]))) > eps
               for k in keys):
            return True
    og, ng = old.get(SCALAR_GROUP, {}), new.get(SCALAR_GROUP, {})
    for k in COORD_KEYS:
        cur = float(og.get(k, DEFAULT_COORD_PARAMS[k]))
        scale = max(abs(cur), 1e-6)
        if abs(float(ng.get(k, cur)) - cur) / scale > eps:
            return True
    return False


def apply_and_notify(current: dict, proposed: dict, rationale: str,
                     evidence: dict, config: dict) -> dict:
    """Clamp every group, persist active params, write an audit row, notify admins."""
    cfg = config.get("tuning", {})
    max_delta = float(cfg.get("max_delta", _MAX_DELTA_DEFAULT))
    max_delta_coord = float(cfg.get("max_delta_coord", _MAX_DELTA_COORD_DEFAULT))
    min_change = float(cfg.get("min_change", _MIN_CHANGE_DEFAULT))
    model = str(cfg.get("model", _MODEL_DEFAULT))

    new_params = _clamp_params(current, proposed, max_delta, max_delta_coord)

    # No-churn guard: if the monitor effectively left the params alone, don't
    # write state, an audit row, or notifications every single night.
    if not _params_material(current, new_params, min_change):
        logger.info("Tuning: no material param change (<%.3f) — left unchanged", min_change)
        return current

    _write_params(new_params)
    db.execute(
        """INSERT INTO weight_history
               (changed_at, old_weights, new_weights, rationale,
                evidence_digest, model, applied)
           VALUES (%s,%s,%s,%s,%s,%s,1)""",
        (_now(), json.dumps(current), json.dumps(new_params), rationale,
         json.dumps(evidence), model),
    )
    _notify_admins(current, new_params, rationale)
    logger.info("Applied tuned scheduler params (%s)", rationale)
    return new_params


def restore_weights(params: dict, rationale: str, config: dict) -> dict:
    """Set the active params exactly (no clamping) — used by admin rollback.

    Accepts either a full params dict (group → weights) or a legacy flat
    observability-weights dict (wrapped automatically), so old audit rows still
    roll back correctly.
    """
    current = active_params(config)
    group_names = set(WEIGHT_GROUPS) | {SCALAR_GROUP}
    if params and not (set(params) & group_names):
        params = {"observability": params}      # legacy flat obs-weights row
    merged = {g: {**current.get(g, {}), **(params.get(g, {}) or {})}
              for g in ("composite", "observability", "slot_quality", SCALAR_GROUP)}
    restored = _canonical(merged)
    _write_params(restored)
    db.execute(
        """INSERT INTO weight_history
               (changed_at, old_weights, new_weights, rationale,
                evidence_digest, model, applied)
           VALUES (%s,%s,%s,%s,%s,%s,1)""",
        (_now(), json.dumps(current), json.dumps(restored), rationale,
         "{}", "manual"),
    )
    _notify_admins(current, restored, rationale)
    logger.info("Restored scheduler params (%s)", rationale)
    return restored


def _notify_admins(old_weights: dict, new_weights: dict, rationale: str) -> None:
    """Write a notification for every admin user (auto-applied, then notify)."""
    admins = db.query("SELECT user_id FROM users WHERE role = 'admin'")
    payload = json.dumps({
        "old_weights": old_weights,
        "new_weights": new_weights,
        "rationale": rationale,
    })
    for a in admins:
        db.execute(
            "INSERT INTO notifications (user_id, type, payload, sent_at) VALUES (%s,%s,%s,%s)",
            (a["user_id"], "weight_tuning", payload, _now()),
        )
    if admins:
        logger.info("Dispatched weight_tuning notifications to %d admin(s)", len(admins))


# ── Orchestration (called from the nightly maintenance loop) ─────────────────────

def run_nightly(config: dict) -> dict | None:
    """
    Nightly entry point.  No-op (returns None) when disabled, unkeyed, or when
    there isn't enough recent data.  Any failure is logged and leaves the active
    weights untouched.
    """
    cfg = config.get("tuning", {})
    if not cfg.get("enabled"):
        logger.debug("Tuning disabled — skipping nightly weight review")
        return None

    try:
        current = active_params(config)
        evidence = gather_evidence(config)

        min_nights = int(cfg.get("min_nights_data", _MIN_NIGHTS_DEFAULT))
        if evidence["n_nights_with_data"] < min_nights:
            logger.info(
                "Tuning skipped — only %d nights of data (need %d)",
                evidence["n_nights_with_data"], min_nights)
            return None

        min_meas = int(cfg.get("min_measurements", _MIN_MEAS_DEFAULT))
        if evidence["n_measurements"] < min_meas:
            logger.info(
                "Tuning skipped — only %d measurements (need %d)",
                evidence["n_measurements"], min_meas)
            return None

        result = propose_weights(evidence, current, config)
        if result is None:
            return None
        proposed, rationale = result
        return apply_and_notify(current, proposed, rationale, evidence, config)
    except Exception as exc:
        logger.error("Nightly tuning failed (weights unchanged): %s", exc)
        return None
