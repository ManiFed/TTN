#!/usr/bin/env python3
"""
Shared data models — used by both the Node Agent and the The Telescope Net cloud.

Plain dataclasses with dict round-tripping so the node can keep working with
the plain dicts it already uses (photometry.run_pipeline output, schedule
items) while the cloud gets typed structure.  Nothing here imports Flask,
astropy, or anything heavy — both sides can import this for free.

    NodeInfo          — registry entry for one telescope node
    TargetInfo        — a deduplicated science target from alert ingestion
    PlanItem          — one scheduled observation (node schedule-runner format)
    ObservationPlan   — a full nightly plan for one node
    Measurement       — one photometry result (photometry.run_pipeline format)
"""

import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


_ENV_RE = re.compile(r"\$\{(\w+)\}")


def expand_env(value: Any) -> Any:
    """Resolve ${VAR} references in a config string against the environment.

    Secrets (AAVSO password, cloud API key, …) live in the environment, not in
    the tracked config file, so config values like ``${AAVSO_PASSWORD}`` are
    expanded here at the point of use.  Non-strings pass through unchanged, and
    an unset variable expands to "" so callers fall back to their own defaults.
    """
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    return value


# ── Node registry ──────────────────────────────────────────────────────────────

@dataclass
class NodeInfo:
    """
    One registered telescope node.

    Fields are grouped by what the scheduler uses them for.  Hardware fields
    describe physical capability; performance fields are recomputed nightly from
    the measurements table and feed directly into reliability_score, which the
    scorer applies as a multiplier on every (target, node) score pair.
    """

    # ── Identity ───────────────────────────────────────────────────────────────
    node_id: str = ""
    owner_name: str = ""
    owner_email: str = ""

    # ── Location ───────────────────────────────────────────────────────────────
    latitude: float = 0.0
    longitude: float = 0.0
    elevation: float = 0.0
    city: str = ""
    country: str = ""
    utc_offset_hours: float = 0.0

    # ── Sky quality ────────────────────────────────────────────────────────────
    light_pollution_mpsas: float = 20.0  # sky brightness (mag/arcsec²)
    bortle: int = 5

    # JSON [[alt_deg, az_deg], ...] polygon of local horizon obstructions
    horizon_mask: str = "[]"

    # ── Hardware: telescope ────────────────────────────────────────────────────
    tier: int = 1                    # 1=Seestar, 2=Filtered, 3=Spectroscopy
    telescope_model: str = "ZWO Seestar S50"
    telescope_serial: str = ""       # ALPACA UniqueID (physical device identifier)
    telescope_name: str = ""         # ALPACA DeviceName (human-readable label from scope)
    aperture_mm: float = 50.0
    focal_length_mm: float = 250.0
    fov_deg: float = 1.27
    pixel_scale_arcsec: float = 2.4
    mount_type: str = "alt_az"       # alt_az | equatorial
    max_exposure_s: float = 30.0     # field-rotation limit (alt-az)

    # ── Hardware: camera ──────────────────────────────────────────────────────
    camera_model: str = ""
    cooled_camera: bool = False      # TEC cooled → lower noise, fainter limit

    # ── Hardware: filters / photometry ────────────────────────────────────────
    filter_set: str = '["CV"]'       # JSON list, e.g. '["B","V","R","I"]'
    filters: str = "CV"              # legacy comma-separated; keep for compat
    mag_bright_limit: float = 6.0
    mag_faint_limit: float = 15.5
    min_altitude_deg: float = 25.0

    # ── Hardware: autonomy ────────────────────────────────────────────────────
    # These flags determine how well the node can run unattended overnight.
    # The scheduler gives a small bonus to nodes with higher autonomy because
    # they are more likely to complete a night without human intervention.
    has_dew_heater: bool = False     # prevents lens fogging in humid weather
    has_power_mgmt: bool = False     # smart power box: can remotely cycle Seestar
    has_enclosure: bool = False      # dome/minidome: operates in light rain/wind
    has_ups: bool = False            # survives brief power cuts

    # ── Portability & session ─────────────────────────────────────────────────
    portable: bool = False           # moves between sites; sleeps between sessions
    vacation_until: str = ""         # ISO date "YYYY-MM-DD"; empty when not on vacation
    session_lat: float = 0.0         # tonight's observing location (overrides home coords)
    session_lon: float = 0.0
    session_city: str = ""
    session_site_name: str = ""
    previous_locations: str = "[]"   # JSON [{lat,lon,city,site_name,last_used}] newest-first

    # ── Status ────────────────────────────────────────────────────────────────
    status: str = "active"           # active | sleeping | vacation | disabled

    # ── Scheduler hints (operator-provided) ───────────────────────────────────
    scheduling_notes: str = ""       # free text, e.g. "south blocked past az 200"
    preferred_targets: str = "[]"    # JSON list of target types this node excels at

    # ── Performance metrics (recomputed nightly) ──────────────────────────────
    # Read by the scheduler; never set by the node agent directly.
    total_observations: int = 0
    aavso_accepted: int = 0
    aavso_rejected: int = 0          # cross-val outliers that were not submitted
    mean_uncertainty: float = 0.0    # typical photometric precision (mag)
    mean_fwhm: float = 0.0           # typical seeing (pixels)
    clear_nights_30d: int = 0        # distinct nights with ≥1 obs in last 30 days
    outlier_rate: float = 0.0        # fraction of obs flagged as cross-val outlier

    # Composite 0..1 multiplier applied to every scheduler score for this node.
    # New nodes start at 0.50.  Formula:
    #   0.40 × aavso_acceptance_rate
    # + 0.25 × (1 − outlier_rate)
    # + 0.20 × (clear_nights_30d / 30)
    # + 0.15 × precision_factor          (= max(0, 1 − mean_uncertainty / 0.3))
    reliability_score: float = 0.5
    scheduler_trust_score: float = 0.5  # reliability after recent incident penalties

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NodeInfo":
        return _from_dict(cls, data)


# ── Targets ────────────────────────────────────────────────────────────────────

_SCIENCE_PROGRAM_MAP: dict = {
    "SN":        "transient_follow_up",
    "TDE":       "transient_follow_up",
    "GRB":       "transient_follow_up",
    "NOVA":      "transient_follow_up",
    "CV":        "variable_stars",
    "VAR":       "variable_stars",
    "EB":        "variable_stars",
    "AGN":       "variable_stars",
    "YSO":       "variable_stars",
    "EXOPLANET": "exoplanet_transits",
}


def science_program_for_type(target_type: str) -> str:
    """Map a target_type string to its science program name."""
    return _SCIENCE_PROGRAM_MAP.get((target_type or "").upper(), "other")


@dataclass
class TargetInfo:
    """A deduplicated, cross-matched science target."""
    target_id: str = ""
    name: str = ""
    ra_deg: float = 0.0
    dec_deg: float = 0.0
    mag: Optional[float] = None      # latest reported magnitude
    mag_band: str = ""
    target_type: str = ""            # SN | CV | TDE | VAR | EB | AGN | GRB | unknown
    priority: float = 0.5            # 0..1 scientific value baseline
    time_critical: bool = False
    cadence_hours: float = 24.0      # desired re-observation cadence
    sources: list = field(default_factory=list)   # ["alerce", "gaia", ...]
    discovered_at: str = ""          # ISO timestamp of first alert
    active: bool = True

    @property
    def science_program(self) -> str:
        return science_program_for_type(self.target_type)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["science_program"] = self.science_program
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TargetInfo":
        return _from_dict(cls, data)


# ── Plans ──────────────────────────────────────────────────────────────────────

@dataclass
class PlanItem:
    """
    One scheduled observation.

    Field names match the node dashboard schedule runner exactly
    (target, ra in decimal HOURS, dec in degrees, expDur, expCount, binning,
    startTime "HH:MM" in node-local time) so the plan can be POSTed straight
    to /api/schedule/run or executed by _run_schedule_bg unchanged.
    """
    target: str = ""
    ra: float = 0.0                  # decimal hours
    dec: float = 0.0                 # degrees
    expDur: float = 10.0             # seconds per sub-frame
    expCount: int = 20
    binning: int = 1
    startTime: str = ""              # "HH:MM" node-local
    # Cloud-side metadata (ignored by the node schedule validator)
    target_id: str = ""
    score: float = 0.0
    filter: str = "CV"
    notes: str = ""
    explanation: dict = field(default_factory=dict)
    # Observation strategy
    observation_mode: str = "single_epoch"  # "single_epoch" | "time_series"
    duration_minutes: float = 0.0           # for time_series: how long to stay on target

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanItem":
        return _from_dict(cls, data)

    def to_node_item(self) -> dict:
        """Strip down to the exact dict the node schedule runner consumes."""
        return {
            "target":           self.target,
            "ra":               self.ra,
            "dec":              self.dec,
            "expDur":           self.expDur,
            "expCount":         self.expCount,
            "binning":          self.binning,
            "startTime":        self.startTime,
            "observation_mode": self.observation_mode,
            "duration_minutes": self.duration_minutes,
        }


@dataclass
class ObservationPlan:
    """A complete nightly plan for one node."""
    plan_id: str = ""
    node_id: str = ""
    night: str = ""                  # "YYYY-MM-DD" (local evening date)
    generated_at: str = ""           # ISO timestamp
    items: list = field(default_factory=list)   # list[PlanItem | dict]

    def to_dict(self) -> dict:
        return {
            "plan_id":      self.plan_id,
            "node_id":      self.node_id,
            "night":        self.night,
            "generated_at": self.generated_at,
            "items": [i.to_dict() if isinstance(i, PlanItem) else i for i in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ObservationPlan":
        data = dict(data or {})
        items = [PlanItem.from_dict(i) if isinstance(i, dict) else i
                 for i in data.pop("items", [])]
        plan = _from_dict(cls, data)
        plan.items = items
        return plan


# ── Measurements ───────────────────────────────────────────────────────────────

@dataclass
class Measurement:
    """
    One photometry measurement.  Field names match photometry.run_pipeline()
    output exactly, so `Measurement.from_dict(result)` works on the node and
    the cloud can validate uploads with the same model.
    """
    target_name: str = ""
    bjd: float = 0.0
    magnitude: float = 0.0
    uncertainty: float = 0.0
    filter: str = "CV"
    airmass: Optional[float] = None
    fwhm: Optional[float] = None
    snr: Optional[float] = None
    comparison_stars: int = 0
    quality_flag: str = "poor"       # good | acceptable | poor
    node_id: str = ""
    zero_point: Optional[float] = None
    zp_scatter: Optional[float] = None
    fits_file: str = ""
    sky_mag: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Measurement":
        return _from_dict(cls, data)

    def is_valid(self) -> bool:
        """Basic sanity bounds — rejects garbage before it reaches the database."""
        return (
            bool(self.target_name)
            and 2400000.0 < self.bjd < 2500000.0
            and -5.0 < self.magnitude < 30.0
            and 0.0 <= self.uncertainty < 5.0
            and self.quality_flag in ("good", "acceptable", "poor")
        )
