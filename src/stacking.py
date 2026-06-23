#!/usr/bin/env python3
"""
Live stacking for the The Telescope Net node.

This is the Seestar app's signature feature: as each sub-frame arrives it is
star-aligned to a reference and added to a running accumulator, so the displayed
image grows cleaner (SNR ∝ √N) in real time.

``LiveStacker`` is self-contained and hardware-free — feed it raw image arrays
(nested lists from ALPACA, or ndarrays). Alignment is translation-only via a
RANSAC-style asterism vote over detected stars, which handles the frame-to-frame
drift of an unguided alt-az mount. Field *rotation* (also present on alt-az
mounts over long runs) is not corrected; keep individual stacking runs short, or
re-seed the reference periodically, until rotation handling is added.

Typical use::

    st = LiveStacker()
    for frame in frames:
        info = st.add_frame(frame)         # {accepted, frames_stacked, offset, ...}
    avg = st.stacked_image()               # float32 average, ready to save/stretch
    png = st.preview_png_b64()             # stretched 8-bit preview for the UI
"""

import base64
import io
import logging
import math
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _to_2d(image_array) -> np.ndarray:
    """Coerce an ALPACA image (nested list / ndarray, mono or colour) to 2-D float32."""
    data = np.asarray(image_array, dtype=np.float32)
    if data.ndim == 3:
        # ALPACA colour responses come as (plane, x, y) or (x, y, plane); collapse
        # to luminance for alignment + stacking by averaging the smallest axis.
        caxis = int(np.argmin(data.shape))
        data = data.mean(axis=caxis)
    if data.ndim != 2:
        raise ValueError(f"Expected a 2-D image, got shape {data.shape}")
    return data


def _detect_stars(data: np.ndarray, fwhm: float, threshold_sigma: float,
                  max_stars: int) -> np.ndarray:
    """Return an (N, 3) array of (x, y, flux) for the brightest detected stars."""
    from photutils.detection import DAOStarFinder
    from astropy.stats import sigma_clipped_stats

    _, median, std = sigma_clipped_stats(data, sigma=3.0)
    if std <= 0:
        return np.empty((0, 3), dtype=np.float32)
    finder = DAOStarFinder(fwhm=fwhm, threshold=threshold_sigma * std, exclude_border=True)
    sources = finder(data - median)
    if sources is None or len(sources) == 0:
        return np.empty((0, 3), dtype=np.float32)

    x_col = "x_centroid" if "x_centroid" in sources.colnames else "xcentroid"
    y_col = "y_centroid" if "y_centroid" in sources.colnames else "ycentroid"
    flux = np.asarray(sources["flux"], dtype=np.float32)
    xs = np.asarray(sources[x_col], dtype=np.float32)
    ys = np.asarray(sources[y_col], dtype=np.float32)
    order = np.argsort(flux)[::-1][:max_stars]
    return np.column_stack([xs[order], ys[order], flux[order]]).astype(np.float32)


def estimate_translation(ref: np.ndarray, cur: np.ndarray,
                         tolerance_px: float = 2.0,
                         max_candidates: int = 25) -> Optional[tuple]:
    """
    Estimate the (dx, dy) translation that maps ``cur`` star positions onto ``ref``.

    RANSAC-style vote: each (ref_i − cur_j) for the brightest stars is a candidate
    translation; the true one is the candidate with the most agreeing star pairs.
    Returns (dx, dy, n_inliers) or None if no consistent translation is found.
    """
    if len(ref) < 2 or len(cur) < 2:
        return None
    r = ref[:max_candidates, :2]
    c = cur[:max_candidates, :2]

    best = None  # (n_inliers, dx, dy)
    tol2 = tolerance_px * tolerance_px
    for ri in range(len(r)):
        for ci in range(len(c)):
            dx = float(r[ri, 0] - c[ci, 0])
            dy = float(r[ri, 1] - c[ci, 1])
            # Shift all current stars by this candidate and count matches to ref.
            shifted = c + (dx, dy)
            n = 0
            for s in shifted:
                d2 = ((r - s) ** 2).sum(axis=1)
                if d2.min() <= tol2:
                    n += 1
            if best is None or n > best[0]:
                best = (n, dx, dy)

    if best is None or best[0] < 2:
        return None

    # Refine: match inliers and take the median residual for sub-pixel accuracy.
    n_in, dx, dy = best
    shifted = c + (dx, dy)
    res = []
    for j, s in enumerate(shifted):
        d2 = ((r - s) ** 2).sum(axis=1)
        k = int(d2.argmin())
        if d2[k] <= tol2:
            res.append((r[k, 0] - c[j, 0], r[k, 1] - c[j, 1]))
    if res:
        res = np.asarray(res)
        dx = float(np.median(res[:, 0]))
        dy = float(np.median(res[:, 1]))
    return dx, dy, n_in


class LiveStacker:
    def __init__(
        self,
        detection_fwhm: float = 4.0,
        detection_threshold: float = 5.0,
        max_stars: int = 60,
        match_tolerance_px: float = 2.0,
        min_inliers: int = 4,
        max_offset_px: float = 300.0,
    ):
        self.detection_fwhm = detection_fwhm
        self.detection_threshold = detection_threshold
        self.max_stars = max_stars
        self.match_tolerance_px = match_tolerance_px
        self.min_inliers = min_inliers
        self.max_offset_px = max_offset_px

        self._ref_stars: Optional[np.ndarray] = None
        self._accum: Optional[np.ndarray] = None   # float64 running sum (aligned)
        self.frames_stacked = 0
        self.frames_total = 0
        self.frames_rejected = 0
        self.last_offset = (0.0, 0.0)
        self.last_inliers = 0

    def reset(self) -> None:
        self._ref_stars = None
        self._accum = None
        self.frames_stacked = 0
        self.frames_total = 0
        self.frames_rejected = 0
        self.last_offset = (0.0, 0.0)
        self.last_inliers = 0

    def add_frame(self, image_array) -> dict:
        """
        Align ``image_array`` to the reference and add it to the stack.

        Returns a status dict: {accepted, reason, frames_stacked, frames_total,
        offset, inliers, snr_gain}.
        """
        self.frames_total += 1
        try:
            data = _to_2d(image_array)
        except ValueError as exc:
            self.frames_rejected += 1
            return self._status(False, f"bad frame: {exc}")

        stars = _detect_stars(data, self.detection_fwhm,
                              self.detection_threshold, self.max_stars)

        # First usable frame becomes the reference.
        if self._accum is None:
            if len(stars) < self.min_inliers:
                self.frames_rejected += 1
                return self._status(False, "reference frame has too few stars")
            self._ref_stars = stars
            self._accum = data.astype(np.float64)
            self.frames_stacked = 1
            self.last_offset = (0.0, 0.0)
            self.last_inliers = len(stars)
            return self._status(True, "reference frame")

        if data.shape != self._accum.shape:
            self.frames_rejected += 1
            return self._status(False, "frame size differs from reference")

        est = estimate_translation(self._ref_stars, stars,
                                   tolerance_px=self.match_tolerance_px)
        if est is None or est[2] < self.min_inliers:
            self.frames_rejected += 1
            return self._status(False, "alignment failed (too few matching stars)")

        dx, dy, inliers = est
        if not (math.isfinite(dx) and math.isfinite(dy)) or \
                abs(dx) > self.max_offset_px or abs(dy) > self.max_offset_px:
            self.frames_rejected += 1
            return self._status(False, f"offset out of range ({dx:.1f},{dy:.1f})")

        # Shift current frame by (dx, dy) to register it onto the reference, then
        # accumulate. scipy shift order=1 = bilinear (sub-pixel, fast).
        from scipy.ndimage import shift as ndi_shift
        # ndimage uses (row, col) = (y, x) ordering.
        aligned = ndi_shift(data, shift=(dy, dx), order=1, mode="constant", cval=0.0)
        self._accum += aligned
        self.frames_stacked += 1
        self.last_offset = (round(dx, 2), round(dy, 2))
        self.last_inliers = inliers
        return self._status(True, "stacked")

    def stacked_image(self) -> Optional[np.ndarray]:
        """Return the current average stack as float32, or None if empty."""
        if self._accum is None or self.frames_stacked == 0:
            return None
        return (self._accum / self.frames_stacked).astype(np.float32)

    def snr_gain(self) -> float:
        """Approximate SNR improvement over a single frame (√N for shot-noise-limited)."""
        return math.sqrt(self.frames_stacked) if self.frames_stacked else 0.0

    def preview_png_b64(self, max_px: int = 720) -> Optional[str]:
        """Return a percentile-stretched 8-bit PNG preview of the stack, base64-encoded."""
        img = self.stacked_image()
        if img is None:
            return None
        from PIL import Image

        lo, hi = np.percentile(img, (1.0, 99.5))
        if hi <= lo:
            hi = lo + 1.0
        stretched = np.clip((img - lo) / (hi - lo), 0.0, 1.0)
        # Mild asinh stretch to lift faint structure, as Seestar's live view does.
        stretched = np.arcsinh(stretched * 10.0) / math.asinh(10.0)
        u8 = (stretched * 255.0).astype(np.uint8)

        im = Image.fromarray(u8, mode="L")
        if max(im.size) > max_px:
            scale = max_px / max(im.size)
            im = im.resize((max(1, int(im.size[0] * scale)),
                            max(1, int(im.size[1] * scale))))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _status(self, accepted: bool, reason: str) -> dict:
        return {
            "accepted": accepted,
            "reason": reason,
            "frames_stacked": self.frames_stacked,
            "frames_total": self.frames_total,
            "frames_rejected": self.frames_rejected,
            "offset": self.last_offset,
            "inliers": self.last_inliers,
            "snr_gain": round(self.snr_gain(), 2),
        }
