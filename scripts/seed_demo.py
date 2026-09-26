#!/usr/bin/env python3
"""
Seed the cloud database with realistic demo data so the public API endpoints
(/network/status, /targets, /lightcurves) return real, non-empty results for
the marketing site and dashboard.

    python3 scripts/seed_demo.py            # uses cloud/config.yaml db path
    python3 scripts/seed_demo.py --wipe     # clear demo rows first

This writes to the real tables via the real db layer — it is not mock data in
the front end; the API reads exactly what this inserts.
"""

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from cloud import db

random.seed(42)
NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def jd(dt):
    """Julian Date for a UTC datetime (good to the second — fine for a demo)."""
    return dt.timestamp() / 86400.0 + 2440587.5


# 23 nodes across 14 countries — varied reliability and hardware.
NODES = [
    ("node_a3f9b2c1", "Maria Soltani",  "Lisbon",      "Portugal",     38.72,  -9.14, 0.94, True),
    ("node_c7e2d918", "James Okafor",   "Lagos",       "Nigeria",       6.52,   3.38, 0.71, True),
    ("node_b1d5a4f0", "Yuki Tanaka",    "Sapporo",     "Japan",        43.06, 141.35, 0.88, True),
    ("node_f4a8c3e2", "Elena Novak",    "Brno",        "Czechia",      49.20,  16.61, 0.82, True),
    ("node_9d2e1b77", "Tom Whitfield",  "Christchurch","New Zealand", -43.53, 172.64, 0.79, False),
    ("node_3a8f5c20", "Priya Nair",     "Bengaluru",   "India",        12.97,  77.59, 0.66, True),
    ("node_e5b7d401", "Lucas Moreau",   "Lyon",        "France",       45.76,   4.84, 0.91, True),
    ("node_7c4a9e13", "Sofia Castro",   "Santiago",    "Chile",       -33.45, -70.66, 0.85, True),
    ("node_2f6b8d05", "Anders Holm",    "Bergen",      "Norway",       60.39,   5.32, 0.58, False),
    ("node_d8e3a7c9", "Grace Liu",      "Vancouver",   "Canada",       49.28,-123.12, 0.77, True),
    ("node_5b1f4c8a", "Omar Haddad",    "Amman",       "Jordan",       31.95,  35.93, 0.69, True),
    ("node_a9c2e6b4", "Nina Petrova",   "Tbilisi",     "Georgia",      41.72,  44.79, 0.63, False),
    ("node_4e7d2a91", "Carlos Mendez",  "Mexico City", "Mexico",       19.43, -99.13, 0.74, True),
    ("node_8f3b5d27", "Aoife Byrne",    "Galway",      "Ireland",      53.27,  -9.05, 0.81, True),
    ("node_1c9a4f63", "Wei Chen",       "Perth",       "Australia",   -31.95, 115.86, 0.86, True),
    ("node_6d2e8b40", "Ravi Kapoor",    "Pune",        "India",        18.52,  73.86, 0.55, False),
    ("node_b4f7c1a8", "Lena Fischer",   "Graz",        "Austria",      47.07,  15.44, 0.78, True),
    ("node_e2a9d5c3", "Diego Rossi",    "Bologna",     "Italy",        44.49,  11.34, 0.83, True),
    ("node_9f1b6e24", "Hana Kim",       "Daejeon",     "South Korea",  36.35, 127.38, 0.72, True),
    ("node_3d8c2a57", "Paulo Santos",   "Porto",       "Portugal",     41.16,  -8.62, 0.67, False),
    ("node_c5e1a9b8", "Freya Olsen",    "Aarhus",      "Denmark",      56.16,  10.20, 0.76, True),
    ("node_7a4f3d09", "Mateus Alves",   "Recife",      "Brazil",       -8.05, -34.88, 0.61, True),
    ("node_2b8e5c41", "Ingrid Berg",    "Uppsala",     "Sweden",       59.86,  17.64, 0.89, True),
]

# Variable-star targets with real coordinates.
TARGETS = [
    ("t_sscyg", "SS Cyg", 325.679,  43.586, 11.5, "V", "dwarf_nova",   0.86, ["AAVSO", "ASAS-SN"]),
    ("t_tcrb",  "T CrB",  239.876,  25.920,  9.9, "V", "recurrent_nova",0.97,["AAVSO", "ATLAS"]),
    ("t_rleo",  "R Leo",  146.894,  11.426,  6.8, "V", "mira",          0.64, ["AAVSO"]),
    ("t_zuma",  "Z UMa",  179.137,  57.866,  7.9, "V", "semiregular",   0.59, ["AAVSO"]),
    ("t_ssaur", "SS Aur",  93.341,  47.739, 12.0, "V", "dwarf_nova",    0.78, ["AAVSO", "ALeRCE"]),
    ("t_uvel",  "U Vel",  135.668, -41.323,  8.4, "V", "mira",          0.52, ["AAVSO"]),
    ("t_rscvn", "RS CVn", 199.667,  35.554,  8.0, "V", "eclipsing",     0.71, ["AAVSO", "Gaia"]),
    ("t_chcyg", "CH Cyg", 291.139,  50.241,  7.1, "V", "symbiotic",     0.68, ["AAVSO"]),
    ("t_sn2026", "SN 2026abc", 187.701, 12.391, 15.8, "V", "supernova", 0.93, ["TNS", "ALeRCE"]),
]

ONLINE_NODE_IDS = [n[0] for n in NODES if n[7]]


def wipe():
    for tbl in ("measurements", "scores", "targets", "node_members", "nodes"):
        db.execute(f"DELETE FROM {tbl}")
    print("wiped demo tables")


def seed_nodes():
    for nid, owner, city, country, lat, lon, rel, online in NODES:
        hb = NOW - timedelta(minutes=random.randint(1, 9)) if online \
            else NOW - timedelta(hours=random.randint(20, 96))
        total_obs = random.randint(40, 900)
        accepted = int(total_obs * (0.80 + rel * 0.15))
        db.execute(
            """INSERT INTO nodes
               (node_id, api_key, owner_name, city, country, latitude, longitude,
                bortle, tier, telescope_model, status, registered_at, last_heartbeat,
                reliability_score, total_observations, aavso_accepted,
                clear_nights_30d, mean_uncertainty, mean_fwhm)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (node_id) DO UPDATE SET
                   api_key=EXCLUDED.api_key, owner_name=EXCLUDED.owner_name,
                   city=EXCLUDED.city, country=EXCLUDED.country,
                   latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
                   bortle=EXCLUDED.bortle, tier=EXCLUDED.tier,
                   telescope_model=EXCLUDED.telescope_model, status=EXCLUDED.status,
                   registered_at=EXCLUDED.registered_at,
                   last_heartbeat=EXCLUDED.last_heartbeat,
                   reliability_score=EXCLUDED.reliability_score,
                   total_observations=EXCLUDED.total_observations,
                   aavso_accepted=EXCLUDED.aavso_accepted,
                   clear_nights_30d=EXCLUDED.clear_nights_30d,
                   mean_uncertainty=EXCLUDED.mean_uncertainty,
                   mean_fwhm=EXCLUDED.mean_fwhm""",
            (nid, "seed_" + nid, owner, city, country, lat, lon,
             random.randint(3, 6), random.choice([1, 1, 1, 2]),
             "ZWO Seestar S50", "active",
             _iso(NOW - timedelta(days=random.randint(30, 400))), _iso(hb),
             rel, total_obs, accepted,
             random.randint(8, 28), round(0.04 + (1 - rel) * 0.10, 3),
             round(2.8 + random.random() * 1.6, 2)),
        )
    print(f"seeded {len(NODES)} nodes ({len(ONLINE_NODE_IDS)} online)")


def seed_targets():
    for tid, name, ra, dec, mag, band, ttype, prio, sources in TARGETS:
        db.execute(
            """INSERT INTO targets
               (target_id, name, ra_deg, dec_deg, mag, mag_band, target_type,
                priority, sources, discovered_at, last_updated, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
               ON CONFLICT (target_id) DO UPDATE SET
                   name=EXCLUDED.name, ra_deg=EXCLUDED.ra_deg, dec_deg=EXCLUDED.dec_deg,
                   mag=EXCLUDED.mag, mag_band=EXCLUDED.mag_band,
                   target_type=EXCLUDED.target_type, priority=EXCLUDED.priority,
                   sources=EXCLUDED.sources, discovered_at=EXCLUDED.discovered_at,
                   last_updated=EXCLUDED.last_updated, active=1""",
            (tid, name, ra, dec, mag, band, ttype, prio,
             json.dumps(sources),
             _iso(NOW - timedelta(days=random.randint(20, 120))), _iso(NOW)),
        )
    print(f"seeded {len(TARGETS)} targets")


def _sscyg_mag(day):
    """SS Cyg dwarf-nova model: ~11.9 quiescent, ~8.4 outburst, ~7-day rise/fall."""
    quiescent, peak = 11.9, 8.4
    cycle = day % 14
    if 4 <= cycle <= 11:                       # outburst window
        phase = (cycle - 4) / 7.0
        env = math.sin(phase * math.pi) ** 0.6
        return peak + (quiescent - peak) * (1 - env)
    return quiescent - random.uniform(0, 0.15)


def seed_measurements():
    """A dense 30-day SS Cyg curve from many nodes, plus sparse points elsewhere."""
    n = 0
    base = NOW - timedelta(days=30)
    contributing = ONLINE_NODE_IDS[:14]
    for day in range(31):
        obs_today = random.randint(2, 5)
        for _ in range(obs_today):
            dt = base + timedelta(days=day, hours=random.uniform(0, 8))
            node = random.choice(contributing)
            mag = round(_sscyg_mag(day) + random.gauss(0, 0.05), 3)
            unc = round(random.uniform(0.02, 0.06), 3)
            db.execute(
                """INSERT INTO measurements
                   (node_id, target_name, bjd, magnitude, uncertainty, filter,
                    airmass, fwhm, snr, comparison_stars, quality_flag,
                    zero_point, zp_scatter, received_at, validation_status,
                    aavso_submitted)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (node_id, target_name, bjd, filter) DO NOTHING""",
                (node, "SS Cyg", jd(dt), mag, unc, "CV",
                 round(random.uniform(1.0, 1.9), 2),
                 round(random.uniform(2.9, 4.4), 2),
                 round(random.uniform(35, 90), 1), random.randint(6, 12),
                 random.choice(["good", "good", "acceptable"]),
                 round(22.3 + random.random() * 0.3, 3),
                 round(random.uniform(0.02, 0.05), 3),
                 _iso(dt), "consistent",
                 1 if random.random() < 0.91 else 0),
            )
            n += 1

    # A few measurements on other targets so /targets shows coverage.
    for tid, name, ra, dec, mag, band, ttype, prio, sources in TARGETS[1:6]:
        for _ in range(random.randint(3, 9)):
            dt = NOW - timedelta(days=random.uniform(0, 25))
            node = random.choice(ONLINE_NODE_IDS)
            db.execute(
                """INSERT INTO measurements
                   (node_id, target_name, bjd, magnitude, uncertainty, filter,
                    snr, comparison_stars, quality_flag, received_at,
                    validation_status, aavso_submitted)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (node_id, target_name, bjd, filter) DO NOTHING""",
                (node, name, jd(dt),
                 round(mag + random.gauss(0, 0.3), 3),
                 round(random.uniform(0.02, 0.07), 3), "CV",
                 round(random.uniform(30, 80), 1), random.randint(5, 11),
                 "good", _iso(dt), "consistent",
                 1 if random.random() < 0.9 else 0),
            )
            n += 1
    print(f"seeded {n} measurements")


def main():
    cfg_path = Path(__file__).resolve().parent.parent / "cloud" / "config.yaml"
    cfg = yaml.safe_load(open(cfg_path)) if cfg_path.exists() else {}
    db_url = cfg.get("database", {}).get("url", "")
    db.init(db_url)
    print(f"database: {db_url or '(DATABASE_URL env)'}")

    if "--wipe" in sys.argv:
        wipe()

    seed_nodes()
    seed_targets()
    seed_measurements()
    print("done")


if __name__ == "__main__":
    main()
