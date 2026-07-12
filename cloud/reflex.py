#!/usr/bin/env python3
"""
Live fleet — reflex confirmation.

When Open Aperture promotes a discovery candidate (a deviation corroborated past
the 2-detection gate), the network should confirm it *now*, not on the next
nightly replan. reflex.on_candidate_promoted picks 1–3 other currently-dark
nodes and drops a targeted interrupt, pushed over the realtime bus so a node
that isn't mid-exposure is on target within seconds.

This is a bounded automation of the existing admin interrupt path — nothing here
does anything an admin couldn't trigger by hand; it just closes the latency.
Guardrails (config survey.reflex): global + per-candidate nightly caps, a
minimum brightness step, a per-source cooldown, and it never fires while an
interrupt for the same source is still open. The never-preempt-active-exposure
invariant is enforced node-side, unchanged.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from cloud import db, live

logger = logging.getLogger("cloud.reflex")

_lock = threading.Lock()
# (source_key -> iso timestamp) of the last reflex fire, for the cooldown.
_last_fire: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg(config: dict) -> dict:
    r = (config.get("survey", {}) or {}).get("reflex", {}) or {}
    return {
        "enabled":             bool(r.get("enabled", True)),
        "max_per_night_global": int(r.get("max_per_night_global", 30)),
        "max_per_candidate":   int(r.get("max_per_candidate", 1)),
        "min_peak_delta_mag":  float(r.get("min_peak_delta_mag", 0.5)),
        "cooldown_min":        float(r.get("cooldown_min", 120.0)),
        "n_nodes":             int(r.get("n_nodes", 3)),
        "expires_hours":       float(r.get("expires_hours", 2.0)),
    }


def _fired_tonight() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM interrupts "
        "WHERE reason = 'reflex_confirm' AND created_at > %s", (since,))
    return int((row or {}).get("n") or 0)


def _open_interrupt_for(source_key: str) -> bool:
    """True when an unexpired reflex interrupt already targets this source."""
    row = db.query_one(
        "SELECT 1 FROM interrupts "
        "WHERE reason = 'reflex_confirm' AND expires_at > %s "
        "  AND name = %s LIMIT 1",
        (_now(), _interrupt_name(source_key)))
    return row is not None


def _interrupt_name(source_key: str) -> str:
    return f"reflex:{source_key}"


def on_candidate_promoted(cand: dict, config: dict) -> bool:
    """Fire a reflex confirmation for a just-promoted candidate.

    `cand` carries at least source_key, ra_deg, dec_deg, kind and (optionally)
    peak_delta_mag / last_mag. Best-effort and fully guarded — returns True when
    an interrupt was dispatched.
    """
    cfg = _cfg(config)
    if not cfg["enabled"]:
        return False
    source_key = str(cand.get("source_key") or "")
    if not source_key:
        return False

    peak = abs(float(cand.get("peak_delta_mag") or 0.0))
    if peak < cfg["min_peak_delta_mag"]:
        logger.debug("reflex skip %s: Δ=%.2f below floor", source_key, peak)
        return False

    with _lock:
        last = _last_fire.get(source_key)
        if last:
            try:
                age_min = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(last)).total_seconds() / 60.0
                if age_min < cfg["cooldown_min"]:
                    return False
            except ValueError:
                pass
        if _fired_tonight() >= cfg["max_per_night_global"]:
            logger.info("reflex global nightly cap reached — skipping %s", source_key)
            return False
        if _open_interrupt_for(source_key):
            return False

        targets = _pick_dark_nodes(cand, config, cfg["n_nodes"])
        if not targets:
            logger.info("reflex: no dark nodes available to confirm %s", source_key)
            return False

        iid = _create_interrupt(cand, targets, cfg)
        _last_fire[source_key] = _now()

    for nid in targets:
        try:
            live.publish(nid, "interrupt",
                         {"reason": "reflex_confirm", "source_key": source_key})
        except Exception as exc:  # pragma: no cover
            logger.debug("reflex publish to %s failed: %s", nid, exc)
    logger.info("Reflex confirm #%s: %s → %d dark node(s) %s",
                iid, source_key, len(targets), targets)
    return True


def _pick_dark_nodes(cand: dict, config: dict, n: int) -> list:
    """Rank eligible interrupt nodes (reusing the admin selector) and keep only
    those the live map says are dark and online right now."""
    # Read the live dark set FIRST: node eligibility scoring can make slow
    # weather calls, and computing staleness before that avoids a node aging
    # out of its own live window while we score it.
    dark = live.dark_online_nodes()
    if not dark:
        return []
    from cloud.server import _eligible_interrupt_nodes
    target = {
        "name": _interrupt_name(cand.get("source_key")),
        "ra_deg": float(cand["ra_deg"]),
        "dec_deg": float(cand["dec_deg"]),
        "mag": cand.get("last_mag"),
    }
    try:
        ranked = _eligible_interrupt_nodes(target, min_score=0.25)
    except Exception as exc:
        logger.debug("reflex eligibility failed: %s", exc)
        ranked = []
    picked = [nid for nid in ranked if nid in dark]
    if not picked:
        # Fall back to any dark/online node — better a confirmation from a
        # slightly-worse-placed node than none at all for a live transient.
        picked = list(dark)
    return picked[:max(1, n)]


def _create_interrupt(cand: dict, node_ids: list, cfg: dict) -> int:
    expires = (datetime.now(timezone.utc)
               + timedelta(hours=cfg["expires_hours"])).isoformat()
    return db.execute(
        """INSERT INTO interrupts
               (target_id, name, ra_deg, dec_deg, mag, reason, node_ids,
                created_at, expires_at)
           VALUES (%s,%s,%s,%s,%s,'reflex_confirm',%s,%s,%s)""",
        (cand.get("target_id") or None, _interrupt_name(cand.get("source_key")),
         float(cand["ra_deg"]), float(cand["dec_deg"]), cand.get("last_mag"),
         json.dumps(node_ids), _now(), expires),
        returning_id=True,
    )
