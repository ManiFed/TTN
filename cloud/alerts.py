#!/usr/bin/env python3
"""
Alert ingestion — pulls candidate targets from public astronomical alert
streams, deduplicates and cross-matches them, and stores them in the targets
table for scoring.

Sources (each one optional and failure-tolerant):
    ALeRCE    — ZTF transient broker, public REST API
    Gaia      — Gaia Photometric Science Alerts, public CSV
    TNS       — Transient Name Server, needs api key + bot id in config
    ATLAS     — ATLAS transient server (QUB), public JSON feed
    ASAS-SN   — ASAS-SN transients list, public CSV endpoint
    AAVSO     — VSX 'targets of interest' via the VSX API

Every fetcher returns a list of raw candidate dicts:
    {"name", "ra_deg", "dec_deg", "mag", "mag_band", "target_type",
     "time_critical", "source"}

ingest_all() runs every enabled fetcher, then upserts through _store(), which
cross-matches against existing targets within MATCH_RADIUS_ARCSEC and merges
sources rather than duplicating.
"""

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from cloud import db
from cloud.conditions import angular_separation_deg

logger = logging.getLogger("cloud.alerts")

MATCH_RADIUS_ARCSEC = 3.0

# Scientific value baseline by object class (0..1); tuned for what a small
# network of Seestars can usefully contribute to.
TYPE_PRIORITY = {
    "SN":      0.95,   # supernovae — early light curves are gold
    "TDE":     0.90,
    "GRB":     0.90,
    "CV":      0.80,   # cataclysmic variables / novae in outburst
    "NOVA":    0.90,
    "YSO":     0.55,
    "AGN":     0.50,
    "EB":      0.55,   # eclipsing binaries
    "VAR":     0.50,   # generic variable
    "unknown": 0.40,
}

# How often each class benefits from re-observation
TYPE_CADENCE_HOURS = {
    "SN": 24.0, "TDE": 24.0, "GRB": 2.0, "NOVA": 12.0,
    "CV": 12.0, "EB": 4.0, "VAR": 48.0, "AGN": 72.0, "unknown": 48.0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get_json(url: str, *, params: dict = None, headers: dict = None,
                   timeout: int = 30):
    import requests
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    return resp.json()


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_alerce(cfg: dict) -> list:
    """ALeRCE ZTF broker — recent bright transients with SN-like classification."""
    mag_limit = float(cfg.get("mag_limit", 16.0))
    days_back = int(cfg.get("days_back", 14))
    first_mjd = _mjd_now() - days_back

    payload = _http_get_json(
        "https://api.alerce.online/ztf/v1/objects",
        params={
            "classifier": "lc_classifier",
            "class_name": "SNIa",
            "probability": 0.5,
            "firstmjd": first_mjd,
            "order_by": "probability",
            "order_mode": "DESC",
            "page_size": 100,
        },
    )
    out = []
    for item in payload.get("items", []):
        try:
            name = str(item["oid"])
            ra, dec = float(item["meanra"]), float(item["meandec"])
        except (KeyError, TypeError, ValueError):
            continue
        # g-band last magnitude when present
        mag = item.get("g_r_mean_corr") or item.get("lastmag")
        try:
            mag = float(mag) if mag is not None else None
        except (TypeError, ValueError):
            mag = None
        if mag is not None and mag > mag_limit:
            continue
        out.append({
            "name": name, "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": "g",
            "target_type": "SN", "time_critical": True, "source": "alerce",
        })
    return out


def fetch_gaia(cfg: dict) -> list:
    """Gaia Photometric Science Alerts — public CSV of all alerts; keep recent ones."""
    import csv
    import io
    import requests

    mag_limit = float(cfg.get("mag_limit", 16.0))
    days_back = int(cfg.get("days_back", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    resp = requests.get("http://gsaweb.ast.cam.ac.uk/alerts/alerts.csv", timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from Gaia alerts")

    out = []
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            published = datetime.strptime(
                row[" Date"].strip(), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        if published < cutoff:
            continue
        try:
            name = row["#Name"].strip()
            ra = float(row[" RaDeg"])
            dec = float(row[" DecDeg"])
            mag = float(row[" AlertMag"])
        except (KeyError, TypeError, ValueError):
            continue
        if mag > mag_limit:
            continue
        cls = (row.get(" Class") or "").strip().upper()
        ttype = "SN" if cls.startswith("SN") else (
            "CV" if cls == "CV" else ("AGN" if cls in ("AGN", "QSO") else "unknown"))
        out.append({
            "name": name, "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": "G",
            "target_type": ttype, "time_critical": ttype in ("SN", "CV"),
            "source": "gaia",
        })
    return out


def fetch_tns(cfg: dict) -> list:
    """Transient Name Server — needs api_key + bot_id + bot_name in cloud config."""
    import json as _json
    import requests

    api_key = cfg.get("api_key", "")
    bot_id = cfg.get("bot_id", "")
    bot_name = cfg.get("bot_name", "")
    if not api_key or not bot_id:
        logger.debug("TNS skipped — api_key/bot_id not configured")
        return []

    mag_limit = float(cfg.get("mag_limit", 16.5))
    days_back = int(cfg.get("days_back", 7))

    headers = {"User-Agent": f'tns_marker{{"tns_id":{bot_id},"type":"bot","name":"{bot_name}"}}'}
    search = {
        "public_timestamp": (datetime.now(timezone.utc)
                             - timedelta(days=days_back)).strftime("%Y-%m-%d"),
    }
    resp = requests.post(
        "https://www.wis-tns.org/api/get/search",
        headers=headers,
        data={"api_key": api_key, "data": _json.dumps(search)},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from TNS search")

    out = []
    for obj in resp.json().get("data", [])[:80]:
        objname = obj.get("objname")
        if not objname:
            continue
        # One detail call per object to get coordinates + magnitude
        try:
            detail = requests.post(
                "https://www.wis-tns.org/api/get/object",
                headers=headers,
                data={"api_key": api_key,
                      "data": _json.dumps({"objname": objname, "photometry": 0})},
                timeout=30,
            ).json().get("data", {})
            ra = float(detail["radeg"])
            dec = float(detail["decdeg"])
            mag = detail.get("discoverymag")
            mag = float(mag) if mag is not None else None
        except Exception:
            continue
        if mag is not None and mag > mag_limit:
            continue
        out.append({
            "name": f"SN {objname}" if str(detail.get("object_type", {}).get("name", "")).startswith("SN") else objname,
            "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": str(detail.get("discmagfilter", {}).get("name", "")),
            "target_type": "SN", "time_critical": True, "source": "tns",
        })
    return out


def fetch_atlas(cfg: dict) -> list:
    """ATLAS transient server (QUB) — public recent-transient JSON feed."""
    mag_limit = float(cfg.get("mag_limit", 16.5))
    payload = _http_get_json(
        "https://star.pst.qub.ac.uk/sne/atlas4/api/objectlist/",
        params={"objectlistid": 2, "format": "json"},  # 2 = good candidates
        timeout=60,
    )
    rows = payload if isinstance(payload, list) else payload.get("results", [])
    out = []
    for row in rows[:200]:
        try:
            name = str(row.get("atlas_designation") or row.get("name") or row["id"])
            ra = float(row["ra"])
            dec = float(row["dec"])
        except (KeyError, TypeError, ValueError):
            continue
        mag = row.get("earliest_mag") or row.get("mag")
        try:
            mag = float(mag) if mag is not None else None
        except (TypeError, ValueError):
            mag = None
        if mag is not None and mag > mag_limit:
            continue
        out.append({
            "name": name, "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": "o",
            "target_type": "SN", "time_critical": True, "source": "atlas",
        })
    return out


def fetch_asassn(cfg: dict) -> list:
    """ASAS-SN transients — public CSV from the ASAS-SN transients page."""
    import csv
    import io
    import requests

    mag_limit = float(cfg.get("mag_limit", 16.5))
    resp = requests.get(
        "https://asas-sn.osu.edu/photometry/transients.csv", timeout=60
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from ASAS-SN")

    out = []
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            name = (row.get("name") or row.get("ASAS-SN") or "").strip()
            ra = float(row.get("ra") or row.get("raj2000"))
            dec = float(row.get("dec") or row.get("dej2000"))
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        mag = row.get("mag") or row.get("V")
        try:
            mag = float(mag) if mag else None
        except (TypeError, ValueError):
            mag = None
        if mag is not None and mag > mag_limit:
            continue
        ttype = (row.get("type") or "unknown").strip().upper()
        ttype = "SN" if ttype.startswith("SN") else ("CV" if "CV" in ttype else "unknown")
        out.append({
            "name": name, "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": "V",
            "target_type": ttype, "time_critical": ttype == "SN",
            "source": "asassn",
        })
    return out


def fetch_aavso(cfg: dict) -> list:
    """
    AAVSO — variables needing observations.  Uses the VSX API to pull the
    configured watch list (vsx_names in cloud config) plus, by default, legacy
    high-value CVs that AAVSO permanently requests coverage for.
    """
    names = list(cfg.get("vsx_names", []) or [])
    if not names and cfg.get("default_watchlist", True):
        names = ["SS Cyg", "U Gem", "Z Cam", "RS Oph", "T CrB", "AE Aqr",
                 "V455 And", "EX Hya", "GK Per", "R CrB"]

    out = []
    for name in names:
        try:
            payload = _http_get_json(
                "https://www.aavso.org/vsx/index.php",
                params={"view": "api.object", "ident": name, "format": "json"},
                timeout=20,
            )
            obj = payload.get("VSXObject", {})
            ra = float(obj["RA2000"])
            dec = float(obj["Declination2000"])
        except Exception as exc:
            logger.debug("VSX lookup failed for %s: %s", name, exc)
            continue
        var_type = str(obj.get("VariabilityType", "")).upper()
        ttype = "CV" if any(k in var_type for k in ("UG", "NL", "ZAND", "N")) else (
            "EB" if "E" in var_type[:2] else "VAR")
        mag = None
        try:
            mag = float(str(obj.get("MaxMag", "")).split()[0].strip("<>"))
        except (ValueError, IndexError):
            pass
        out.append({
            "name": str(obj.get("Name", name)), "ra_deg": ra, "dec_deg": dec,
            "mag": mag, "mag_band": "V",
            "target_type": ttype, "time_critical": False, "source": "aavso",
        })
    return out


_FETCHERS: dict[str, Callable[[dict], list]] = {
    "alerce": fetch_alerce,
    "gaia":   fetch_gaia,
    "tns":    fetch_tns,
    "atlas":  fetch_atlas,
    "asassn": fetch_asassn,
    "aavso":  fetch_aavso,
}


# ── Dedup / cross-match / store ────────────────────────────────────────────────

def _target_id_for(name: str) -> str:
    return "tgt_" + hashlib.sha1(name.strip().lower().encode()).hexdigest()[:12]


def _find_crossmatch(ra_deg: float, dec_deg: float) -> Optional[dict]:
    """Existing target within MATCH_RADIUS_ARCSEC, using a coarse box pre-filter."""
    box = 0.01  # degrees, generous around 3 arcsec
    cos_dec = max(0.05, math.cos(math.radians(dec_deg)))
    rows = db.query(
        """SELECT * FROM targets
           WHERE dec_deg BETWEEN ? AND ? AND ra_deg BETWEEN ? AND ?""",
        (dec_deg - box, dec_deg + box,
         ra_deg - box / cos_dec, ra_deg + box / cos_dec),
    )
    for row in rows:
        sep = angular_separation_deg(ra_deg, dec_deg, row["ra_deg"], row["dec_deg"])
        if sep * 3600.0 <= MATCH_RADIUS_ARCSEC:
            return row
    return None


def _store(candidate: dict) -> bool:
    """Insert a candidate or merge it into a cross-matched existing target.
    Returns True when a new target row was created."""
    name = candidate["name"]
    source = candidate["source"]
    existing = _find_crossmatch(candidate["ra_deg"], candidate["dec_deg"])

    ttype = candidate.get("target_type") or "unknown"
    priority = TYPE_PRIORITY.get(ttype, TYPE_PRIORITY["unknown"])
    cadence = TYPE_CADENCE_HOURS.get(ttype, 48.0)

    if existing:
        sources = db.loads(existing["sources"], [])
        if source not in sources:
            sources.append(source)
        # Keep the most specific classification and the freshest magnitude
        new_type = existing["target_type"]
        if new_type in ("unknown", "VAR") and ttype not in ("unknown",):
            new_type = ttype
        db.execute(
            """UPDATE targets SET mag = COALESCE(?, mag),
                   mag_band = CASE WHEN ? IS NULL THEN mag_band ELSE ? END,
                   target_type = ?, priority = MAX(priority, ?),
                   time_critical = MAX(time_critical, ?),
                   sources = ?, last_updated = ?, active = 1
               WHERE target_id = ?""",
            (candidate.get("mag"), candidate.get("mag"), candidate.get("mag_band"),
             new_type, TYPE_PRIORITY.get(new_type, 0.4),
             1 if candidate.get("time_critical") else 0,
             json.dumps(sources), _now(), existing["target_id"]),
        )
        return False

    db.execute(
        """INSERT OR IGNORE INTO targets
               (target_id, name, ra_deg, dec_deg, mag, mag_band, target_type,
                priority, time_critical, cadence_hours, sources,
                discovered_at, last_updated, active)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (_target_id_for(name), name, candidate["ra_deg"], candidate["dec_deg"],
         candidate.get("mag"), candidate.get("mag_band", ""),
         ttype, priority, 1 if candidate.get("time_critical") else 0,
         cadence, json.dumps([source]), _now(), _now()),
    )
    return True


def ingest_all(config: dict) -> dict:
    """
    Run every enabled fetcher and store the results.
    Returns {"fetched": int, "new": int, "per_source": {...}}.
    """
    alerts_cfg = config.get("alerts", {})
    total_fetched, total_new = 0, 0
    per_source = {}

    for source, fetcher in _FETCHERS.items():
        src_cfg = alerts_cfg.get(source, {})
        if not src_cfg.get("enabled", True):
            continue
        try:
            candidates = fetcher(src_cfg)
        except Exception as exc:
            logger.warning("Alert source %s failed: %s", source, exc)
            per_source[source] = {"fetched": 0, "error": str(exc)}
            continue

        new = 0
        for cand in candidates:
            try:
                if _store(cand):
                    new += 1
            except Exception as exc:
                logger.debug("Could not store candidate %s: %s",
                             cand.get("name"), exc)
        per_source[source] = {"fetched": len(candidates), "new": new}
        total_fetched += len(candidates)
        total_new += new
        logger.info("Alerts from %s: %d candidates, %d new targets",
                    source, len(candidates), new)

    _expire_stale_targets(alerts_cfg)
    return {"fetched": total_fetched, "new": total_new, "per_source": per_source}


def _expire_stale_targets(alerts_cfg: dict) -> None:
    """Deactivate transients that have not been refreshed by any source recently
    (AAVSO watch-list variables never expire — they are long-term programmes)."""
    days = int(alerts_cfg.get("expire_days", 45))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.execute(
        """UPDATE targets SET active = 0
           WHERE active = 1 AND last_updated < ? AND sources NOT LIKE '%aavso%'""",
        (cutoff,),
    )


def _mjd_now() -> float:
    return (datetime.now(timezone.utc)
            - datetime(1858, 11, 17, tzinfo=timezone.utc)).total_seconds() / 86400.0
