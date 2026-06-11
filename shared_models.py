#!/usr/bin/env python3
"""
Shared data models — used by both the Node Agent and the Boundless Skies cloud.

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

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ── Node registry ──────────────────────────────────────────────────────────────

@dataclass
class NodeInfo:
    """One registered telescope node."""
    node_id: str = ""
    owner_name: str = ""
    owner_email: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    elevation: float = 0.0
    city: str = ""
    country: str = ""
    utc_offset_hours: float = 0.0
    telescope_model: str = "ZWO Seestar S50"
    aperture_mm: float = 50.0
    focal_length_mm: float = 250.0
    fov_deg: float = 1.27            # diagonal field of view
    pixel_scale_arcsec: float = 2.4
    filters: str = "CV"              # comma-separated available filters
    mag_bright_limit: float = 6.0    # saturates brighter than this
    mag_faint_limit: float = 15.5    # SNR too low fainter than this
    min_altitude_deg: float = 25.0
    max_exposure_s: float = 30.0     # alt-az field rotation limit per sub
    light_pollution_mpsas: float = 20.0   # sky brightness, mag/arcsec²
    bortle: int = 5
    status: str = "active"           # active | offline | disabled

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NodeInfo":
        return _from_dict(cls, data)


# ── Targets ────────────────────────────────────────────────────────────────────

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

    def to_dict(self) -> dict:
        return asdict(self)

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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlanItem":
        return _from_dict(cls, data)

    def to_node_item(self) -> dict:
        """Strip down to the exact dict the node schedule runner consumes."""
        return {
            "target":    self.target,
            "ra":        self.ra,
            "dec":       self.dec,
            "expDur":    self.expDur,
            "expCount":  self.expCount,
            "binning":   self.binning,
            "startTime": self.startTime,
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
