"""
Discovery-candidate crossmatch — is this deviation already known?

A background worker walks discovery_candidates rows in state 'crossmatching'
(promoted by cloud.survey once the 2-detection gate passes) and cone-matches
each against:

    VSX  — AAVSO's variable star index; a match closes the candidate as
           known_vsx and permanently suppresses the source in survey_sources
    TNS  — the Transient Name Server (reuses alerts.tns credentials); a match
           closes as known_tns

Anything the universe doesn't already know becomes state 'candidate' and
waits for a human verdict:

    confirm_candidate() → alerts._store() → targets → CHORUS schedules it
    reject_candidate()  → closed, source suppressed for 30 days via cooldown
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from cloud import db

logger = logging.getLogger("cloud.crossmatch")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── External catalog lookups ───────────────────────────────────────────────────

def _vsx_lookup(ra_deg: float, dec_deg: float, radius_arcsec: float) -> str:
    """Nearest VSX variable within the cone, or ''. Same plain-requests style
    as the node's AAVSO VSP queries."""
    import requests
    resp = requests.get(
        "https://www.aavso.org/vsx/index.php",
        params={
            "view": "api.list",
            "ra": f"{ra_deg:.5f}",
            "dec": f"{dec_deg:+.5f}",
            "radius": f"{radius_arcsec / 3600.0:.5f}",
            "format": "json",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"VSX HTTP {resp.status_code}")
    objs = (resp.json().get("VSXObjects") or {}).get("VSXObject") or []
    if isinstance(objs, dict):    # VSX returns a bare dict for single matches
        objs = [objs]
    for obj in objs:
        name = str(obj.get("Name") or "").strip()
        if name:
            return name
    return ""


def _tns_lookup(ra_deg: float, dec_deg: float, radius_arcsec: float,
                tns_cfg: dict) -> str:
    """Nearest TNS transient within the cone, or ''. Reuses the alerts.tns
    bot credentials and marker-header construction."""
    api_key = tns_cfg.get("api_key", "")
    bot_id = tns_cfg.get("bot_id", "")
    bot_name = tns_cfg.get("bot_name", "")
    if not api_key or not bot_id:
        return ""
    import requests
    headers = {"User-Agent":
               f'tns_marker{{"tns_id":{bot_id},"type":"bot","name":"{bot_name}"}}'}
    resp = requests.post(
        "https://www.wis-tns.org/api/get/search",
        headers=headers,
        data={"api_key": api_key,
              "data": json.dumps({"ra": f"{ra_deg:.5f}",
                                  "dec": f"{dec_deg:+.5f}",
                                  "radius": f"{radius_arcsec:.1f}",
                                  "units": "arcsec"})},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"TNS HTTP {resp.status_code}")
    for obj in resp.json().get("data", [])[:5]:
        name = obj.get("objname")
        if name:
            prefix = str(obj.get("prefix") or "").strip()
            return f"{prefix} {name}".strip()
    return ""


# ── Worker pass ────────────────────────────────────────────────────────────────

def run_pending(config: dict, limit: int = 25) -> dict:
    """Crossmatch all candidates awaiting it. Called on a background loop."""
    radius = float((config.get("survey") or {})
                   .get("crossmatch_radius_arcsec", 10.0))
    tns_cfg = (config.get("alerts") or {}).get("tns", {})

    rows = db.query(
        "SELECT * FROM discovery_candidates WHERE state = 'crossmatching' "
        "ORDER BY updated_at ASC LIMIT %s", (limit,))
    checked = vsx_hits = tns_hits = promoted = 0

    for cand in rows:
        checked += 1
        now = _now()
        try:
            vsx_name = _vsx_lookup(cand["ra_deg"], cand["dec_deg"], radius)
        except Exception as exc:
            logger.warning("VSX lookup failed for candidate #%s: %s",
                           cand["id"], exc)
            continue    # transient API failure — retry on the next pass

        if vsx_name:
            vsx_hits += 1
            db.execute(
                "UPDATE discovery_candidates SET state = 'known_vsx', "
                "vsx_name = %s, updated_at = %s WHERE id = %s",
                (vsx_name, now, cand["id"]))
            # Permanent suppression: this star never alerts again, its
            # measurements simply accumulate as survey science.
            db.execute(
                "UPDATE survey_sources SET vsx_name = %s, "
                "variability_flag = 'vsx_known', vsx_checked_at = %s "
                "WHERE source_key = %s",
                (vsx_name, now, cand["source_key"]))
            logger.info("Candidate #%s is known variable %s",
                        cand["id"], vsx_name)
            continue

        try:
            tns_name = _tns_lookup(cand["ra_deg"], cand["dec_deg"],
                                   radius, tns_cfg)
        except Exception as exc:
            logger.warning("TNS lookup failed for candidate #%s: %s",
                           cand["id"], exc)
            continue

        if tns_name:
            tns_hits += 1
            db.execute(
                "UPDATE discovery_candidates SET state = 'known_tns', "
                "tns_name = %s, updated_at = %s WHERE id = %s",
                (tns_name, now, cand["id"]))
            logger.info("Candidate #%s is known transient %s",
                        cand["id"], tns_name)
            continue

        promoted += 1
        db.execute(
            "UPDATE discovery_candidates SET state = 'candidate', "
            "updated_at = %s WHERE id = %s", (now, cand["id"]))
        logger.warning(
            "UNKNOWN SOURCE survives crossmatch → candidate #%s: %s at "
            "(%.5f, %+.5f) Δ=%s — awaiting human review",
            cand["id"], cand["kind"], cand["ra_deg"], cand["dec_deg"],
            cand.get("peak_delta_mag"))

    if checked:
        logger.info("Crossmatch pass: %d checked, %d VSX, %d TNS, %d promoted",
                    checked, vsx_hits, tns_hits, promoted)
    return {"checked": checked, "vsx": vsx_hits, "tns": tns_hits,
            "candidates": promoted}


def run_pending_retro(config: dict, limit: int = 25) -> dict:
    """Crossmatch retrospective (archive) discoveries. Same position-based
    VSX/TNS lookups — timestamps don't matter — but a separate table and no
    promotion/interrupt path: an unknown archive source becomes a 'candidate'
    for human review, never a live target."""
    radius = float((config.get("survey") or {})
                   .get("crossmatch_radius_arcsec", 10.0))
    tns_cfg = (config.get("alerts") or {}).get("tns", {})
    rows = db.query(
        "SELECT * FROM retro_discoveries WHERE state = 'detected' "
        "ORDER BY updated_at ASC LIMIT %s", (limit,))
    checked = vsx_hits = tns_hits = candidates = 0
    for r in rows:
        checked += 1
        now = _now()
        try:
            vsx_name = _vsx_lookup(r["ra_deg"], r["dec_deg"], radius)
        except Exception as exc:
            logger.warning("retro VSX lookup failed for #%s: %s", r["id"], exc)
            continue
        if vsx_name:
            vsx_hits += 1
            db.execute("UPDATE retro_discoveries SET state='known_vsx', "
                       "vsx_name=%s, updated_at=%s WHERE id=%s",
                       (vsx_name, now, r["id"]))
            continue
        try:
            tns_name = _tns_lookup(r["ra_deg"], r["dec_deg"], radius, tns_cfg)
        except Exception as exc:
            logger.warning("retro TNS lookup failed for #%s: %s", r["id"], exc)
            continue
        if tns_name:
            tns_hits += 1
            db.execute("UPDATE retro_discoveries SET state='known_tns', "
                       "tns_name=%s, updated_at=%s WHERE id=%s",
                       (tns_name, now, r["id"]))
            continue
        candidates += 1
        db.execute("UPDATE retro_discoveries SET state='candidate', "
                   "updated_at=%s WHERE id=%s", (now, r["id"]))
        logger.info("Retro discovery #%s unknown after crossmatch → candidate "
                    "(%s at %.5f,%+.5f)", r["id"], r["kind"],
                    r["ra_deg"], r["dec_deg"])
    if checked:
        logger.info("Retro crossmatch: %d checked, %d VSX, %d TNS, %d candidates",
                    checked, vsx_hits, tns_hits, candidates)
    return {"checked": checked, "vsx": vsx_hits, "tns": tns_hits,
            "candidates": candidates}


# ── Human verdicts ─────────────────────────────────────────────────────────────

def confirm_candidate(cand_id: int, target_type: str = "",
                      note: str = "") -> Optional[dict]:
    """Confirm a candidate as a real discovery: it becomes a target that the
    next replan cycle hands to CHORUS, and the detecting nodes' owners get a
    highlight. No scheduler overrides — CHORUS decides who observes it."""
    from cloud import alerts

    cand = db.query_one("SELECT * FROM discovery_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None or cand["state"] not in ("candidate", "detected",
                                             "crossmatching"):
        return None

    year = datetime.now(timezone.utc).year
    name = f"BS-OA {year}-{cand_id:04d}"
    ttype = (target_type or
             ("VAR" if cand["kind"] in ("brightening", "fading") else "unknown"))
    alerts._store({
        "name": name,
        "ra_deg": float(cand["ra_deg"]),
        "dec_deg": float(cand["dec_deg"]),
        "mag": cand.get("last_mag"),
        "target_type": ttype,
        "time_critical": True,
        "source": "open_aperture",
    })
    target = db.query_one(
        "SELECT target_id FROM targets WHERE name = %s", (name,))
    target_id = (target or {}).get("target_id", "")

    now = _now()
    detail = db.loads(cand.get("detail"), {})
    if note:
        detail["confirm_note"] = note
    db.execute(
        "UPDATE discovery_candidates SET state = 'confirmed', target_id = %s, "
        "detail = %s, updated_at = %s WHERE id = %s",
        (target_id, json.dumps(detail), now, cand_id))

    # Credit every member whose node contributed a detection.
    headline = "Your telescope may have discovered something new!"
    for node_id in db.loads(cand.get("node_ids"), []):
        for owner in db.query(
                "SELECT user_id FROM node_members WHERE node_id = %s",
                (node_id,)):
            try:
                db.execute(
                    """INSERT INTO member_highlights
                           (user_id, node_id, target_name, target_type, bjd,
                            magnitude, headline, detail, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (owner["user_id"], node_id, name, ttype,
                     float(cand.get("last_bjd") or 0.0),
                     float(cand.get("last_mag") or 0.0),
                     headline,
                     f"{name} — {cand['kind']} at "
                     f"({cand['ra_deg']:.5f}, {cand['dec_deg']:+.5f}), "
                     f"now under network follow-up",
                     now))
            except Exception as exc:
                logger.warning("Could not create discovery highlight: %s", exc)

    logger.warning("DISCOVERY CONFIRMED: candidate #%s → target %s (%s)",
                   cand_id, name, target_id)
    return {"candidate_id": cand_id, "target_id": target_id, "name": name}


def reject_candidate(cand_id: int, note: str = "") -> bool:
    cand = db.query_one("SELECT * FROM discovery_candidates WHERE id = %s",
                        (cand_id,))
    if cand is None:
        return False
    detail = db.loads(cand.get("detail"), {})
    if note:
        detail["reject_note"] = note
    db.execute(
        "UPDATE discovery_candidates SET state = 'rejected', detail = %s, "
        "updated_at = %s WHERE id = %s",
        (json.dumps(detail), _now(), cand_id))
    logger.info("Candidate #%s rejected%s", cand_id,
                f" ({note})" if note else "")
    return True
