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

from cloud import db

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


# ── Active weight source-of-truth (read path — keep dependency-free) ─────────────

def _load_state_weights() -> dict | None:
    """Return the active obs-weights dict from tuning_state, or None if unseeded."""
    row = db.query_one("SELECT obs_weights FROM tuning_state WHERE id = 1")
    if not row:
        return None
    weights = db.loads(row.get("obs_weights"), {})
    return weights or None


def _write_state_weights(weights: dict) -> None:
    db.execute(
        """INSERT INTO tuning_state (id, obs_weights, updated_at)
           VALUES (1, %s, %s)
           ON CONFLICT(id) DO UPDATE SET
               obs_weights = excluded.obs_weights,
               updated_at  = excluded.updated_at""",
        (json.dumps(weights), _now()),
    )


def active_obs_weights(config: dict) -> dict:
    """
    The live observability sub-weights, read fresh from the DB.

    On first call the table is seeded from config.yaml
    (scoring.observability_weights) layered over DEFAULT_OBS_WEIGHTS, so behavior
    is identical to the pre-tuning system until the monitor changes anything.
    """
    db_w = _load_state_weights()
    if db_w is None:
        seed = {**DEFAULT_OBS_WEIGHTS,
                **(config.get("scoring", {}).get("observability_weights", {}) or {})}
        # only keep the canonical keys
        seed = {k: float(seed.get(k, DEFAULT_OBS_WEIGHTS[k])) for k in OBS_KEYS}
        _write_state_weights(seed)
        logger.info("Seeded tuning_state observability weights from config")
        return seed
    return {k: float(db_w.get(k, DEFAULT_OBS_WEIGHTS[k])) for k in OBS_KEYS}


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

    # Node tier lookup, for the telescope-match signal.
    tiers = {n["node_id"]: int(n.get("tier", 1) or 1)
             for n in db.query("SELECT node_id, tier FROM nodes")}

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
        },
    }


# ── Claude proposal (the only LLM call in the cloud) ─────────────────────────────

_SYSTEM_PROMPT = (
    "You tune the six observability sub-weights of an autonomous-telescope "
    "scheduler for a volunteer astronomy charity. These weights blend into the "
    "'observe' component of each target's score; higher weight means that factor "
    "matters more when choosing what to observe.\n\n"
    "The weights are:\n"
    "- light_pollution: penalty for faint targets under bright skies\n"
    "- weather: forecast clear-sky fraction over the night\n"
    "- moon: penalty for bright moon near the target\n"
    "- airmass: preference for targets near the zenith\n"
    "- window: preference for longer visibility windows\n"
    "- telescope: aperture/FoV suitability for the target class\n\n"
    "You are given the current weights and an evidence brief. Under "
    "'per_factor' each weight has a directly relevant outcome signal with its "
    "own sample count (n_*). Reason factor by factor:\n"
    "- Raise a weight when its factor's outcomes are poor and well-sampled "
    "(e.g. high outlier rate for faint targets -> light_pollution; low "
    "plan_completion_rate -> weather; worse uncertainty under bright moon -> "
    "moon; worse uncertainty at high airmass -> airmass).\n"
    "- Discount any signal with a small sample (low n_*) — do not move a weight "
    "on a handful of measurements.\n"
    "- If the evidence is weak or mixed, return the current weights unchanged.\n"
    "Make small, evidence-justified moves only. Each weight must stay within the "
    "stated max_delta of its current value and remain non-negative. They need "
    "not sum to 1 — the system renormalizes. Explain your reasoning in 2-3 "
    "sentences, citing the specific signals you acted on."
)

_WEIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "weights": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in OBS_KEYS},
            "required": list(OBS_KEYS),
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["weights", "rationale"],
    "additionalProperties": False,
}


def _resolve_api_key(cfg: dict) -> str:
    return str(cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")).strip()


def propose_weights(evidence: dict, current_weights: dict, config: dict):
    """
    Ask Claude for adjusted weights.  Returns (weights_dict, rationale) or None
    if tuning can't run (no API key).  Raises on a genuine API failure so the
    caller's guard logs it and leaves the weights unchanged.
    """
    cfg = config.get("tuning", {})
    api_key = _resolve_api_key(cfg)
    if not api_key:
        logger.warning("Tuning skipped — no API key (tuning.api_key / ANTHROPIC_API_KEY)")
        return None

    import anthropic  # lazy: only needed when tuning is enabled and keyed

    model = str(cfg.get("model", _MODEL_DEFAULT))
    max_delta = float(cfg.get("max_delta", _MAX_DELTA_DEFAULT))

    brief = {
        "current_weights": current_weights,
        "max_delta": max_delta,
        "evidence": evidence,
    }
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(brief, indent=2)}],
        output_config={"format": {"type": "json_schema", "schema": _WEIGHTS_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    return data["weights"], data.get("rationale", "")


# ── Apply + audit + notify ───────────────────────────────────────────────────────

def _clamp_and_normalize(current: dict, proposed: dict, max_delta: float) -> dict:
    """
    Take a bounded step from `current` toward `proposed`, returning weights that
    sum to 1.0 with **every** weight within max_delta of its current value.

    This is a uniform trust-region step rather than per-key clamping:
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
        s = sum(max(0.0, float(d.get(k, 0.0))) for k in OBS_KEYS)
        if s <= 0:
            return None
        return {k: max(0.0, float(d.get(k, 0.0))) / s for k in OBS_KEYS}

    cur = _norm({k: current.get(k, DEFAULT_OBS_WEIGHTS[k]) for k in OBS_KEYS})
    prop = _norm(proposed)
    if cur is None:
        cur = dict(DEFAULT_OBS_WEIGHTS)
    if prop is None:
        return {k: round(cur[k], 4) for k in OBS_KEYS}

    direction = {k: prop[k] - cur[k] for k in OBS_KEYS}
    max_abs = max(abs(d) for d in direction.values())
    factor = min(1.0, max_delta / max_abs) if max_abs > 0 else 0.0
    new = {k: round(cur[k] + factor * direction[k], 4) for k in OBS_KEYS}
    # Absorb the 4-dp rounding residual into the largest weight so the applied
    # weights sum to exactly 1.0 (the residual is <= 6e-4, well under max_delta).
    residual = round(1.0 - sum(new.values()), 4)
    if residual:
        kmax = max(OBS_KEYS, key=lambda k: new[k])
        new[kmax] = round(new[kmax] + residual, 4)
    return new


def _is_material(old: dict, new: dict, eps: float) -> bool:
    """True if any weight moved more than eps — used to skip no-op churn."""
    return any(abs(float(new[k]) - float(old.get(k, DEFAULT_OBS_WEIGHTS[k]))) > eps
               for k in OBS_KEYS)


def apply_and_notify(current: dict, proposed: dict, rationale: str,
                     evidence: dict, config: dict) -> dict:
    """Clamp/normalize, persist active weights, write an audit row, notify admins."""
    cfg = config.get("tuning", {})
    max_delta = float(cfg.get("max_delta", _MAX_DELTA_DEFAULT))
    min_change = float(cfg.get("min_change", _MIN_CHANGE_DEFAULT))
    model = str(cfg.get("model", _MODEL_DEFAULT))

    new_weights = _clamp_and_normalize(current, proposed, max_delta)

    # No-churn guard: if the monitor effectively left the weights alone, don't
    # write state, an audit row, or notifications every single night.
    if not _is_material(current, new_weights, min_change):
        logger.info("Tuning: no material weight change (<%.3f) — left unchanged", min_change)
        return current

    _write_state_weights(new_weights)
    db.execute(
        """INSERT INTO weight_history
               (changed_at, old_weights, new_weights, rationale,
                evidence_digest, model, applied)
           VALUES (%s,%s,%s,%s,%s,%s,1)""",
        (_now(), json.dumps(current), json.dumps(new_weights), rationale,
         json.dumps(evidence), model),
    )
    _notify_admins(current, new_weights, rationale)
    logger.info("Applied tuned observability weights: %s (%s)", new_weights, rationale)
    return new_weights


def restore_weights(weights: dict, rationale: str, config: dict) -> dict:
    """Set the active weights exactly (no clamping) — used by admin rollback."""
    current = active_obs_weights(config)
    restored = {k: float(weights.get(k, current.get(k, DEFAULT_OBS_WEIGHTS[k])))
                for k in OBS_KEYS}
    _write_state_weights(restored)
    db.execute(
        """INSERT INTO weight_history
               (changed_at, old_weights, new_weights, rationale,
                evidence_digest, model, applied)
           VALUES (%s,%s,%s,%s,%s,%s,1)""",
        (_now(), json.dumps(current), json.dumps(restored), rationale,
         "{}", "manual"),
    )
    _notify_admins(current, restored, rationale)
    logger.info("Restored observability weights: %s (%s)", restored, rationale)
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
        current = active_obs_weights(config)
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
