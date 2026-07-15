"""Deterministic residual-probability tiling for network transient events."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from cloud import db, live, registry, scheduler


def _sep(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle distance in degrees, including RA=0 and polar fields."""
    a1, a2, d1, d2 = map(math.radians, (ra1, ra2, dec1, dec2))
    value = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, value))))


def probability_samples(localization: dict) -> list[dict]:
    """Convert canonical point/ellipse/sample localizations to sky samples."""
    pixels = localization.get("pixels") or localization.get("samples") or []
    if not pixels and localization.get("storage_key"):
        pixels = _read_healpix(str(localization["storage_key"]))
    if pixels:
        out = [{"ra_deg": float(p["ra_deg"]) % 360.0,
                "dec_deg": max(-90.0, min(90.0, float(p["dec_deg"]))),
                "probability": max(0.0, float(p.get("probability") or 0.0)),
                **({"area_deg2": float(p["area_deg2"])} if p.get("area_deg2") is not None else {})}
               for p in pixels]
    elif localization.get("ra_deg") is not None:
        ra = float(localization["ra_deg"]) % 360.0
        dec = float(localization["dec_deg"])
        major = max(0.05, float(localization.get("major_deg")
                                or localization.get("error_radius_deg") or 0.1))
        minor = max(0.05, float(localization.get("minor_deg") or major))
        angle = math.radians(float(localization.get("position_angle_deg") or 0.0))
        out = []
        # Fixed tangent-plane quadrature makes ellipse handling reproducible.
        for ix in range(-4, 5):
            for iy in range(-4, 5):
                x, y = ix * major / 2.0, iy * minor / 2.0
                weight = math.exp(-0.5 * ((x / major) ** 2 + (y / minor) ** 2))
                east = x * math.cos(angle) - y * math.sin(angle)
                north = x * math.sin(angle) + y * math.cos(angle)
                cos_dec = max(0.05, math.cos(math.radians(dec)))
                out.append({"ra_deg": (ra + east / cos_dec) % 360.0,
                            "dec_deg": max(-90.0, min(90.0, dec + north)),
                            "probability": weight})
    else:
        return []
    total = sum(p["probability"] for p in out)
    if total <= 0:
        return []
    for p in out:
        p["probability"] /= total
    return out


def _read_healpix(storage_key: str) -> list[dict]:
    """Read fixed-order or multi-order HEALPix FITS probability maps."""
    import io
    import numpy as np
    from astropy.table import QTable
    from astropy_healpix import HEALPix, healpix_to_lonlat, uniq_to_level_ipix
    if storage_key.startswith(("http://", "https://")):
        import requests
        response = requests.get(storage_key, timeout=30)
        response.raise_for_status()
        source = io.BytesIO(response.content)
    else:
        source = storage_key
    table = QTable.read(source, format="fits")
    names = {name.upper(): name for name in table.colnames}
    out = []
    if "UNIQ" in names:
        levels, ipix = uniq_to_level_ipix(np.asarray(table[names["UNIQ"]], dtype=np.int64))
        densities = np.asarray(table[names.get("PROBDENSITY", "PROBDENSITY")], dtype=float)
        for level in np.unique(levels):
            mask = levels == level
            nside = 2 ** int(level)
            lon, lat = healpix_to_lonlat(ipix[mask], nside, order="nested")
            area_sr = 4 * math.pi / (12 * nside * nside)
            for ra, dec, density in zip(lon.deg, lat.deg, densities[mask]):
                out.append({"ra_deg": float(ra), "dec_deg": float(dec),
                            "probability": max(0.0, float(density) * area_sr),
                            "area_deg2": math.degrees(1) ** 2 * area_sr})
    elif "PROB" in names:
        probs = np.asarray(table[names["PROB"]], dtype=float)
        nside = int(table.meta.get("NSIDE") or round(math.sqrt(len(probs) / 12)))
        hp = HEALPix(nside=nside, order=str(table.meta.get("ORDERING", "RING")).lower())
        lon, lat = hp.healpix_to_lonlat(np.arange(len(probs)))
        area_deg2 = 4 * math.pi / (12 * nside * nside) * math.degrees(1) ** 2
        out = [{"ra_deg": float(ra), "dec_deg": float(dec), "probability": float(prob),
                "area_deg2": area_deg2}
               for ra, dec, prob in zip(lon.deg, lat.deg, probs) if prob > 0]
    return out


def credible_area_deg2(localization: dict, level: float = 0.9) -> float | None:
    samples = probability_samples(localization)
    if not samples or not all(p.get("area_deg2") is not None for p in samples):
        return None
    total = 0.0
    area = 0.0
    for sample in sorted(samples, key=lambda p: p["probability"] / p["area_deg2"], reverse=True):
        area += float(sample["area_deg2"])
        total += float(sample["probability"])
        if total >= level:
            break
    return area


def generate_tiles(localization: dict, radius_deg: float) -> list[dict]:
    """Greedily cover samples, removing covered probability after each tile."""
    remaining = _coarsen(probability_samples(localization), max(radius_deg / 2.0, 0.05))
    tiles = []
    while remaining:
        best = None
        for seed in remaining:
            covered = [p for p in remaining if _sep(
                seed["ra_deg"], seed["dec_deg"], p["ra_deg"], p["dec_deg"]) <= radius_deg]
            mass = sum(p["probability"] for p in covered)
            key = (-mass, round(seed["ra_deg"], 8), round(seed["dec_deg"], 8))
            if best is None or key < best[0]:
                best = (key, seed, covered, mass)
        _, seed, covered, mass = best
        tiles.append({"ra_deg": seed["ra_deg"], "dec_deg": seed["dec_deg"],
                      "radius_deg": radius_deg, "probability_mass": mass})
        covered_ids = {id(p) for p in covered}
        remaining = [p for p in remaining if id(p) not in covered_ids]
    return tiles


def _coarsen(samples: list[dict], bin_deg: float) -> list[dict]:
    """Conserve probability while bounding work for high-resolution maps."""
    bins: dict[tuple[int, int], dict] = {}
    for sample in samples:
        key = (int((sample["ra_deg"] % 360) / bin_deg),
               int((sample["dec_deg"] + 90) / bin_deg))
        cell = bins.setdefault(key, {"probability": 0.0, "x": 0.0, "y": 0.0, "z": 0.0})
        p = float(sample["probability"])
        ra, dec = math.radians(sample["ra_deg"]), math.radians(sample["dec_deg"])
        cell["probability"] += p
        cell["x"] += p * math.cos(dec) * math.cos(ra)
        cell["y"] += p * math.cos(dec) * math.sin(ra)
        cell["z"] += p * math.sin(dec)
    out = []
    for cell in bins.values():
        p = cell["probability"]
        ra = math.degrees(math.atan2(cell["y"], cell["x"])) % 360
        dec = math.degrees(math.atan2(cell["z"], math.hypot(cell["x"], cell["y"])))
        out.append({"ra_deg": ra, "dec_deg": dec, "probability": p})
    return out


def galaxy_mixture(localization: dict, distance: dict, galaxy_fraction: float = 0.7) -> dict:
    """Mix local indexed-galaxy probability with raw sky probability."""
    sky = probability_samples(localization)
    if not sky:
        return localization
    try:
        mean = float(distance.get("mean_mpc") or distance.get("mean") or 0)
        sigma = float(distance.get("std_mpc") or distance.get("sigma") or max(mean * 0.3, 1))
    except (TypeError, ValueError):
        mean = sigma = 0.0
    if mean > 0:
        galaxies = db.query(
            "SELECT * FROM galaxy_catalog WHERE distance_mpc BETWEEN %s AND %s LIMIT 200000",
            (max(0, mean - 4 * sigma), mean + 4 * sigma))
    else:
        galaxies = db.query("SELECT * FROM galaxy_catalog LIMIT 200000")
    weighted = []
    for galaxy in galaxies:
        weight = max(0.0, float(galaxy.get("luminosity_weight") or 1.0))
        if mean > 0 and galaxy.get("distance_mpc") is not None:
            z = (float(galaxy["distance_mpc"]) - mean) / max(sigma, 1e-6)
            weight *= math.exp(-0.5 * z * z)
        if weight > 0:
            weighted.append({"ra_deg": float(galaxy["ra_deg"]),
                             "dec_deg": float(galaxy["dec_deg"]), "probability": weight})
    total = sum(g["probability"] for g in weighted)
    if total <= 0:
        return localization
    mixed = [{**p, "probability": p["probability"] * (1 - galaxy_fraction)} for p in sky]
    mixed.extend({**g, "probability": g["probability"] / total * galaxy_fraction}
                 for g in weighted)
    return {"pixels": mixed, "galaxy_weighted": True,
            "galaxy_fraction": galaxy_fraction}


def assign_event(event: dict, revision: int, localization: dict,
                 config: dict, *, dispatch: bool = False) -> list[dict]:
    """Create fleet assignments ranked by marginal probability per time."""
    ecfg = config.get("gcn") or {}
    fleet_live = {n["node_id"]: n for n in live.fleet_state()}
    online = {nid for nid in live.dark_online_nodes()
              if (fleet_live.get(nid) or {}).get("sky_clear") is not False}
    nodes = [n for n in registry.list_nodes(active_only=True)
             if n["node_id"] in online]
    if not nodes:
        return []
    # Candidate geometry uses the smallest online field so all assigned
    # footprints are honest; larger fields get their true radius in storage.
    radii = [max(0.1, float(n.get("fov_deg") or 1.0) / 2.0) for n in nodes]
    if event.get("source") == "lvk" and event.get("distance"):
        localization = galaxy_mixture(
            localization, event["distance"], float(ecfg.get("galaxy_weight_fraction", 0.7)))
    tiles = generate_tiles(localization, min(radii))
    now = datetime.now(timezone.utc)
    latest = now + timedelta(hours=float(ecfg.get("task_expiry_hours", 6)))
    assignments = []
    occupied: set[str] = set()
    max_nodes = max(1, int(ecfg.get("max_online_node_fraction", 0.2) * len(nodes)))
    max_hours = float(ecfg.get("max_node_hours_per_event", 50))
    used_hours = 0.0
    for tile in tiles:
        choices = []
        for node in nodes:
            if node["node_id"] in occupied:
                continue
            from cloud.conditions import angular_separation_deg, moon_state, target_alt
            altitude = target_alt(tile["ra_deg"], tile["dec_deg"],
                                  float(node.get("latitude") or 0),
                                  float(node.get("longitude") or 0), now)
            if altitude < float(node.get("min_altitude_deg") or 25):
                continue
            required_filter = str(ecfg.get("default_filter") or "CV")
            available = db.loads(node.get("filter_set"), []) or [str(node.get("filters") or "CV")]
            if required_filter not in available and required_filter != "CV":
                continue
            dur, count = scheduler.choose_exposure(None, node)
            cost_h = max(1.0 / 60.0, dur * count / 3600.0)
            reliability = float(node.get("reliability_score") or 0.7)
            depth = float(node.get("mag_faint_limit") or 14.0)
            detectability = max(0.05, min(1.0, (depth - 10.0) / 8.0))
            moon = moon_state(now)
            moon_sep = angular_separation_deg(tile["ra_deg"], tile["dec_deg"],
                                              moon["ra_deg"], moon["dec_deg"])
            moon_factor = max(0.2, 1.0 - float(moon["illumination"]) *
                              max(0.0, (45.0 - moon_sep) / 45.0))
            phase = str((fleet_live.get(node["node_id"]) or {}).get("phase") or "")
            occupancy = 1.25 if phase in ("exposing", "slewing") else 1.0
            value = (tile["probability_mass"] * reliability * detectability * moon_factor
                     / (cost_h * occupancy))
            choices.append((-value, str(node["node_id"]), node, dur, count, cost_h))
        if not choices:
            continue
        if len(assignments) >= max_nodes or used_hours >= max_hours:
            break
        _, _, node, dur, count, cost_h = min(choices)
        tile_key = f"{event['event_id']}:{revision}:{tile['ra_deg']:.7f}:{tile['dec_deg']:.7f}"
        tile_id = "tile_" + hashlib.sha256(tile_key.encode()).hexdigest()[:20]
        task_id = "task_" + hashlib.sha256((tile_key + node["node_id"]).encode()).hexdigest()[:20]
        detail = {"scoring": "residual_probability*detectability*delivery/time",
                  "processing_mode": "event_tile"}
        db.execute(
            "INSERT INTO event_tiles(tile_id,event_id,event_revision,ra_deg,dec_deg,radius_deg,"
            "probability_mass,pass_number,status,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,1,%s,%s) "
            "ON CONFLICT(tile_id) DO NOTHING",
            (tile_id, event["event_id"], revision, tile["ra_deg"], tile["dec_deg"],
             max(0.1, float(node.get("fov_deg") or 1.0) / 2), tile["probability_mass"],
             "assigned" if dispatch else "shadow", json.dumps(detail)))
        exposure = {"expDur": dur, "expCount": count,
                    "filter": str(ecfg.get("default_filter") or "CV")}
        state = "pending" if dispatch else "shadow"
        db.execute(
            "INSERT INTO observation_tasks(task_id,node_id,event_id,event_revision,tile_id,ra_deg,"
            "dec_deg,earliest_utc,latest_utc,exposure,priority,state,cancellation_generation,"
            "created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(task_id) DO NOTHING",
            (task_id, node["node_id"], event["event_id"], revision, tile_id,
             tile["ra_deg"], tile["dec_deg"], now.isoformat(), latest.isoformat(),
             json.dumps(exposure), float(tile["probability_mass"]), state,
             int(event.get("cancellation_generation") or 0), now.isoformat(), now.isoformat()))
        if dispatch:
            live.publish(node["node_id"], "retask", {"task_id": task_id})
        assignments.append({"task_id": task_id, "tile_id": tile_id,
                            "node_id": node["node_id"], **tile, "exposure": exposure})
        occupied.add(node["node_id"])
        used_hours += cost_h
    # A second epoch on the most valuable assigned fields is pre-authorized no
    # earlier than 30 minutes later. It uses the same nodes, so the live fleet
    # fraction cap remains a cap on concurrent participation.
    first_pass = list(assignments)
    for parent in sorted(first_pass, key=lambda a: (-a["probability_mass"], a["tile_id"])):
        cost_h = (float(parent["exposure"]["expDur"])
                  * int(parent["exposure"]["expCount"]) / 3600.0)
        if used_hours + cost_h > max_hours:
            break
        tile_id = parent["tile_id"] + "_p2"
        task_id = parent["task_id"] + "_p2"
        earliest2 = now + timedelta(minutes=30)
        db.execute(
            "INSERT INTO event_tiles(tile_id,event_id,event_revision,ra_deg,dec_deg,radius_deg,"
            "probability_mass,pass_number,status,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,2,%s,%s) "
            "ON CONFLICT(tile_id) DO NOTHING",
            (tile_id, event["event_id"], revision, parent["ra_deg"], parent["dec_deg"],
             parent["radius_deg"], parent["probability_mass"],
             "assigned" if dispatch else "shadow", json.dumps({"revisit_of": parent["tile_id"]})))
        db.execute(
            "INSERT INTO observation_tasks(task_id,node_id,event_id,event_revision,tile_id,ra_deg,"
            "dec_deg,earliest_utc,latest_utc,exposure,priority,state,cancellation_generation,"
            "created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(task_id) DO NOTHING",
            (task_id, parent["node_id"], event["event_id"], revision, tile_id,
             parent["ra_deg"], parent["dec_deg"], earliest2.isoformat(), latest.isoformat(),
             json.dumps(parent["exposure"]), parent["probability_mass"] * 0.5,
             "pending" if dispatch else "shadow", int(event.get("cancellation_generation") or 0),
             now.isoformat(), now.isoformat()))
        assignments.append({**parent, "task_id": task_id, "tile_id": tile_id,
                            "pass_number": 2})
        used_hours += cost_h
    return assignments
