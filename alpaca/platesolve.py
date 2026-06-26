"""
Plate-solve & auto-center for the The Telescope Net node.

This is the closed-loop "goto refinement" the Seestar app does invisibly: after
slewing to a target you capture a frame, plate-solve it to learn where the mount
*actually* pointed, and nudge it until the target lands within a tolerance of the
frame centre.  Without it, alt-az mount pointing error (often several arcmin)
leaves the science target off-centre or out of frame.

Two layers, mirroring autofocus.py:

* ``center_on_target`` — dependency-injected core (``slew_fn``, ``capture_fn``,
  ``solve_fn``) so it can be unit-tested with a synthetic mount and no ASTAP.
* ``solve_image_array`` / ``center_on_target_device`` — wire it to a live
  ALPACA Telescope + Camera, reusing ``photometry._run_astap`` for the solve.

All sky coordinates are handled in **degrees** internally; the telescope slew
callback receives RA in **hours** (the ALPACA convention) and Dec in degrees.
"""

import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class CenteringError(Exception):
    """Raised when auto-centering cannot solve any frame."""


class CenteringCancelled(Exception):
    """Raised when auto-centering is aborted via its cancel_check callback."""


@dataclass
class CenterIteration:
    iteration: int
    commanded_ra: float   # deg, what we told the mount to slew to
    commanded_dec: float  # deg
    solved_ra: Optional[float]   # deg, where the frame actually pointed (None=solve failed)
    solved_dec: Optional[float]  # deg
    error_arcmin: Optional[float]  # angular separation from target


@dataclass
class CenterResult:
    success: bool
    target_ra: float        # deg
    target_dec: float       # deg
    final_ra: Optional[float]   # deg, last solved centre
    final_dec: Optional[float]  # deg
    error_arcmin: Optional[float]
    iterations: list = field(default_factory=list)  # list[CenterIteration]

    def as_dict(self) -> dict:
        def _r(v, n=5):
            return round(v, n) if v is not None else None
        return {
            "success": self.success,
            "target_ra": _r(self.target_ra),
            "target_dec": _r(self.target_dec),
            "final_ra": _r(self.final_ra),
            "final_dec": _r(self.final_dec),
            "error_arcmin": _r(self.error_arcmin, 3),
            "iterations": [
                {
                    "iteration": it.iteration,
                    "commanded_ra": _r(it.commanded_ra),
                    "commanded_dec": _r(it.commanded_dec),
                    "solved_ra": _r(it.solved_ra),
                    "solved_dec": _r(it.solved_dec),
                    "error_arcmin": _r(it.error_arcmin, 3),
                }
                for it in self.iterations
            ],
        }


def angular_separation_arcmin(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation between two RA/Dec points (all degrees) in arcminutes."""
    r1, d1 = math.radians(ra1), math.radians(dec1)
    r2, d2 = math.radians(ra2), math.radians(dec2)
    # Vincenty formula — stable for small separations (haversine loses precision
    # near zero from the 1 - cos cancellation; auto-centering lives near zero).
    dra = r2 - r1
    sin_d = math.sin(dra)
    cos_d = math.cos(dra)
    num = math.sqrt(
        (math.cos(d2) * sin_d) ** 2
        + (math.cos(d1) * math.sin(d2) - math.sin(d1) * math.cos(d2) * cos_d) ** 2
    )
    den = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * cos_d
    return math.degrees(math.atan2(num, den)) * 60.0


def center_on_target(
    slew_fn: Callable[[float, float], None],
    capture_fn: Callable[[], object],
    solve_fn: Callable[[object, float, float], Optional[tuple]],
    target_ra: float,
    target_dec: float,
    *,
    tolerance_arcmin: float = 3.0,
    max_iterations: int = 4,
    settle_s: float = 2.0,
    max_correction_deg: float = 5.0,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[CenterIteration], None]] = None,
) -> CenterResult:
    """
    Iteratively slew → solve → correct until the target is within tolerance.

    slew_fn(ra_hours, dec_deg) – blocking slew to commanded coordinates.
    capture_fn()               – returns an image (passed straight to solve_fn).
    solve_fn(image, ra_deg, dec_deg) – plate-solve the image with an RA/Dec hint;
                                 returns the frame-centre (ra_deg, dec_deg) the
                                 mount actually pointed at, or None on failure.
    target_ra / target_dec     – desired frame centre, in **degrees**.
    tolerance_arcmin           – stop once the centre is within this of target.
    max_iterations             – give up after this many solve attempts.
    max_correction_deg         – clamp per-step correction (guards against a wild
                                 mis-solve flinging the mount away).
    cancel_check()             – polled each iteration; True raises CenteringCancelled.
    progress_cb(iteration)     – called after each iteration with a CenterIteration.

    Returns a CenterResult (success flag + per-iteration trace). Raises
    CenteringError only if *no* iteration produced a solve.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    # Commanded position starts at the target; each correction assumes the mount's
    # pointing error is roughly constant in commanded-space, so
    #   commanded_next = commanded + (target - solved).
    commanded_ra = target_ra
    commanded_dec = target_dec
    iterations: list[CenterIteration] = []
    any_solved = False
    last_solved: Optional[tuple] = None
    last_error: Optional[float] = None

    def _check_cancel():
        if cancel_check is not None and cancel_check():
            raise CenteringCancelled("Auto-centering cancelled")

    for i in range(1, max_iterations + 1):
        _check_cancel()
        logger.info("Centering iteration %d/%d — slewing to RA=%.5f° Dec=%.5f°",
                    i, max_iterations, commanded_ra, commanded_dec)
        slew_fn(commanded_ra / 15.0, commanded_dec)  # RA deg → hours for ALPACA
        if settle_s > 0:
            time.sleep(settle_s)

        _check_cancel()
        image = capture_fn()
        solved = solve_fn(image, commanded_ra, commanded_dec)

        if solved is None:
            logger.warning("Centering iteration %d: plate solve failed", i)
            it = CenterIteration(i, commanded_ra, commanded_dec, None, None, None)
            iterations.append(it)
            if progress_cb:
                _safe_progress(progress_cb, it)
            continue

        any_solved = True
        solved_ra, solved_dec = float(solved[0]), float(solved[1])
        last_solved = (solved_ra, solved_dec)
        error = angular_separation_arcmin(target_ra, target_dec, solved_ra, solved_dec)
        last_error = error
        it = CenterIteration(i, commanded_ra, commanded_dec, solved_ra, solved_dec, error)
        iterations.append(it)
        if progress_cb:
            _safe_progress(progress_cb, it)

        logger.info("Centering iteration %d: solved RA=%.5f° Dec=%.5f° — error %.2f′",
                    i, solved_ra, solved_dec, error)

        if error <= tolerance_arcmin:
            logger.info("Target centered within %.2f′ (tolerance %.2f′) after %d iteration(s)",
                        error, tolerance_arcmin, i)
            return CenterResult(True, target_ra, target_dec, solved_ra, solved_dec,
                                error, iterations)

        # Apply correction in commanded-space: command_next = command + (target - solved).
        # RA and Dec are both in degrees here; the mount takes RA in degrees too,
        # so no cos(dec) scaling is needed — that would amplify corrections at high
        # declination and cause divergence.
        d_dec = target_dec - solved_dec
        d_ra = target_ra - solved_ra
        # Wrap RA delta into [-180, 180] so a target near 0h/24h corrects the short way.
        d_ra = (d_ra + 180.0) % 360.0 - 180.0
        d_ra = _clamp(d_ra, -max_correction_deg, max_correction_deg)
        d_dec = _clamp(d_dec, -max_correction_deg, max_correction_deg)
        commanded_ra = (commanded_ra + d_ra) % 360.0
        commanded_dec = _clamp(commanded_dec + d_dec, -90.0, 90.0)

    if not any_solved:
        raise CenteringError(
            "Auto-centering failed: no frame could be plate-solved. Check ASTAP is "
            "installed, the star database covers this field, and the exposure shows stars."
        )

    final_ra, final_dec = (last_solved if last_solved else (None, None))
    logger.warning("Auto-centering did not reach tolerance in %d iterations "
                   "(last error %.2f′)", max_iterations, last_error or float("nan"))
    return CenterResult(False, target_ra, target_dec, final_ra, final_dec,
                        last_error, iterations)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _safe_progress(cb, it):
    try:
        cb(it)
    except Exception:
        logger.debug("progress_cb raised", exc_info=True)


# ── ASTAP-backed solve of an in-memory image array ────────────────────────────

def solve_image_array(
    image_array,
    ra_deg: float,
    dec_deg: float,
    solver: str = "astrometry",
    solver_path: str = "solve-field",
    search_radius: float = 10.0,
    pixel_scale: Optional[float] = None,
) -> Optional[tuple]:
    """
    Plate-solve an ALPACA image array and return its frame-centre (ra_deg, dec_deg).

    solver        – "astrometry" (Astrometry.net) or "astap"
    solver_path   – path to solve-field or astap binary
    search_radius – search radius in degrees
    pixel_scale   – arcsec/px hint (speeds up astrometry.net solve)
    """
    import numpy as np
    from astropy.io import fits
    from astropy.wcs import WCS
    from src.photometry import _run_astrometry_net, _run_astap

    data = np.asarray(image_array, dtype=np.float32)
    if data.ndim == 3:
        data = data[0]
    if data.ndim != 2:
        logger.error("solve_image_array: expected 2-D image, got shape %s", data.shape)
        return None
    data = data.T  # ALPACA column-major → row-major

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".fits", prefix="bs_solve_", dir="/tmp")
        os.close(fd)
        fits.PrimaryHDU(data=data).writeto(tmp_path, overwrite=True)

        if solver == "astrometry":
            ok = _run_astrometry_net(tmp_path, ra_deg, dec_deg,
                                     solver_path, search_radius, pixel_scale)
        else:
            ok = _run_astap(tmp_path, ra_deg, dec_deg, solver_path, search_radius)

        if not ok:
            return None

        with fits.open(tmp_path, memmap=False, ignore_missing_simple=True) as hdul:
            hdr = hdul[0].header
            ny, nx = data.shape
            wcs = WCS(hdr, naxis=2)
            sky = wcs.pixel_to_world((nx + 1) / 2.0 - 1.0, (ny + 1) / 2.0 - 1.0)
            return float(sky.ra.deg), float(sky.dec.deg)
    except Exception as exc:
        logger.error("solve_image_array failed: %s", exc)
        return None
    finally:
        for ext in ("", ".ini", ".wcs"):
            p = (tmp_path or "") + ext if ext else tmp_path
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def center_on_target_device(
    telescope,
    camera,
    target_ra: float,
    target_dec: float,
    *,
    exposure_s: float = 3.0,
    tolerance_arcmin: float = 3.0,
    max_iterations: int = 4,
    settle_s: float = 2.0,
    solver: str = "astrometry",
    solver_path: str = "solve-field",
    search_radius: float = 10.0,
    pixel_scale: Optional[float] = None,
    readout_timeout: float = 60.0,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[CenterIteration], None]] = None,
) -> CenterResult:
    """
    Run the auto-centering loop against live ALPACA Telescope + Camera wrappers.

    target_ra / target_dec are in **degrees**.
    solver / solver_path – passed through to solve_image_array.
    """
    def slew_fn(ra_hours: float, dec_deg: float) -> None:
        telescope.slew_to_coordinates(ra_hours, dec_deg)

    def capture_fn():
        camera.expose(exposure_s, readout_timeout=readout_timeout, cancel_check=cancel_check)
        return camera.image_array()

    def solve_fn(image, ra_deg, dec_deg):
        return solve_image_array(image, ra_deg, dec_deg,
                                 solver=solver, solver_path=solver_path,
                                 search_radius=search_radius, pixel_scale=pixel_scale)

    return center_on_target(
        slew_fn, capture_fn, solve_fn, target_ra, target_dec,
        tolerance_arcmin=tolerance_arcmin,
        max_iterations=max_iterations,
        settle_s=settle_s,
        cancel_check=cancel_check,
        progress_cb=progress_cb,
    )
