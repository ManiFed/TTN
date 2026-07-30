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

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _coerce(value: Any, typ: Any) -> Any:
    """Coerce an untrusted dict value to a dataclass field's declared type.

    Payloads arrive over the network; without coercion a wrongly-typed field
    (a dict where a string belongs, a string where a float belongs) flows all
    the way into SQL parameters or arithmetic before blowing up far from the
    input boundary. Raises ValueError when the value cannot represent the type.
    """
    if value is None:
        return value
    if typ is str:
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)
    if typ is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    try:
        if typ is float:
            return float(value)
        if typ is int:
            return int(float(value))
    except (TypeError, ValueError):
        raise ValueError(f"cannot convert {value!r} to {typ.__name__}")
    return value


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys and coercing
    values to the declared field types (ValueError on impossible values)."""
    fields = cls.__dataclass_fields__  # type: ignore[attr-defined]
    if not isinstance(data, dict):
        data = {}
    kwargs = {}
    for k, v in data.items():
        f = fields.get(k)
        if f is None:
            continue
        try:
            kwargs[k] = _coerce(v, f.type if not isinstance(f.type, str)
                                else {"str": str, "float": float, "int": int,
                                      "bool": bool}.get(f.type, object))
        except ValueError as exc:
            raise ValueError(f"{cls.__name__}.{k}: {exc}")
    return cls(**kwargs)


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
    vacation_from: str = ""          # ISO date "YYYY-MM-DD"; empty means immediate start
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
    # Versioned execution contract.  These fields are additive so older node
    # agents can continue using startTime while upgraded agents use absolute
    # UTC windows and durable item identities.
    item_id: str = ""
    starts_at_utc: str = ""
    latest_start_utc: str = ""
    task_type: str = "science"              # science | calibration | event_tile
    campaign_id: str = ""
    priority: float = 0.0
    cancellation_generation: int = 0

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
            "item_id":          self.item_id,
            "starts_at_utc":    self.starts_at_utc,
            "latest_start_utc": self.latest_start_utc,
            "task_type":        self.task_type,
            "campaign_id":      self.campaign_id,
            "priority":         self.priority,
            "cancellation_generation": self.cancellation_generation,
        }


@dataclass
class ObservationPlan:
    """A complete nightly plan for one node."""
    plan_id: str = ""
    node_id: str = ""
    night: str = ""                  # "YYYY-MM-DD" (local evening date)
    generated_at: str = ""           # ISO timestamp
    items: list = field(default_factory=list)   # list[PlanItem | dict]
    # CHORUS contingency ladder (additive; empty for legacy planners, ignored
    # by node agents that don't understand it).  See CHORUS.md §6 T3.
    contingencies: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "plan_id":      self.plan_id,
            "node_id":      self.node_id,
            "night":        self.night,
            "generated_at": self.generated_at,
            "items": [i.to_dict() if isinstance(i, PlanItem) else i for i in self.items],
        }
        if self.contingencies:
            out["contingencies"] = self.contingencies
        return out

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
    bjd: float = 0.0                 # BJD_TDB, mid-exposure — the science timestamp
    hjd: Optional[float] = None      # HJD_UTC, same instant — what AAVSO accepts
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
    item_id: str = ""
    bundle_id: str = ""
    response_fingerprint: str = ""
    instrumental_magnitude: Optional[float] = None
    network_magnitude: Optional[float] = None
    network_uncertainty: Optional[float] = None
    calibration_correction: Optional[float] = None
    calibration_model_version: str = ""
    calibration_state: str = ""
    magnitude_system: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Measurement":
        return _from_dict(cls, data)

    def is_valid(self) -> bool:
        """Basic sanity bounds — rejects garbage before it reaches the database.

        Any field may arrive as an explicit JSON null (None survives type
        coercion), so a comparison failure means invalid, not a crash.
        """
        try:
            return (
                bool(self.target_name)
                and 2400000.0 < self.bjd < 2500000.0
                and -5.0 < self.magnitude < 30.0
                and 0.0 <= self.uncertainty < 5.0
                and self.quality_flag in ("good", "acceptable", "poor")
            )
        except TypeError:
            return False


# ── Network science expansion contracts ──────────────────────────────────────

@dataclass
class CalibrationSample:
    response_fingerprint: str = ""
    response_family: str = ""
    frame_id: str = ""
    source_key: str = ""
    node_id: str = ""
    bjd: float = 0.0
    filter: str = "CV"
    instrumental_mag: float = 0.0
    instrumental_err: float = 0.05
    frame_zero_point: float = 0.0
    catalog_mag: float = 0.0
    catalog_err: float = 0.05
    catalog_color: Optional[float] = None
    airmass: Optional[float] = None
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CalibrationSample":
        return _from_dict(cls, data)


@dataclass
class PhotometricModel:
    model_version: str = ""
    response_fingerprint: str = ""
    response_family: str = ""
    filter: str = "CV"
    state: str = "collecting"
    offset: float = 0.0
    color_term: float = 0.0
    extinction: float = 0.0
    drift_per_day: float = 0.0
    pivot_color: float = 0.0
    model_uncertainty: float = 0.0
    validation: dict = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PhotometricModel":
        return _from_dict(cls, data)


@dataclass
class NetworkEvent:
    event_id: str = ""
    source_event_id: str = ""
    source: str = ""
    mission: str = ""
    topic: str = ""
    schema_version: str = ""
    event_class: str = "unknown"
    role: str = "observation"
    revision: int = 0
    notice_type: str = "initial"
    status: str = "received"
    event_time: str = ""
    received_time: str = ""
    significance: dict = field(default_factory=dict)
    localization_type: str = "none"
    localization: dict = field(default_factory=dict)
    area50_deg2: Optional[float] = None
    area90_deg2: Optional[float] = None
    distance: dict = field(default_factory=dict)
    policy: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NetworkEvent":
        return _from_dict(cls, data)


@dataclass
class EventTile:
    tile_id: str = ""
    event_id: str = ""
    event_revision: int = 0
    ra_deg: float = 0.0
    dec_deg: float = 0.0
    radius_deg: float = 0.0
    probability_mass: float = 0.0
    pass_number: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ObservationTask:
    task_id: str = ""
    node_id: str = ""
    event_id: str = ""
    event_revision: int = 0
    tile_id: str = ""
    ra_deg: float = 0.0
    dec_deg: float = 0.0
    earliest_utc: str = ""
    latest_utc: str = ""
    exposure: dict = field(default_factory=dict)
    priority: float = 0.0
    state: str = "pending"
    cancellation_generation: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecutionOutcome:
    attempt_id: str = ""
    item_id: str = ""
    bundle_id: str = ""
    task_id: str = ""
    node_id: str = ""
    state: str = ""
    started_at: str = ""
    finished_at: str = ""
    frames_attempted: int = 0
    frames_completed: int = 0
    last_checkpoint: str = ""
    failure_reason: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionOutcome":
        return _from_dict(cls, data)


@dataclass
class AutonomyBundle:
    schema_version: int = 1
    bundle_id: str = ""
    sequence: int = 0
    node_id: str = ""
    plan_id: str = ""
    issued_at: str = ""
    valid_from: str = ""
    expires_at: str = ""
    minimum_agent_version: str = "1"
    items: list = field(default_factory=list)
    contingencies: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)
    safety_policy_version: str = "1"
    config_fingerprint: str = ""
    signing_key_id: str = ""
    next_public_key: dict = field(default_factory=dict)
    signature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutonomyBundle":
        return _from_dict(cls, data)
