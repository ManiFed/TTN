"""
Autofocus engine for the The Telescope Net node.

Implements the V-curve sweep that the Seestar app performs automatically:
step the focuser across a range, measure stellar sharpness (FWHM, in pixels)
from a short exposure at each step, fit a parabola near the sharpest sample,
and drive the focuser to the interpolated best-focus position.

The core ``run_autofocus`` is dependency-injected (it takes ``move_fn`` and
``measure_fn`` callables) so it can be unit-tested with a synthetic V-curve and
no hardware.  ``autofocus_device`` wires it to a live ALPACA Focuser + Camera,
reusing the photometry pipeline's FWHM estimator.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AutofocusError(Exception):
    """Raised when autofocus cannot determine a best-focus position."""


class AutofocusCancelled(Exception):
    """Raised when autofocus is aborted via its cancel_check callback."""


@dataclass
class FocusSample:
    position: int
    fwhm: Optional[float]  # None when the frame could not be measured


@dataclass
class AutofocusResult:
    best_position: int
    best_fwhm: Optional[float]
    start_position: int
    samples: list = field(default_factory=list)  # list[FocusSample]
    interpolated: bool = False  # True if best_position came from a parabola fit

    def as_dict(self) -> dict:
        return {
            "best_position": self.best_position,
            "best_fwhm": round(self.best_fwhm, 2) if self.best_fwhm is not None else None,
            "start_position": self.start_position,
            "interpolated": self.interpolated,
            "samples": [
                {"position": s.position,
                 "fwhm": round(s.fwhm, 2) if s.fwhm is not None else None}
                for s in self.samples
            ],
        }


def _parabola_vertex(p1, m1, p2, m2, p3, m3) -> Optional[float]:
    """
    Vertex x-coordinate of the parabola through three (position, metric) points.

    Returns None if the points are collinear or form an upward-opening curve is
    not the case (i.e. the middle point is not the lowest) — the caller should
    then fall back to the discrete minimum.
    """
    denom = (p1 - p2) * (p1 - p3) * (p2 - p3)
    if denom == 0:
        return None
    a = (p3 * (m2 - m1) + p2 * (m1 - m3) + p1 * (m3 - m2)) / denom
    if a <= 0:  # not a minimum (opens downward or flat)
        return None
    b = (p3 * p3 * (m1 - m2) + p2 * p2 * (m3 - m1) + p1 * p1 * (m2 - m3)) / denom
    return -b / (2 * a)


def run_autofocus(
    move_fn: Callable[[int], None],
    measure_fn: Callable[[], Optional[float]],
    start_position: int,
    *,
    step_size: int = 50,
    steps_per_side: int = 5,
    settle_s: float = 1.0,
    min_position: Optional[int] = None,
    max_position: Optional[int] = None,
    samples_per_point: int = 1,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[FocusSample, int, int], None]] = None,
) -> AutofocusResult:
    """
    Sweep the focuser symmetrically around ``start_position`` and return the
    best-focus position.

    move_fn(position)   – blocking move of the focuser to an absolute step.
    measure_fn()        – returns a sharpness metric where *lower is sharper*
                          (e.g. FWHM in px), or None if the frame was unusable.
    step_size           – focuser steps between samples.
    steps_per_side      – samples taken on each side of the start position; the
                          sweep visits ``2*steps_per_side + 1`` positions.
    settle_s            – pause after each move before measuring (vibration).
    min/max_position    – hard travel limits; sample positions are clamped and
                          duplicates dropped.
    samples_per_point   – frames averaged (median) per position to fight noise.
    cancel_check()      – polled before each move; True aborts (raises
                          AutofocusCancelled).
    progress_cb(sample, index, total) – called after each measured position.

    Returns an AutofocusResult. Raises AutofocusError if no position could be
    measured, and AutofocusCancelled if aborted (focuser is returned to start).
    """
    if step_size <= 0:
        raise ValueError("step_size must be positive")
    if steps_per_side < 1:
        raise ValueError("steps_per_side must be >= 1")

    # Build the ordered list of unique, in-range sample positions. Sweeping
    # low→high keeps the mechanical motion monotonic, which minimises backlash
    # error across the curve.
    raw = [start_position + i * step_size
           for i in range(-steps_per_side, steps_per_side + 1)]
    positions: list[int] = []
    for p in raw:
        if min_position is not None:
            p = max(min_position, p)
        if max_position is not None:
            p = min(max_position, p)
        if p not in positions:
            positions.append(p)
    positions.sort()

    total = len(positions)
    if total < 3:
        raise AutofocusError(
            "Autofocus needs at least 3 distinct positions within the travel "
            "limits — widen the range or reduce step_size"
        )

    samples: list[FocusSample] = []

    def _check_cancel():
        if cancel_check is not None and cancel_check():
            logger.warning("Autofocus cancelled — returning focuser to %d", start_position)
            try:
                move_fn(start_position)
            except Exception as exc:  # best-effort restore
                logger.error("Failed to restore focuser position: %s", exc)
            raise AutofocusCancelled("Autofocus cancelled")

    logger.info(
        "Autofocus: sweeping %d positions [%d … %d] step %d around start %d",
        total, positions[0], positions[-1], step_size, start_position,
    )

    for idx, pos in enumerate(positions):
        _check_cancel()
        move_fn(pos)
        if settle_s > 0:
            time.sleep(settle_s)

        metric = _measure_median(measure_fn, samples_per_point)
        sample = FocusSample(position=pos, fwhm=metric)
        samples.append(sample)
        logger.info(
            "Autofocus: position %d → FWHM %s",
            pos, f"{metric:.2f} px" if metric is not None else "no stars",
        )
        if progress_cb is not None:
            try:
                progress_cb(sample, idx + 1, total)
            except Exception:
                logger.debug("progress_cb raised", exc_info=True)

    measured = [s for s in samples if s.fwhm is not None]
    if not measured:
        # Nothing usable — leave the focuser where it started.
        try:
            move_fn(start_position)
        except Exception:
            pass
        raise AutofocusError(
            "Autofocus failed: no stars detected at any focuser position. "
            "Check that the target field contains stars and the exposure is long enough."
        )

    # Discrete minimum among measured samples.
    best = min(measured, key=lambda s: s.fwhm)
    best_position = best.position
    best_fwhm = best.fwhm
    interpolated = False

    # Refine with a parabola through the minimum and its measured neighbours.
    order = [s for s in samples if s.fwhm is not None]
    bi = order.index(best)
    if 0 < bi < len(order) - 1:
        left, mid, right = order[bi - 1], order[bi], order[bi + 1]
        vertex = _parabola_vertex(
            left.position, left.fwhm, mid.position, mid.fwhm, right.position, right.fwhm
        )
        if vertex is not None and left.position <= vertex <= right.position:
            best_position = int(round(vertex))
            interpolated = True
            logger.info("Autofocus: parabola vertex at %d (between %d and %d)",
                        best_position, left.position, right.position)
    else:
        logger.warning(
            "Autofocus: best sample is at a sweep edge (%d) — true focus may lie "
            "beyond the swept range; consider re-running centred on this position",
            best_position,
        )

    _check_cancel()
    move_fn(best_position)
    logger.info("Autofocus complete — best focus at position %d (FWHM ~%.2f px%s)",
                best_position, best_fwhm, ", interpolated" if interpolated else "")

    return AutofocusResult(
        best_position=best_position,
        best_fwhm=best_fwhm,
        start_position=start_position,
        samples=samples,
        interpolated=interpolated,
    )


def _measure_median(measure_fn: Callable[[], Optional[float]], n: int) -> Optional[float]:
    if n <= 1:
        return measure_fn()
    vals = [v for v in (measure_fn() for _ in range(n)) if v is not None]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def autofocus_device(
    focuser,
    camera,
    *,
    exposure_s: float = 2.0,
    step_size: int = 50,
    steps_per_side: int = 5,
    settle_s: float = 1.0,
    samples_per_point: int = 1,
    min_position: Optional[int] = None,
    max_position: Optional[int] = None,
    readout_timeout: float = 60.0,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[FocusSample, int, int], None]] = None,
) -> AutofocusResult:
    """
    Run autofocus against live ALPACA Focuser + Camera wrappers.

    Reuses photometry._estimate_fwhm to measure star sharpness from each frame.
    The focuser's current position is used as the sweep centre.
    """
    import numpy as np
    from src.photometry import _estimate_fwhm

    start_position = focuser.position()

    def move_fn(position: int) -> None:
        focuser.move(position)  # blocking in the Focuser wrapper

    def measure_fn() -> Optional[float]:
        camera.expose(exposure_s, readout_timeout=readout_timeout,
                      cancel_check=cancel_check)
        data = np.asarray(camera.image_array(), dtype=float)
        # ALPACA delivers ImageArray column-major (x, y); transpose to (row, col)
        # so the FWHM stamps line up with the image the same way photometry sees it.
        if data.ndim == 2:
            data = data.T
        fwhm = _estimate_fwhm(data)
        # _estimate_fwhm returns its 4.0 px sentinel when it finds no stars; we
        # can't distinguish that from a genuine 4 px result, so treat the whole
        # sweep's relative shape as the signal and keep the value.
        return fwhm

    return run_autofocus(
        move_fn,
        measure_fn,
        start_position,
        step_size=step_size,
        steps_per_side=steps_per_side,
        settle_s=settle_s,
        min_position=min_position,
        max_position=max_position,
        samples_per_point=samples_per_point,
        cancel_check=cancel_check,
        progress_cb=progress_cb,
    )
