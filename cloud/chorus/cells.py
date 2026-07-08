#!/usr/bin/env python3
"""
CHORUS information cells — the unit of scientific value (CHORUS.md §2.1, §5).

Each active target is compiled into a finite set of cells: the atomic "things
worth knowing" tonight, each with a value density nu, a precision requirement
sigma_ref, and a residual (how much of it is still uncaptured — persisted
across nights for phase/campaign cells via chorus_target_state).

Class strategy templates are declarative: family_of() maps a target_type to a
template family, compile_cells() turns (target, state, night span) into cells.
Kernels measure how much of a cell one observation touches:

    time / phase / band cells → point semantics: a sample inside the locus
        captures it fully; outside, value decays with distance (Gaussian).
    event cells (transit sub-windows) → coverage semantics: value scales with
        the fraction of the sub-window the dwell actually covers.

Pure stdlib; no DB, no cloud imports.  All datetimes are aware UTC.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from cloud.chorus import params as chorus_params

PHASE_BINS = 16          # EB phase-coverage resolution
MAX_TIME_CELLS = 24      # cap per target per cycle


@dataclass
class InfoCell:
    cell_id: str
    target_id: str
    kind: str                       # "time" | "phase" | "event" | "band"
    nu: float                       # value density (already scarcity-scaled)
    sigma_ref: float                # mag precision at which the cell saturates
    residual: float = 1.0           # r_u — fraction of value still uncaptured
    t0: Optional[datetime] = None   # time locus (time/event/band cells)
    t1: Optional[datetime] = None
    phase0: Optional[float] = None  # phase locus (phase cells)
    phase1: Optional[float] = None
    band: Optional[str] = None      # band requirement (band cells)
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id, "target_id": self.target_id,
            "kind": self.kind, "nu": round(self.nu, 5),
            "sigma_ref": self.sigma_ref, "residual": round(self.residual, 5),
            "t0": self.t0.isoformat() if self.t0 else None,
            "t1": self.t1.isoformat() if self.t1 else None,
            "phase0": self.phase0, "phase1": self.phase1,
            "band": self.band, "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InfoCell":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        return cls(
            cell_id=d["cell_id"], target_id=d["target_id"], kind=d["kind"],
            nu=float(d["nu"]), sigma_ref=float(d["sigma_ref"]),
            residual=float(d.get("residual", 1.0)),
            t0=_dt(d.get("t0")), t1=_dt(d.get("t1")),
            phase0=d.get("phase0"), phase1=d.get("phase1"),
            band=d.get("band"), label=d.get("label", ""),
        )


# ── Class families ────────────────────────────────────────────────────────────

_FAMILY_BY_TYPE = {
    "EB": "eb", "EA": "eb", "EB*": "eb", "EW": "eb", "ECLIPSING": "eb",
    "EXOPLANET": "exoplanet",
    "CV": "cv", "UG": "cv", "UGSS": "cv", "UGSU": "cv", "NL": "cv", "AM": "cv",
    "SN": "transient", "SNE": "transient", "AT": "transient",
    "NOVA": "transient", "TDE": "transient", "TRANSIENT": "transient",
    "LPV": "lpv", "MIRA": "lpv", "M": "lpv", "SR": "lpv", "SRB": "lpv", "L": "lpv",
}


def family_of(target_type: str) -> str:
    return _FAMILY_BY_TYPE.get((target_type or "unknown").strip().upper(), "default")


# ── Kernels ───────────────────────────────────────────────────────────────────

def phase_of(when: datetime, ephemeris: dict) -> Optional[float]:
    """Orbital phase in [0,1) from a {period_days, epoch_jd|epoch_iso} ephemeris."""
    try:
        period = float(ephemeris["period_days"])
        if period <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if ephemeris.get("epoch_iso"):
        try:
            epoch = datetime.fromisoformat(str(ephemeris["epoch_iso"]))
        except ValueError:
            return None
        days = (when - epoch).total_seconds() / 86400.0
    else:
        try:
            epoch_jd = float(ephemeris["epoch_jd"])
        except (KeyError, TypeError, ValueError):
            return None
        jd = when.timestamp() / 86400.0 + 2440587.5
        days = jd - epoch_jd
    return days / period % 1.0


def _phase_dist(p: float, lo: float, hi: float) -> float:
    """Wrap-aware distance of phase p from the bin [lo, hi) (0 when inside)."""
    if lo <= p < hi:
        return 0.0
    d_lo = min(abs(p - lo), 1.0 - abs(p - lo))
    d_hi = min(abs(p - hi), 1.0 - abs(p - hi))
    return min(d_lo, d_hi)


def kernel(cell: InfoCell, obs_start: datetime, obs_end: datetime,
           obs_band: str, ephemeris: Optional[dict], params: dict) -> float:
    """Proximity of one observation (dwell [obs_start, obs_end], filter
    obs_band) to a cell — the k_t term of the capture coefficient."""
    if cell.kind == "event":
        if cell.t0 is None or cell.t1 is None:
            return 0.0
        span = (cell.t1 - cell.t0).total_seconds()
        if span <= 0:
            return 0.0
        overlap = (min(cell.t1, obs_end) - max(cell.t0, obs_start)).total_seconds()
        return max(0.0, min(1.0, overlap / span))

    if cell.kind == "band" and (cell.band or "") not in ("", obs_band):
        return 0.0

    mid = obs_start + (obs_end - obs_start) / 2

    if cell.kind == "phase":
        if ephemeris is None or cell.phase0 is None or cell.phase1 is None:
            return 0.0
        p = phase_of(mid, ephemeris)
        if p is None:
            return 0.0
        dist = _phase_dist(p, cell.phase0, cell.phase1)
        scale = max(float(params.get("kernel_phase_scale", 0.04)), 1e-3)
        return math.exp(-((dist / scale) ** 2))

    # time / band cells: point semantics with Gaussian tails outside the locus
    if cell.t0 is None or cell.t1 is None:
        return 0.0
    if cell.t0 <= mid <= cell.t1:
        return 1.0
    gap_h = min(abs((mid - cell.t0).total_seconds()),
                abs((mid - cell.t1).total_seconds())) / 3600.0
    scale = max(float(params.get("kernel_time_scale_h", 3.0)), 0.1)
    return math.exp(-((gap_h / scale) ** 2))


# ── Template compilation ──────────────────────────────────────────────────────

def _time_cells(target: dict, t0: datetime, t1: datetime, *, width_h: float,
                nu: float, sigma_ref: float, kind_label: str) -> list:
    span_h = max((t1 - t0).total_seconds() / 3600.0, 0.5)
    width_h = max(0.5, min(width_h, span_h))
    n = min(MAX_TIME_CELLS, max(1, int(math.ceil(span_h / width_h))))
    width = timedelta(hours=span_h / n)
    tid = target["target_id"]
    return [
        InfoCell(cell_id=f"{tid}:time:{i}", target_id=tid, kind="time",
                 nu=nu, sigma_ref=sigma_ref,
                 t0=t0 + i * width, t1=t0 + (i + 1) * width,
                 label=f"{kind_label} bin {i}")
        for i in range(n)
    ]


def _eb_phase_cells(target: dict, state: dict, nu_base: float) -> list:
    tid = target["target_id"]
    coverage = state.get("phase_coverage") or []
    cells = []
    for i in range(PHASE_BINS):
        lo, hi = i / PHASE_BINS, (i + 1) / PHASE_BINS
        center = (lo + hi) / 2.0
        # Eclipses carry the timing science; quadratures the O'Connell effect.
        if min(center, 1.0 - center) < 0.07 or abs(center - 0.5) < 0.07:
            weight, label = 2.5, "eclipse"
        elif abs(center - 0.25) < 0.07 or abs(center - 0.75) < 0.07:
            weight, label = 1.5, "quadrature"
        else:
            weight, label = 1.0, "phase"
        residual = 1.0
        if i < len(coverage):
            try:
                residual = max(0.0, min(1.0, float(coverage[i])))
            except (TypeError, ValueError):
                residual = 1.0
        cells.append(InfoCell(
            cell_id=f"{tid}:phase:{i}", target_id=tid, kind="phase",
            nu=nu_base * weight, sigma_ref=0.03, residual=residual,
            phase0=lo, phase1=hi, label=f"{label} φ[{lo:.2f},{hi:.2f})"))
    return cells


def _hours_since(iso: Optional[str], now: datetime) -> float:
    if not iso:
        return 24.0 * 30.0
    try:
        return max(0.0, (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0)
    except ValueError:
        return 24.0 * 30.0


def compile_cells(target: dict, state: dict, t0: datetime, t1: datetime,
                  params: dict, scarcity: float,
                  band_union: Optional[set] = None) -> list:
    """Tonight's information cells for one target (CHORUS.md §5).

    target — targets-table row; state — chorus_target_state dict (may be {});
    [t0, t1] — the fleet-wide planning span; scarcity — the T1 multiplier;
    band_union — filters available anywhere in tonight's fleet (band cells
    only exist when someone can capture them).
    """
    fam = family_of(target.get("target_type", ""))
    priority = max(0.05, min(1.0, float(target.get("priority", 0.5) or 0.5)))
    vs = chorus_params.value_scale(params, fam)
    nu_base = vs * priority * max(0.0, min(1.0, scarcity))
    cadence_h = max(0.5, float(target.get("cadence_hours", 24.0) or 24.0))
    ephemeris = state.get("ephemeris") or {}
    now = t0 if t0.tzinfo else t0.replace(tzinfo=timezone.utc)

    if fam == "eb" and ephemeris.get("period_days"):
        return _eb_phase_cells(target, state, nu_base)

    if fam == "cv":
        if state.get("outburst"):
            # Campaign escalation: dense, precise time coverage.
            return _time_cells(target, t0, t1, width_h=1.0,
                               nu=nu_base * 1.6, sigma_ref=0.03,
                               kind_label="outburst")
        # Quiescent monitoring: one detection-quality cell whose value is the
        # hazard of a missed state change since the last accepted sample.
        tau_h = max(1.0, float(state.get("hazard_tau_h", 48.0) or 48.0))
        dt_h = _hours_since(state.get("last_accepted_utc"), now)
        hazard = 1.0 - math.exp(-dt_h / tau_h)
        tid = target["target_id"]
        return [InfoCell(cell_id=f"{tid}:time:0", target_id=tid, kind="time",
                         nu=nu_base * max(hazard, 0.02), sigma_ref=0.10,
                         t0=t0, t1=t1, label="quiescent detection")]

    if fam == "transient":
        age_d = _hours_since(target.get("discovered_at"), now) / 24.0
        seg = 1.5 if age_d < 5 else (1.0 if age_d < 20 else 0.6)
        out = _time_cells(target, t0, t1, width_h=cadence_h,
                          nu=nu_base * seg, sigma_ref=0.05,
                          kind_label="lightcurve")
        # Color cells exist only when someone in tonight's fleet has the band.
        for b in sorted((band_union or set()) & {"B", "V", "R"}):
            tid = target["target_id"]
            out.append(InfoCell(
                cell_id=f"{tid}:band:{b}", target_id=tid, kind="band",
                nu=nu_base * 0.4 * seg, sigma_ref=0.06,
                t0=t0, t1=t1, band=b, label=f"color {b}"))
        return out

    if fam == "lpv":
        return _time_cells(target, t0, t1, width_h=max(cadence_h, 24.0),
                           nu=nu_base, sigma_ref=0.08, kind_label="lpv")

    # default (generic variables, EBs without an ephemeris, unknowns)
    return _time_cells(target, t0, t1, width_h=cadence_h,
                       nu=nu_base, sigma_ref=0.05, kind_label="cadence")


def transit_cells(target_id: str, name: str, t_mid: datetime,
                  duration_hours: float, depth_ppt: float,
                  obs_start: datetime, obs_end: datetime,
                  params: dict, scarcity: float) -> list:
    """Event sub-window cells for one transit: baselines pay their own way,
    ingress/egress carry the timing science (CHORUS.md §5)."""
    vs = chorus_params.value_scale(params, "exoplanet")
    depth_factor = max(0.5, min(1.5, (depth_ppt or 5.0) / 10.0))
    nu = vs * depth_factor * max(0.0, min(1.0, scarcity))
    sigma_ref = max(0.004, 1.0857 * max(depth_ppt or 5.0, 1.0) / 1000.0 / 3.0)
    half = timedelta(hours=max(duration_hours, 0.2) / 2.0)
    third = timedelta(hours=max(duration_hours, 0.2) / 3.0)
    ingress, egress = t_mid - half, t_mid + half

    windows = [
        ("pre",     obs_start,        ingress,          0.5),
        ("ingress", ingress,          ingress + third,  1.0),
        ("mid",     ingress + third,  egress - third,   0.8),
        ("egress",  egress - third,   egress,           1.0),
        ("post",    egress,           obs_end,          0.5),
    ]
    out = []
    for i, (label, w0, w1, weight) in enumerate(windows):
        if w1 <= w0:
            continue
        out.append(InfoCell(
            cell_id=f"{target_id}:event:{i}", target_id=target_id,
            kind="event", nu=nu * weight, sigma_ref=sigma_ref,
            t0=w0, t1=w1, label=f"{name} {label}"))
    return out
