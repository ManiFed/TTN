#!/usr/bin/env python3
"""
Ingestion triage — a cheap gate before the expensive solver/extraction.

Universal frame ingestion accepts frames from anyone, which means junk too:
screenshots, terrestrial photos, heavily stretched/processed images whose pixel
values are no longer linear (so photometry is meaningless), and star-trailed or
gradient-ruined frames. Solving and extracting those wastes the solver worker.

Heuristics first, deliberately — the user's rule is that AI goes only where an
algorithm genuinely can't. Everything here is a statistic on the pixels or a
header keyword:

  * not-sky   — too few point sources for a real star field
  * stretched — non-linear transfer (histogram shape + processing-software
                headers); photometry on stretched data is invalid → reject
  * trailed   — high median source elongation (tracking failure)
  * gradient  — strong background ramp (light pollution / vignette)

`classify(path, config)` returns
    {"ok": bool, "label": str, "score": float, "gates": {...}, "reason": str}
`ok=False` means "don't spend solver time on this". The `classify` signature is
the seam a small vision model can slot behind later without touching callers.
"""

import logging

logger = logging.getLogger("cloud.triage")

# Processing software that implies a stretched / non-linear image. Matched
# case-insensitively against CREATOR/PROGRAM/SWCREATE/ORIGIN and HISTORY.
_PROCESSING_MARKERS = (
    "pixinsight", "photoshop", "gimp", "siril", "lightroom", "affinity",
    "startools", "astro pixel processor", "aps", "denoise", "graxpert",
)


def _thresholds(config: dict) -> dict:
    t = config.get("triage", {}) or {}
    return {
        "min_sources":        int(t.get("min_sources", 8)),
        "max_saturated_frac": float(t.get("max_saturated_frac", 0.02)),
        "max_elongation":     float(t.get("max_elongation", 1.8)),
        "max_gradient_ratio": float(t.get("max_gradient_ratio", 6.0)),
        "enabled":            bool(t.get("enabled", True)),
    }


def classify(path: str, config: dict) -> dict:
    """Cheap accept/reject verdict for a contributed frame."""
    th = _thresholds(config)
    if not th["enabled"]:
        return {"ok": True, "label": "skipped", "score": 1.0, "gates": {}}

    import numpy as np
    from astropy.io import fits

    try:
        with fits.open(path, memmap=False, ignore_missing_simple=True) as hdul:
            hdu = next((h for h in hdul if getattr(h, "data", None) is not None), None)
            if hdu is None:
                return _reject("no_image", "file has no image data")
            data = np.asarray(hdu.data, dtype="float64")
            header = hdu.header
    except Exception as exc:
        return _reject("unreadable", f"could not read FITS: {exc}")

    # Collapse colour/cube frames to 2-D by taking the first plane.
    while data.ndim > 2:
        data = data[0]
    if data.ndim != 2 or min(data.shape) < 16:
        return _reject("not_image", "not a 2-D image of usable size")

    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return _reject("empty", "no finite pixels")

    gates: dict = {}

    # ── stretched / non-linear ────────────────────────────────────────────────
    stretched, why = _looks_stretched(finite, header, th)
    gates["stretched"] = stretched
    if stretched:
        return _reject("stretched", f"non-linear/processed image: {why}",
                       gates=gates)

    # ── source detection (drives not-sky + trailing) ──────────────────────────
    n_sources, med_elong = _source_stats(data)
    gates["n_sources"] = n_sources
    gates["median_elongation"] = round(med_elong, 3)
    if n_sources < th["min_sources"]:
        return _reject("not_sky",
                       f"only {n_sources} point sources (<{th['min_sources']})",
                       gates=gates)
    if med_elong > th["max_elongation"]:
        return _reject("trailed",
                       f"median elongation {med_elong:.2f} > {th['max_elongation']}",
                       gates=gates)

    # ── gradient (warning only: extraction subtracts background) ───────────────
    ratio = _gradient_ratio(data)
    gates["gradient_ratio"] = round(ratio, 2)
    warn = ratio > th["max_gradient_ratio"]

    return {"ok": True, "label": "sky_linear",
            "score": round(min(1.0, n_sources / 100.0), 3),
            "gates": gates,
            "reason": "strong background gradient" if warn else ""}


def _reject(label: str, reason: str, gates: dict | None = None) -> dict:
    return {"ok": False, "label": label, "score": 0.0,
            "gates": gates or {}, "reason": reason}


def _looks_stretched(finite, header, th) -> tuple[bool, str]:
    """A linear astro frame is heavily bottom-weighted (sky background) with a
    long faint tail of stars. Stretched images compress that: the mode moves up
    and a large fraction of pixels pile near the maximum."""
    import numpy as np

    # Processing-software headers are a strong, cheap signal.
    hay = " ".join(str(header.get(k, "")) for k in
                   ("CREATOR", "PROGRAM", "SWCREATE", "ORIGIN", "SOFTWARE"))
    hay += " " + " ".join(str(v) for v in header.get("HISTORY", []))
    hay = hay.lower()
    for marker in _PROCESSING_MARKERS:
        if marker in hay:
            return True, f"header names {marker}"

    vmax = float(np.nanmax(finite))
    vmin = float(np.nanmin(finite))
    if vmax <= vmin:
        return True, "flat image"
    # Fraction of pixels within 1% of the top — near-white pile-up from an
    # aggressive stretch. A raw linear frame keeps this tiny.
    hi = vmin + 0.99 * (vmax - vmin)
    sat_frac = float(np.mean(finite >= hi))
    if sat_frac > max(th["max_saturated_frac"], 0.05):
        return True, f"{sat_frac:.0%} of pixels near white"
    # 8-bit dynamic range is a giveaway that a linear sensor frame was exported
    # through a display-referred pipeline.
    if vmax <= 255.0 and float(header.get("BITPIX", 16)) in (8, -8):
        median = float(np.median(finite))
        if median > 0.15 * vmax:      # sky background sitting mid-range, not low
            return True, "8-bit with elevated background"
    return False, ""


def _source_stats(data) -> tuple[int, float]:
    """(#detected sources, median elongation) via image segmentation.

    SourceCatalog.elongation is the true semimajor/semiminor axis ratio, so a
    star-trailed frame reads as high elongation directly (DAOStarFinder's
    roundness both filters trails out and measures them poorly)."""
    import numpy as np
    from astropy.stats import sigma_clipped_stats
    try:
        from photutils.segmentation import SourceCatalog, detect_sources
        _, median, std = sigma_clipped_stats(data, sigma=3.0)
        if std <= 0:
            return 0, 1.0
        try:
            segm = detect_sources(data - median, threshold=5.0 * std, n_pixels=5)
        except TypeError:      # photutils < 2.0 spells it 'npixels'
            segm = detect_sources(data - median, threshold=5.0 * std, npixels=5)
        n_labels = 0 if segm is None else int(len(segm.labels))
        if n_labels == 0:
            return 0, 1.0
        cat = SourceCatalog(data - median, segm)
        elong = np.asarray(cat.elongation, dtype="float64")
        elong = elong[np.isfinite(elong)]
        med = float(np.median(elong)) if elong.size else 1.0
        return n_labels, med
    except Exception as exc:
        logger.debug("triage source pass fell back: %s", exc)
        _, median, std = sigma_clipped_stats(data, sigma=3.0)
        if std <= 0:
            return 0, 1.0
        peaks = int(np.count_nonzero(data - median > 5.0 * std))
        return peaks // 9, 1.0


def _gradient_ratio(data) -> float:
    """max/min of 3×3 block background medians — a smooth ramp shows a high
    ratio. Robust to stars (uses medians)."""
    import numpy as np
    h, w = data.shape
    bh, bw = h // 3, w // 3
    if bh < 4 or bw < 4:
        return 1.0
    meds = []
    for i in range(3):
        for j in range(3):
            block = data[i * bh:(i + 1) * bh, j * bw:(j + 1) * bw]
            block = block[np.isfinite(block)]
            if block.size:
                meds.append(float(np.median(block)))
    if len(meds) < 2:
        return 1.0
    lo = min(meds)
    hi = max(meds)
    # Shift so a near-zero-background frame doesn't divide by ~0.
    offset = abs(lo) + 1.0
    return (hi + offset) / (lo + offset)
