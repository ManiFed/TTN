#!/usr/bin/env python3
"""
Synthetic fleets and target catalogs for the digital twin.

Every simulated node carries two layers:

  * a **declared layer** (`as_row()`) — exactly the nodes-table dict the
    production planner reads: geometry, optics, sensor, site sky, limits.
    This is what CHORUS's physics model and the baselines see.
  * a **truth layer** — hidden parameters the schedulers must NOT see:
    true execution reliability, true acceptance rate, true photometric
    efficiency (kappa), nightly-outage probability, and per-site forecast
    skill.  Only sim.outcomes reads these, when realizing what actually
    happened.  Schedulers learn about them the same way production does:
    from realized outcomes via a Beta-posterior ledger (sim.engine).

Targets follow the same discipline: the declared layer is a targets-table
row; the truth layer holds the processes the scheduler is trying to catch
(CV outburst hazard, transient decay, transit ephemerides).

All generation is deterministic in (seed, config).
"""

import math
import zlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


def sub_rng(seed: int, *tags) -> random.Random:
    """Deterministic child RNG: stable across runs, platforms, and Python
    hash randomization (crc32, not hash())."""
    key = ":".join(str(t) for t in tags)
    return random.Random((seed & 0xFFFFFFFF) ^ zlib.crc32(key.encode("utf-8")))


# ── Hardware classes ──────────────────────────────────────────────────────────
# Declared specs follow src/telescope_specs.py conventions (Seestar-anchored).
# Truth-reliability ranges are per-class priors the generator draws from;
# scenarios can override them wholesale (e.g. unreliable_fleet).

HARDWARE_CLASSES = {
    "seestar_s50": dict(
        telescope_model="ZWO Seestar S50", aperture_mm=50, focal_length_mm=250,
        fov_deg=1.27, pixel_scale_arcsec=2.4, max_exposure_s=30.0,
        cooled_camera=0, mount_type="alt_az", filter_set=["CV"],
        mag_bright_limit=6.0, mag_faint_limit=15.5,
        p_exec_range=(0.75, 0.92), p_accept_range=(0.80, 0.95),
        kappa_range=(1.0, 1.8), weight=0.55,
    ),
    "seestar_s30": dict(
        telescope_model="ZWO Seestar S30", aperture_mm=30, focal_length_mm=150,
        fov_deg=2.0, pixel_scale_arcsec=4.0, max_exposure_s=30.0,
        cooled_camera=0, mount_type="alt_az", filter_set=["CV"],
        mag_bright_limit=5.0, mag_faint_limit=14.0,
        p_exec_range=(0.72, 0.90), p_accept_range=(0.75, 0.92),
        kappa_range=(1.0, 2.0), weight=0.15,
    ),
    "refractor_80": dict(
        telescope_model="80mm APO + CMOS", aperture_mm=80, focal_length_mm=480,
        fov_deg=1.6, pixel_scale_arcsec=1.6, max_exposure_s=120.0,
        cooled_camera=0, mount_type="equatorial", filter_set=["CV", "V"],
        mag_bright_limit=6.5, mag_faint_limit=16.0,
        p_exec_range=(0.60, 0.88), p_accept_range=(0.70, 0.92),
        kappa_range=(1.1, 2.5), weight=0.12,
    ),
    "newt_150": dict(
        telescope_model="150mm Newtonian + CMOS", aperture_mm=150,
        focal_length_mm=750, fov_deg=0.9, pixel_scale_arcsec=1.0,
        max_exposure_s=180.0, cooled_camera=1, mount_type="equatorial",
        filter_set=["CV", "B", "V"],
        mag_bright_limit=7.5, mag_faint_limit=17.0,
        p_exec_range=(0.55, 0.85), p_accept_range=(0.70, 0.92),
        kappa_range=(1.1, 3.0), weight=0.10,
    ),
    "sct_200_cooled": dict(
        telescope_model="200mm SCT cooled", aperture_mm=200,
        focal_length_mm=2000, fov_deg=0.35, pixel_scale_arcsec=0.8,
        max_exposure_s=300.0, cooled_camera=1, mount_type="equatorial",
        filter_set=["CV", "B", "V", "R"],
        mag_bright_limit=8.5, mag_faint_limit=18.0,
        p_exec_range=(0.50, 0.85), p_accept_range=(0.70, 0.93),
        kappa_range=(1.2, 4.0), weight=0.08,
    ),
}

# ── Geographic regions ────────────────────────────────────────────────────────
# (lat_range, lon_range, base clear-night probability, seasonal amplitude,
#  mpsas range).  Clear prob for month m (northern convention):
#  clear = base + amp·cos(2π(m − best_month)/12), clamped to [0.05, 0.95].
# These are coarse climate archetypes, not site forecasts — they only need to
# make "Arizona is clearer than Britain, and monsoons/monsoon-winters exist"
# true.  Every number is overridable per scenario.

REGIONS = {
    "na_southwest":   dict(lat=(31, 40), lon=(-115, -104), clear=0.72, amp=0.10, best_month=10, mpsas=(20.3, 21.6)),
    "na_east":        dict(lat=(33, 45), lon=(-85, -70),   clear=0.48, amp=0.08, best_month=9,  mpsas=(18.5, 20.8)),
    "na_west_coast":  dict(lat=(33, 48), lon=(-124, -117), clear=0.55, amp=0.15, best_month=8,  mpsas=(18.8, 21.0)),
    "europe_north":   dict(lat=(47, 57), lon=(-6, 15),     clear=0.35, amp=0.12, best_month=7,  mpsas=(18.5, 20.8)),
    "europe_south":   dict(lat=(36, 44), lon=(-9, 26),     clear=0.60, amp=0.15, best_month=7,  mpsas=(19.5, 21.2)),
    "east_asia":      dict(lat=(30, 42), lon=(126, 142),   clear=0.45, amp=0.15, best_month=11, mpsas=(18.0, 20.5)),
    "india":          dict(lat=(10, 28), lon=(72, 88),     clear=0.50, amp=0.25, best_month=1,  mpsas=(18.0, 20.5)),
    "australia":      dict(lat=(-38, -25), lon=(138, 153), clear=0.58, amp=0.10, best_month=3,  mpsas=(19.5, 21.6)),
    "south_america":  dict(lat=(-35, -22), lon=(-71, -48), clear=0.55, amp=0.12, best_month=5,  mpsas=(19.5, 21.5)),
    "southern_africa": dict(lat=(-34, -23), lon=(17, 31),  clear=0.62, amp=0.12, best_month=6,  mpsas=(20.0, 21.6)),
    "hawaii":         dict(lat=(19, 21), lon=(-156, -155), clear=0.65, amp=0.05, best_month=8,  mpsas=(20.5, 21.8)),
    "middle_east":    dict(lat=(24, 33), lon=(34, 46),     clear=0.75, amp=0.08, best_month=9,  mpsas=(19.0, 21.0)),
}

NORTHERN_REGIONS = ["na_southwest", "na_east", "na_west_coast", "europe_north",
                    "europe_south", "east_asia", "india", "hawaii", "middle_east"]
SOUTHERN_REGIONS = ["australia", "south_america", "southern_africa"]


def region_clear_prob(region: str, month: int,
                      overrides: Optional[dict] = None) -> float:
    """Climatological clear-night probability for a region in a month."""
    r = dict(REGIONS[region])
    r.update((overrides or {}).get(region, {}))
    phase = 2.0 * math.pi * ((month - r["best_month"]) % 12) / 12.0
    return max(0.05, min(0.95, r["clear"] + r["amp"] * math.cos(phase)))


# ── Nodes ─────────────────────────────────────────────────────────────────────

@dataclass
class SimNode:
    node_id: str
    hardware_class: str
    region: str
    lat: float
    lon: float
    elevation: float
    mpsas: float
    # truth layer — hidden from schedulers
    p_exec_true: float
    p_accept_true: float
    kappa_true: float
    p_night_up: float          # prob the node comes online at all on a night
    forecast_skill: float      # 0 = forecast is climatology, 1 = forecast is truth

    def as_row(self) -> dict:
        """The declared layer: a nodes-table-shaped dict for the planners."""
        hw = HARDWARE_CLASSES[self.hardware_class]
        return {
            "node_id": self.node_id,
            "latitude": self.lat, "longitude": self.lon,
            "elevation": self.elevation,
            "utc_offset_hours": round(self.lon / 15.0),
            "light_pollution_mpsas": self.mpsas,
            "telescope_model": hw["telescope_model"],
            "aperture_mm": hw["aperture_mm"],
            "focal_length_mm": hw["focal_length_mm"],
            "fov_deg": hw["fov_deg"],
            "pixel_scale_arcsec": hw["pixel_scale_arcsec"],
            "max_exposure_s": hw["max_exposure_s"],
            "cooled_camera": hw["cooled_camera"],
            "mount_type": hw["mount_type"],
            "filter_set": list(hw["filter_set"]),
            "mag_bright_limit": hw["mag_bright_limit"],
            "mag_faint_limit": hw["mag_faint_limit"],
            "min_altitude_deg": 25.0,
            "mean_fwhm": 0.0,
            "status": "active",
        }


def _draw(rng: random.Random, lo_hi: tuple) -> float:
    return rng.uniform(*lo_hi)


def generate_fleet(n_nodes: int, seed: int, *,
                   regions: Optional[list] = None,
                   region_weights: Optional[dict] = None,
                   hardware_mix: Optional[dict] = None,
                   reliability_scale: float = 1.0,
                   forecast_skill_range: tuple = (0.55, 0.85),
                   p_night_up_range: tuple = (0.80, 0.97)) -> list:
    """Deterministic fleet of SimNodes.

    reliability_scale < 1 degrades the whole fleet's true p_exec/p_accept
    (unreliable_fleet scenario); regions / region_weights shape geography
    (southern_gap passes only northern regions).
    """
    rng = sub_rng(seed, "fleet", n_nodes)
    pool = regions or list(REGIONS.keys())
    weights = [float((region_weights or {}).get(r, 1.0)) for r in pool]
    hw_names = list(HARDWARE_CLASSES.keys())
    hw_weights = [float((hardware_mix or {}).get(h, HARDWARE_CLASSES[h]["weight"]))
                  for h in hw_names]

    fleet = []
    for i in range(n_nodes):
        region = rng.choices(pool, weights=weights)[0]
        r = REGIONS[region]
        hw = rng.choices(hw_names, weights=hw_weights)[0]
        spec = HARDWARE_CLASSES[hw]
        p_exec = min(0.98, _draw(rng, spec["p_exec_range"]) * reliability_scale)
        p_accept = min(0.98, _draw(rng, spec["p_accept_range"]) * reliability_scale)
        fleet.append(SimNode(
            node_id=f"sim{i:04d}_{hw.split('_')[0]}",
            hardware_class=hw,
            region=region,
            lat=round(_draw(rng, r["lat"]), 3),
            lon=round(_draw(rng, r["lon"]), 3),
            elevation=round(rng.uniform(0, 2200), 0),
            mpsas=round(_draw(rng, r["mpsas"]), 2),
            p_exec_true=round(max(0.05, p_exec), 3),
            p_accept_true=round(max(0.05, p_accept), 3),
            kappa_true=round(_draw(rng, spec["kappa_range"]), 2),
            p_night_up=round(min(0.99, _draw(rng, p_night_up_range)
                                 * min(1.0, reliability_scale + 0.1)), 3),
            forecast_skill=round(_draw(rng, forecast_skill_range), 3),
        ))
    return fleet


# ── Targets ───────────────────────────────────────────────────────────────────

@dataclass
class SimTarget:
    target_id: str
    name: str
    target_type: str            # EB | EXOPLANET | CV | SN | LPV | VAR
    ra_deg: float
    dec_deg: float
    mag: float                  # current apparent mag (transients evolve)
    priority: float
    cadence_hours: float
    # class truth
    ephemeris: Optional[dict] = None          # EB: {period_days, epoch_jd}
    transit: Optional[dict] = None            # EXOPLANET: {period_days, epoch,
                                              #   duration_hours, depth_ppt}
    cv: Optional[dict] = None                 # CV: {tau_h, outburst_rate_per_night,
                                              #   outburst_nights, outburst_delta_mag}
    discovered_at: Optional[str] = None       # transients: ISO arrival time
    alert_night: Optional[int] = None         # night index the alert arrives
    time_critical: bool = False

    def as_row(self) -> dict:
        """Declared layer: a targets-table-shaped dict."""
        return {
            "target_id": self.target_id, "name": self.name,
            "target_type": self.target_type,
            "ra_deg": self.ra_deg, "dec_deg": self.dec_deg,
            "mag": self.mag, "priority": self.priority,
            "cadence_hours": self.cadence_hours,
            "discovered_at": self.discovered_at,
            "active": 1,
        }


DEFAULT_CLASS_MIX = {
    # fractions of the catalog per class (filler VAR absorbs the remainder)
    "EB": 0.22, "CV": 0.18, "LPV": 0.20, "EXOPLANET": 0.10, "VAR": 0.30,
}


def generate_catalog(n_targets: int, seed: int, epoch: datetime, *,
                     class_mix: Optional[dict] = None,
                     dec_bias_north: float = 0.5,
                     transient_rate_per_night: float = 0.15,
                     n_nights: int = 30,
                     alert_storm_night: Optional[int] = None,
                     alert_storm_count: int = 6) -> list:
    """Deterministic target catalog + scheduled transient arrivals.

    dec_bias_north ∈ [0,1]: 0.5 = symmetric sky; >0.5 favors northern decs.
    Transients are pre-drawn for the whole run (arrival night + position);
    they stay inactive (not in the planner's list) until their alert night.
    """
    rng = sub_rng(seed, "catalog", n_targets)
    mix = dict(DEFAULT_CLASS_MIX)
    mix.update(class_mix or {})

    def draw_dec() -> float:
        # sin(dec)-uniform on the sphere, then hemisphere-biased.
        north = rng.random() < dec_bias_north
        s = rng.random()            # |sin dec| uniform → area-uniform
        dec = math.degrees(math.asin(s))
        return round(dec if north else -dec, 3)

    targets = []
    counts = {c: int(n_targets * f) for c, f in mix.items() if c != "VAR"}
    counts["VAR"] = n_targets - sum(counts.values())
    idx = 0
    for cls, count in counts.items():
        for _ in range(count):
            idx += 1
            tid = f"T{idx:04d}_{cls}"
            ra = round(rng.uniform(0, 360), 3)
            dec = draw_dec()
            t = SimTarget(
                target_id=tid, name=tid, target_type=cls,
                ra_deg=ra, dec_deg=dec,
                mag=0.0, priority=round(rng.uniform(0.4, 0.9), 2),
                cadence_hours=24.0,
            )
            if cls == "EB":
                t.mag = round(rng.uniform(9.0, 13.5), 2)
                t.cadence_hours = 4.0
                t.ephemeris = {"period_days": round(rng.uniform(0.3, 8.0), 4),
                               "epoch_jd": 2460000.0 + rng.uniform(0, 100)}
            elif cls == "CV":
                t.mag = round(rng.uniform(13.0, 16.5), 2)   # quiescent
                t.cadence_hours = 24.0
                t.cv = {"tau_h": round(rng.uniform(24.0, 96.0), 1),
                        "outburst_rate_per_night": rng.uniform(0.01, 0.05),
                        "outburst_nights": rng.randint(3, 10),
                        "outburst_delta_mag": round(rng.uniform(2.0, 5.0), 1)}
            elif cls == "LPV":
                t.mag = round(rng.uniform(7.5, 12.0), 2)
                t.cadence_hours = 72.0
                t.priority = round(rng.uniform(0.3, 0.6), 2)
            elif cls == "EXOPLANET":
                t.mag = round(rng.uniform(9.0, 12.5), 2)
                t.time_critical = True
                t.transit = {
                    "period_days": round(rng.uniform(1.2, 12.0), 4),
                    "epoch": (epoch - timedelta(days=rng.uniform(0, 12))).isoformat(),
                    "duration_hours": round(rng.uniform(1.5, 3.5), 2),
                    "depth_ppt": round(rng.uniform(4.0, 25.0), 1),
                }
            else:   # VAR filler
                t.mag = round(rng.uniform(8.5, 14.0), 2)
                t.cadence_hours = rng.choice([12.0, 24.0, 48.0])
            targets.append(t)

    # ── Transient arrivals (novae / SNe / alerts) ─────────────────────────────
    arrivals = []
    for night in range(n_nights):
        n_new = _poisson(rng, transient_rate_per_night)
        if alert_storm_night is not None and night == alert_storm_night:
            n_new += alert_storm_count
        for _ in range(n_new):
            arrivals.append(night)
    for j, night in enumerate(arrivals):
        idx += 1
        arrive = epoch + timedelta(days=night, hours=rng.uniform(0, 20))
        targets.append(SimTarget(
            target_id=f"T{idx:04d}_SN", name=f"T{idx:04d}_SN",
            target_type="SN",
            ra_deg=round(rng.uniform(0, 360), 3), dec_deg=draw_dec(),
            mag=round(rng.uniform(11.0, 14.5), 2),
            priority=0.95, cadence_hours=6.0,
            discovered_at=arrive.isoformat(), alert_night=night,
            time_critical=True))
    return targets


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm — deterministic small-λ Poisson draw."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1
