#!/usr/bin/env python3
"""
CHORUS measurement model — predicted photometric error for one observation by
one telescope (CHORUS.md §3).

Everything is anchored on the ZWO Seestar S50, the same discipline as
src/telescope_specs.derive_params: a 50 mm aperture at a 30 s sub and V = 10
collects N_REF electrons, and every other scope/sensor/site scales away from
that anchor by physics.  The absolute calibration does not need to be perfect —
per-node kappa (ledger.py) measures how far each node's *delivered* errors sit
from these predictions and inflates them accordingly — but the *relative*
structure (aperture, cooling, pixel scale, field of view, sky, moon, airmass)
is what routes work across a heterogeneous fleet.

Pure stdlib; no DB, no astropy, no cloud imports.  Node hardware is read from
the plain nodes-table dict with Seestar-anchored fallbacks for every field.
"""

import math
from dataclasses import dataclass
from typing import Optional

# ── Anchor constants (Seestar S50 @ V=10, 30 s sub, airmass 1) ────────────────
N_REF_E = 40_000.0        # electrons collected at the anchor point
ANCHOR_MAG = 10.0
ANCHOR_T_SUB = 30.0
ANCHOR_APERTURE_MM = 50.0

DEFAULT_READ_NOISE_E = 5.0
DARK_E_PER_PX_S_COOLED = 0.02
DARK_E_PER_PX_S_UNCOOLED = 0.5
SYS_FLOOR_MAG = 0.005     # flat-field / rotation residual systematic floor
DEFAULT_K_EXT = 0.25      # V-band extinction, mag per airmass (Ring-0 calibrated later)
SLEW_RESERVE_MIN = 5.0    # matches the scheduler's per-target occupancy reserve

# Usable comparison stars (roughly V 9–13) per square degree, by |galactic b|.
# Coarse — it only needs to separate "milky-way rich field" from "galactic pole".
_STAR_DENSITY_BY_GAL_B = [
    (10.0, 300.0),
    (30.0, 120.0),
    (60.0, 60.0),
    (90.0, 35.0),
]
COMP_USABLE_FRACTION = 0.5   # fraction of catalog stars that survive as comps
COMP_TYPICAL_MAG = 11.5      # magnitude at which the ensemble error is evaluated


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── Geometry helpers ──────────────────────────────────────────────────────────

def galactic_latitude_deg(ra_deg: float, dec_deg: float) -> float:
    """Galactic latitude b from equatorial J2000 (for star-field density)."""
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    ra_ngp = math.radians(192.85948)
    dec_ngp = math.radians(27.12825)
    sin_b = (math.sin(dec) * math.sin(dec_ngp)
             + math.cos(dec) * math.cos(dec_ngp) * math.cos(ra - ra_ngp))
    return math.degrees(math.asin(clamp(sin_b, -1.0, 1.0)))


def airmass_from_alt(alt_deg: float) -> float:
    """Plane-parallel with Pickering-style clamp (same shape as cloud.conditions)."""
    if alt_deg <= 3.0:
        return 38.0
    return 1.0 / math.sin(math.radians(alt_deg))


# ── Sky model ─────────────────────────────────────────────────────────────────

def sky_mpsas_effective(mpsas: float, moon_illum: float = 0.0,
                        moon_sep_deg: float = 180.0) -> float:
    """Site sky brightness (mag/arcsec²) brightened by the moon: a global
    illumination term plus a proximity term, clamped to a plausible range."""
    proximity = max(0.0, 1.0 - moon_sep_deg / 120.0)
    brighten = moon_illum * (0.8 + 2.5 * proximity ** 1.5)
    return clamp(mpsas - brighten, 16.0, 22.0)


# ── Node hardware accessors (Seestar-anchored fallbacks) ─────────────────────

def _f(node: dict, key: str, default: float) -> float:
    try:
        v = node.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def node_fwhm_px(node: dict) -> float:
    """Measured PSF width when the node has one; else the derive_params
    fallback clamp(9.6 / pixel_scale, 2, 6)."""
    measured = _f(node, "mean_fwhm", 0.0)
    if measured > 0.5:
        return clamp(measured, 1.5, 12.0)
    scale = _f(node, "pixel_scale_arcsec", 2.4)
    return clamp(9.6 / max(scale, 0.1), 2.0, 6.0)


def psf_npix(node: dict) -> float:
    """Pixels inside the photometric aperture (radius ≈ 1.5 × FWHM)."""
    r = 1.5 * node_fwhm_px(node)
    return math.pi * r * r


# ── Electron budget ───────────────────────────────────────────────────────────

def signal_electrons(node: dict, mag: float, t_sub: float, airmass: float,
                     k_ext: float = DEFAULT_K_EXT) -> float:
    """Star electrons in one sub, scaled from the Seestar anchor by light
    grasp (aperture²), sub length, and atmospheric extinction."""
    aperture = _f(node, "aperture_mm", ANCHOR_APERTURE_MM)
    grasp = (aperture / ANCHOR_APERTURE_MM) ** 2
    m_eff = mag + k_ext * max(0.0, airmass - 1.0)
    return (N_REF_E * grasp * (t_sub / ANCHOR_T_SUB)
            * 10.0 ** (-0.4 * (m_eff - ANCHOR_MAG)))


def sky_electrons_per_px(node: dict, mpsas_eff: float, t_sub: float) -> float:
    """Sky electrons per pixel per sub: the sky is a mag-per-arcsec² source
    observed through pixel_scale² arcsec² pixels, on the same zero point."""
    scale = _f(node, "pixel_scale_arcsec", 2.4)
    px_area = max(scale, 0.05) ** 2
    mag_per_px = mpsas_eff - 2.5 * math.log10(px_area)
    return signal_electrons(node, mag_per_px, t_sub, airmass=1.0, k_ext=0.0)


def scintillation_mag(node: dict, airmass: float, t_sub: float) -> float:
    """Young's scintillation approximation (fractional → mag)."""
    d_cm = max(_f(node, "aperture_mm", ANCHOR_APERTURE_MM), 10.0) / 10.0
    elev_m = _f(node, "elevation", 0.0)
    frac = (0.09 * d_cm ** (-2.0 / 3.0) * airmass ** 1.75
            * math.exp(-elev_m / 8000.0) / math.sqrt(max(2.0 * t_sub, 0.5)))
    return 1.0857 * frac


# ── Ensemble (comparison-star) floor — where FoV earns its keep ──────────────

def comp_star_count(node: dict, ra_deg: float, dec_deg: float) -> float:
    """Expected usable comparison stars in this node's field at this pointing."""
    b = abs(galactic_latitude_deg(ra_deg, dec_deg))
    density = _STAR_DENSITY_BY_GAL_B[-1][1]
    for limit, d in _STAR_DENSITY_BY_GAL_B:
        if b <= limit:
            density = d
            break
    fov = _f(node, "fov_deg", 1.27)
    area = fov * fov * 0.7          # usable sensor fraction (edges vignetted)
    return max(0.0, density * area * COMP_USABLE_FRACTION)


# ── Total predicted error ─────────────────────────────────────────────────────

def sigma_instrumental(node: dict, mag: float, alt_deg: float, t_sub: float,
                       n_sub: int, mpsas_eff: float,
                       k_ext: float = DEFAULT_K_EXT) -> float:
    """Predicted instrumental error (mag) of the stacked measurement of one
    star of `mag`, before the ensemble floor."""
    airmass = airmass_from_alt(alt_deg)
    n_e = max(signal_electrons(node, mag, t_sub, airmass, k_ext), 1e-6)
    npix = psf_npix(node)
    sky = npix * sky_electrons_per_px(node, mpsas_eff, t_sub)
    dark_rate = (DARK_E_PER_PX_S_COOLED if node.get("cooled_camera")
                 else DARK_E_PER_PX_S_UNCOOLED)
    dark = npix * dark_rate * t_sub
    read = npix * DEFAULT_READ_NOISE_E ** 2
    var_e = n_e + sky + dark + read
    snr_sub = n_e / math.sqrt(var_e)
    sigma_sub = 1.0857 / max(snr_sub, 1e-6)
    scint = scintillation_mag(node, airmass, t_sub)
    per_sub = math.sqrt(sigma_sub ** 2 + scint ** 2)
    return per_sub / math.sqrt(max(1, n_sub))


def sigma_total(node: dict, mag: Optional[float], alt_deg: float, t_sub: float,
                n_sub: int, *, mpsas: Optional[float] = None,
                moon_illum: float = 0.0, moon_sep_deg: float = 180.0,
                ra_deg: float = 0.0, dec_deg: float = 0.0,
                kappa: float = 1.0, k_ext: float = DEFAULT_K_EXT) -> float:
    """Full predicted photometric error (mag) for a stacked observation:
    instrumental + ensemble floor + systematic floor, inflated by the node's
    measured efficiency kappa (variance multiplier, 1.0 = performs to physics).
    """
    m = 13.0 if mag is None else float(mag)
    sky = sky_mpsas_effective(
        mpsas if mpsas is not None else _f(node, "light_pollution_mpsas", 20.0),
        moon_illum, moon_sep_deg)
    inst = sigma_instrumental(node, m, alt_deg, t_sub, n_sub, sky, k_ext)
    comp_inst = sigma_instrumental(node, COMP_TYPICAL_MAG, alt_deg, t_sub,
                                   n_sub, sky, k_ext)
    n_comp = comp_star_count(node, ra_deg, dec_deg)
    ens = comp_inst / math.sqrt(max(1.0, n_comp))
    var = inst ** 2 + ens ** 2 + SYS_FLOOR_MAG ** 2
    return math.sqrt(max(kappa, 0.01) * var)


def capture(sigma: float, sigma_ref: float) -> float:
    """g(σ, u): fraction of a cell's information one successful measurement of
    error σ extracts — the exact Gaussian posterior variance reduction for a
    scalar cell state with prior sd σ_ref (CHORUS.md §2.2)."""
    s2 = max(sigma, 1e-6) ** 2
    r2 = max(sigma_ref, 1e-6) ** 2
    return r2 / (r2 + s2)


# ── Exposure optimization (replaces the mag-bucket choose_exposure) ───────────

@dataclass(frozen=True)
class ExposurePlan:
    t_sub: float          # seconds per sub
    n_sub: int
    dwell_min: float      # minutes on target (excl. slew reserve)
    sigma: float          # predicted error of the stack (mag)


_SUB_CANDIDATES_S = (5.0, 10.0, 15.0, 20.0, 30.0, 60.0, 120.0)
_DWELL_CANDIDATES_MIN = (5.0, 8.0, 12.0, 20.0, 35.0)


def exposure_candidates(node: dict) -> list:
    """Deterministic (t_sub, dwell) grid capped by the node's per-sub limit
    (alt-az field rotation for smartscopes, long for equatorial mounts)."""
    max_exp = _f(node, "max_exposure_s", 30.0)
    subs = sorted({min(t, max_exp) for t in _SUB_CANDIDATES_S if t <= max_exp} | {max_exp})
    out = []
    for t_sub in subs:
        for dwell in _DWELL_CANDIDATES_MIN:
            n = max(3, int(round(dwell * 60.0 / t_sub)))
            out.append((t_sub, n, dwell))
    return out


def best_exposure(node: dict, mag: Optional[float], alt_deg: float,
                  sigma_ref: float, *, mpsas: Optional[float] = None,
                  moon_illum: float = 0.0, moon_sep_deg: float = 180.0,
                  ra_deg: float = 0.0, dec_deg: float = 0.0,
                  kappa: float = 1.0) -> ExposurePlan:
    """The exposure plan maximizing information per occupied minute:
    argmax g(σ_plan, σ_ref) / (dwell + slew reserve).  Deterministic scan of
    the small candidate grid (CHORUS.md §3.1)."""
    best: Optional[ExposurePlan] = None
    best_rate = -1.0
    for t_sub, n_sub, dwell in exposure_candidates(node):
        sigma = sigma_total(node, mag, alt_deg, t_sub, n_sub, mpsas=mpsas,
                            moon_illum=moon_illum, moon_sep_deg=moon_sep_deg,
                            ra_deg=ra_deg, dec_deg=dec_deg, kappa=kappa)
        rate = capture(sigma, sigma_ref) / (dwell + SLEW_RESERVE_MIN)
        if rate > best_rate + 1e-12:
            best_rate = rate
            best = ExposurePlan(t_sub=t_sub, n_sub=n_sub,
                                dwell_min=dwell, sigma=round(sigma, 5))
    assert best is not None
    return best
