"""Broad GCN notice normalization, policy, revision and cancellation handling."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from cloud import db, live
from cloud.event_tiling import assign_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(body: dict, *names, default=None):
    for name in names:
        value = body.get(name)
        if value is not None and value != "":
            return value
    return default


def _scalar(value):
    """Use the first stable identifier when schemas encode IDs as arrays."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value


def _source(topic: str) -> tuple[str, str, str]:
    value = topic.lower()
    if "lvc" in value or "lvk" in value or "gwalert" in value or "igwn" in value:
        return "lvk", "LIGO/Virgo/KAGRA", "gravitational_wave"
    if "icecube" in value:
        return "icecube", "IceCube", "neutrino"
    if "fermi" in value or "swift" in value or "grb" in value:
        return "grb", "Fermi/Swift", "grb"
    if "frb" in value:
        return "frb", "FRB", "frb"
    if "snews" in value or "superk" in value:
        return "snews", "SNEWS/Super-K", "neutrino_burst"
    return topic.split(".")[1] if "." in topic else "gcn", "", "unknown"


def _localization(body: dict) -> tuple[str, dict]:
    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    loc = body.get("localization") if isinstance(body.get("localization"), dict) else {}
    if not loc and isinstance(body.get("most_probable_direction"), dict):
        loc = body["most_probable_direction"]
    if not loc and isinstance(event.get("localization"), dict):
        loc = event["localization"]
    pixels = loc.get("pixels") or body.get("probability_samples")
    if isinstance(pixels, list) and pixels:
        return "healpix_moc", {"pixels": pixels,
                               "object_hash": hashlib.sha256(
                                   json.dumps(pixels, sort_keys=True).encode()).hexdigest()}
    # Prefer the authoritative probability map when a notice also provides a
    # summary RA/Dec. Point/ellipse fallback remains available when no map is
    # attached (for example, a CHIME localization).
    url = _first(loc, "skymap_url", "healpix_url", "url", default=_first(
        body, "skymap_url", "healpix_url", default=_first(event, "skymap_url")))
    if url:
        return "healpix_moc", {"storage_key": str(url),
                               "object_hash": hashlib.sha256(str(url).encode()).hexdigest()}
    ra = _first(loc, "ra", "ra_deg", default=_first(body, "ra", "ra_deg", "RA"))
    dec = _first(loc, "dec", "dec_deg", default=_first(body, "dec", "dec_deg", "DEC"))
    if ra is not None and dec is not None:
        ra_dec_error = _first(loc, "ra_dec_error", default=body.get("ra_dec_error"))
        if isinstance(ra_dec_error, (list, tuple)):
            error = max(float(ra_dec_error[0]), float(ra_dec_error[1]))
            default_major = float(ra_dec_error[0])
            default_minor = float(ra_dec_error[1])
            default_angle = float(ra_dec_error[2]) if len(ra_dec_error) > 2 else 0.0
        else:
            error = float(ra_dec_error if ra_dec_error is not None else _first(
                loc, "error_radius", "error_radius_deg", "radius",
                default=_first(body, "error_radius", "error_radius_deg", default=0.1)))
            default_major = default_minor = error
            default_angle = 0.0
        major = float(_first(loc, "major_deg", "semi_major_axis", default=default_major))
        minor = float(_first(loc, "minor_deg", "semi_minor_axis", default=default_minor))
        kind = "point" if max(major, minor) <= 0.2 else "ellipse"
        return kind, {"ra_deg": float(ra), "dec_deg": float(dec),
                      "error_radius_deg": error, "major_deg": major, "minor_deg": minor,
                      "position_angle_deg": float(_first(
                          loc, "position_angle_deg", "position_angle",
                          default=default_angle))}
    return "none", {}


def normalize(topic: str, body: dict) -> dict:
    source, mission, event_class = _source(topic)
    nested_event = body.get("event") if isinstance(body.get("event"), dict) else {}
    properties = nested_event.get("properties") \
        if isinstance(nested_event.get("properties"), dict) else {}
    classification = nested_event.get("classification") \
        if isinstance(nested_event.get("classification"), dict) else {}
    sid = str(_scalar(_first(body, "superevent_id", "event_name", "event_id", "ref_ID",
                            "trigger_id", "id", "ivorn", default=""))).strip()
    if not sid:
        sid = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
    role = str(_first(body, "role", "alert_tense", default="observation")).lower()
    notice_type = str(_first(body, "alert_type", "notice_type", default="initial")).lower()
    revision_value = _first(body, "sequence_num", "record_number", "revision", "sequence",
                            default=None)
    try:
        revision = int(revision_value) if revision_value is not None else None
    except (TypeError, ValueError):
        revision = None
    loc_type, localization = _localization(body)
    area90 = _first(body, "area90", "area_90")
    if area90 is None and loc_type == "healpix_moc" and localization.get("pixels"):
        try:
            from cloud.event_tiling import credible_area_deg2
            area90 = credible_area_deg2(localization, 0.9)
        except Exception:
            area90 = None
    return {
        "source_event_id": sid, "source": source, "mission": mission, "topic": topic,
        "schema_version": str(_first(body, "schema_version", "$schema", default="")),
        "event_class": str(_first(body, "event_class", default=event_class)),
        "role": "test" if role in ("test", "mdc", "mock", "injection") else "observation",
        "revision": revision, "notice_type": notice_type,
        "event_time": str(_first(body, "event_time", "time_created", "trigger_time",
                                 "alert_datetime", default=nested_event.get("time") or "")),
        "significance": {
            "significant": bool(_first(body, "significant",
                                       default=nested_event.get("significant", False))),
            "false_alarm_rate": _first(body, "far", "false_alarm_rate",
                                       default=nested_event.get("far")),
            "has_ns": _first(body, "HasNS", "has_ns", default=properties.get("HasNS")),
            "p_astro": _first(body, "p_astro"),
            "class": _first(body, "pipeline", "classification", "signalness", "alert_class",
                            default=max(classification, key=classification.get)
                            if classification else None),
            "external_coincidence": bool(_first(
                body, "external_coincidence", default=body.get("external_coinc"))),
        },
        "localization_type": loc_type, "localization": localization,
        "area50_deg2": _first(body, "area50", "area_50"),
        "area90_deg2": area90,
        "distance": body.get("distance") or ({
            "mean_mpc": body.get("luminosity_distance"),
            "std_mpc": body.get("luminosity_distance_error"),
        } if body.get("luminosity_distance") is not None else {}), "raw": body,
    }


def policy_decision(event: dict, config: dict) -> dict:
    gcfg = config.get("gcn") or {}
    if event["role"] == "test":
        return {"eligible": True, "dispatch": False, "reason": "test_shadow"}
    if event["localization_type"] == "none":
        return {"eligible": False, "dispatch": False, "reason": "no_optical_localization"}
    sig, source = event["significance"], event["source"]
    area = float(event.get("area90_deg2") or 0.0)
    eligible, reason = False, "source_policy_rejected"
    if source == "lvk":
        eligible = (bool(sig.get("significant"))
                    and (float(sig.get("has_ns") or 0) >= float(
                             gcfg.get("lvk_min_has_ns", 0.1))
                         or bool(sig.get("external_coincidence")))
                    and (not area or area <= float(gcfg.get("lvk_max_area_deg2", 2000))))
        reason = "lvk_policy" if eligible else "lvk_threshold"
    elif source == "icecube":
        cls = str(sig.get("class") or "").lower()
        eligible = "gold" in cls or float(sig.get("p_astro") or 0) >= float(
            gcfg.get("icecube_min_p_astro", 0.5))
        reason = "icecube_policy" if eligible else "icecube_threshold"
    elif source == "grb":
        age_ok = True
        if event.get("event_time"):
            try:
                when = datetime.fromisoformat(str(event["event_time"]).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                age_ok = ((datetime.now(timezone.utc) - when).total_seconds()
                          <= float(gcfg.get("grb_max_age_hours", 6)) * 3600)
            except ValueError:
                age_ok = False
        eligible = (not area or area <= float(gcfg.get("grb_max_area_deg2", 500))) and age_ok
        reason = "grb_policy" if eligible else ("grb_too_old" if not age_ok else "grb_area_cap")
    elif source == "frb":
        radius = float((event.get("localization") or {}).get("error_radius_deg") or 99)
        eligible = event["localization_type"] == "point" or radius <= float(
            gcfg.get("frb_max_radius_deg", 0.5))
        reason = "frb_localized" if eligible else "frb_too_broad"
    elif source == "snews":
        reason = "awaiting_optical_counterpart"
    else:
        eligible = bool(gcfg.get("observe_unknown_sources", False))
        reason = "configured_unknown_source" if eligible else "unknown_source"
    dispatch = eligible and bool(gcfg.get("live_dispatch", False))
    return {"eligible": eligible, "dispatch": dispatch, "reason": reason}


def ingest(topic: str, body: dict, config: dict) -> dict:
    normalized = normalize(topic, body)
    notice_hash = hashlib.sha256(json.dumps(body, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    existing = db.query_one(
        "SELECT * FROM network_events WHERE source=%s AND source_event_id=%s",
        (normalized["source"], normalized["source_event_id"]))
    event_id = existing["event_id"] if existing else "evt_" + uuid.uuid4().hex[:20]
    if existing and db.query_one(
            "SELECT revision FROM event_revisions WHERE event_id=%s AND notice_hash=%s",
            (event_id, notice_hash)):
        prior = db.query_one(
            "SELECT revision FROM event_revisions WHERE event_id=%s AND notice_hash=%s",
            (event_id, notice_hash))
        return {"event_id": event_id, "revision": prior["revision"], "duplicate": True}
    if normalized["revision"] is None:
        normalized["revision"] = int(existing.get("active_revision") or 0) + 1 if existing else 1
    revision = int(normalized["revision"])
    if existing and db.query_one(
            "SELECT 1 FROM event_revisions WHERE event_id=%s AND revision=%s",
            (event_id, revision)):
        return {"event_id": event_id, "revision": revision, "duplicate": True}
    policy = policy_decision(normalized, config)
    retracted = normalized["notice_type"] in ("retraction", "retracted", "withdrawal")
    generation = int(existing.get("cancellation_generation") or 0) + (1 if retracted else 0) \
        if existing else (1 if retracted else 0)
    status = "retracted" if retracted else ("eligible" if policy["eligible"] else "received")
    now = _now()
    if existing:
        db.execute(
            "UPDATE network_events SET mission=%s,topic=%s,schema_version=%s,event_class=%s,"
            "role=%s,active_revision=%s,status=%s,event_time=%s,received_time=%s,policy=%s,"
            "cancellation_generation=%s WHERE event_id=%s",
            (normalized["mission"], topic, normalized["schema_version"], normalized["event_class"],
             normalized["role"], revision, status, normalized["event_time"], now,
             json.dumps(policy), generation, event_id))
    else:
        db.execute(
            "INSERT INTO network_events(event_id,source_event_id,source,mission,topic,schema_version,"
            "event_class,role,active_revision,status,event_time,received_time,policy,"
            "cancellation_generation) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (event_id, normalized["source_event_id"], normalized["source"], normalized["mission"],
             topic, normalized["schema_version"], normalized["event_class"], normalized["role"],
             revision, status, normalized["event_time"], now, json.dumps(policy), generation))
    db.execute(
        "INSERT INTO event_revisions(event_id,revision,notice_type,significance,localization_type,"
        "localization,area50_deg2,area90_deg2,distance,raw_notice,notice_hash,received_at) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (event_id, revision, normalized["notice_type"], json.dumps(normalized["significance"]),
         normalized["localization_type"], json.dumps(normalized["localization"]),
         normalized["area50_deg2"], normalized["area90_deg2"],
         json.dumps(normalized["distance"]), json.dumps(body), notice_hash, now))
    if existing:
        affected_nodes = [r["node_id"] for r in db.query(
            "SELECT DISTINCT node_id FROM observation_tasks WHERE event_id=%s "
            "AND state IN ('pending','received')", (event_id,))]
        db.execute("UPDATE observation_tasks SET state='cancelled',updated_at=%s "
                   "WHERE event_id=%s AND state IN ('pending','received')",
                   (now, event_id))
        for node_id in affected_nodes:
            live.publish(node_id, "retask", {"event_id": event_id, "revision": revision})
    assignments = []
    if policy["eligible"] and not retracted:
        assignments = assign_event(
            {"event_id": event_id, "cancellation_generation": generation,
             "source": normalized["source"], "distance": normalized["distance"]}, revision,
            normalized["localization"], config, dispatch=policy["dispatch"])
        if assignments:
            db.execute("UPDATE network_events SET status=%s WHERE event_id=%s",
                       ("active" if policy["dispatch"] else "eligible", event_id))
    return {"event_id": event_id, "revision": revision, "policy": policy,
            "assignments": assignments, "retracted": retracted}


def cancel(event_id: str) -> bool:
    row = db.query_one("SELECT cancellation_generation FROM network_events WHERE event_id=%s",
                       (event_id,))
    if not row:
        return False
    now = _now()
    affected_nodes = [r["node_id"] for r in db.query(
        "SELECT DISTINCT node_id FROM observation_tasks WHERE event_id=%s "
        "AND state IN ('pending','received')", (event_id,))]
    db.execute("UPDATE network_events SET status='retracted',cancellation_generation=%s "
               "WHERE event_id=%s", (int(row["cancellation_generation"] or 0) + 1, event_id))
    db.execute("UPDATE observation_tasks SET state='cancelled',updated_at=%s WHERE event_id=%s "
               "AND state IN ('pending','received')", (now, event_id))
    for node_id in affected_nodes:
        live.publish(node_id, "retask", {"event_id": event_id, "cancelled": True})
    return True


def expire_events() -> int:
    """Close elapsed tasks and derive revision completion dispositions."""
    now = _now()
    expired = db.query(
        "SELECT DISTINCT event_id FROM observation_tasks WHERE latest_utc<=%s "
        "AND state IN ('pending','received')", (now,))
    db.execute("UPDATE observation_tasks SET state='cancelled',updated_at=%s "
               "WHERE latest_utc<=%s AND state IN ('pending','received')", (now, now))
    touched = {r["event_id"] for r in expired}
    for event in db.query("SELECT event_id,status FROM network_events "
                          "WHERE status IN ('eligible','active')"):
        counts = db.query_one(
            "SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE state='completed') AS complete,"
            "COUNT(*) FILTER(WHERE state IN ('pending','received','started')) AS open "
            "FROM observation_tasks WHERE event_id=%s", (event["event_id"],)) or {}
        if int(counts.get("total") or 0) and not int(counts.get("open") or 0):
            disposition = ("complete" if int(counts.get("complete") or 0)
                           else "expired" if event["event_id"] in touched else "capacity-limited")
            db.execute("UPDATE network_events SET status=%s WHERE event_id=%s",
                       (disposition, event["event_id"]))
    return len(touched)
