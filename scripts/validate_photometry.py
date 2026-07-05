#!/usr/bin/env python3
"""
Photometry validation engine — The Telescope Net.

Runs the full photometry pipeline over a corpus of FITS files with known
reference magnitudes and emits a validation report: recovered magnitude vs
reference, residual distributions, uncertainty calibration, zero-point
scatter, outlier diagnostics, catalog provenance, and pass/fail against
AAVSO-style quality gates.

Two modes
---------
1. Real data (beta-node corpus; see docs/validation/FIXTURE_MANIFEST.md):

       python3 scripts/validate_photometry.py \\
           --fits-dir /path/to/fits --manifest manifest.json --out out_dir

   The manifest pins the reference magnitude per target and (optionally) a
   frozen comparison-star catalog so the run is offline and reproducible.

2. Synthetic self-validation (no data required, fully offline, deterministic):

       python3 scripts/validate_photometry.py --synthetic --out out_dir

   Generates a corpus with known ground truth — clean frames across a
   magnitude range plus pathological frames (saturated target, moon-bright
   sky, sparse comparison field, gross WCS error, dark frame) — and verifies
   the pipeline recovers the truth or rejects/flags each frame correctly.

Outputs (in --out):
    results.json    every measurement, rejection, and gate verdict
    report.md       human-readable validation report with tables
    *.png           residual / z-score / zp-scatter figures (needs matplotlib)

Exit status: 0 when all gates pass, 1 otherwise — usable in CI.
"""

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

# Repo root on sys.path so `src.` imports work when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

warnings.filterwarnings("ignore")

# ── Acceptance gates (AAVSO-style, documented in VALIDATION_REPORT.md) ─────────
GATES = {
    "median_abs_residual_mag": 0.05,   # |median(measured − reference)|
    "rms_residual_mag":        0.10,   # RMS of residuals, good/acceptable frames
    "z_rms_max":               1.50,   # RMS of residual/reported_uncertainty
    "outlier_fraction_max":    0.05,   # frames with |resid| > max(0.3, 3σ)
    "pathology_miss_max":      0,      # pathological frames neither rejected nor flagged
}


# ── Corpus runners ─────────────────────────────────────────────────────────────

def run_corpus(frames: list, base_config: dict) -> dict:
    """
    frames: list of dicts {fits_path, target_name, reference_mag,
                           expect (optional: 'good'|'flagged'|'rejected'),
                           label (optional), config (optional per-frame)}
    Returns {"measurements": [...], "rejections": [...]}.
    """
    from src.photometry import run_pipeline_ex

    measurements, rejections = [], []
    for fr in frames:
        cfg = fr.get("config") or base_config
        result, rej = run_pipeline_ex(fr["fits_path"], cfg)
        rec = {
            "fits_file":     os.path.basename(fr["fits_path"]),
            "label":         fr.get("label", ""),
            "target_name":   fr.get("target_name", ""),
            "reference_mag": fr.get("reference_mag"),
            "expect":        fr.get("expect", "good"),
        }
        if result is None:
            rec["rejection"] = rej
            rejections.append(rec)
        else:
            rec["measurement"] = result
            if fr.get("reference_mag") is not None:
                rec["residual"] = round(result["magnitude"] - fr["reference_mag"], 4)
                if result["uncertainty"] > 0:
                    rec["z"] = round(rec["residual"] / result["uncertainty"], 3)
            measurements.append(rec)
    return {"measurements": measurements, "rejections": rejections}


# ── Statistics & gating ────────────────────────────────────────────────────────

def analyse(corpus: dict) -> dict:
    """Compute the summary statistics and gate verdicts for a corpus run."""
    meas = corpus["measurements"]
    rej  = corpus["rejections"]

    # Only frames expected to be measurable participate in accuracy stats
    clean = [m for m in meas if m["expect"] == "good"
             and m.get("residual") is not None]
    residuals = np.array([m["residual"] for m in clean]) if clean else np.array([])
    zs        = np.array([m["z"] for m in clean if "z" in m]) if clean else np.array([])
    zps       = np.array([m["measurement"]["zp_scatter"] for m in clean]) if clean else np.array([])

    outliers = [m for m in clean
                if abs(m["residual"]) > max(0.3, 3 * m["measurement"]["uncertainty"])]

    # Pathological frames: correct behaviour = rejected OR flagged not-good
    pathological = [x for x in meas + rej if x["expect"] in ("flagged", "rejected")]
    missed = [x for x in pathological
              if "measurement" in x and x["measurement"]["quality_flag"] == "good"]

    stats = {
        "n_frames":        len(meas) + len(rej),
        "n_measured":      len(meas),
        "n_rejected":      len(rej),
        "n_clean":         len(clean),
        "n_pathological":  len(pathological),
        "median_residual": float(np.median(residuals)) if len(residuals) else None,
        "mean_residual":   float(np.mean(residuals)) if len(residuals) else None,
        "rms_residual":    float(np.sqrt(np.mean(residuals ** 2))) if len(residuals) else None,
        "max_abs_residual": float(np.max(np.abs(residuals))) if len(residuals) else None,
        "z_rms":           float(np.sqrt(np.mean(zs ** 2))) if len(zs) else None,
        "z_max_abs":       float(np.max(np.abs(zs))) if len(zs) else None,
        "median_reported_unc": (float(np.median([m["measurement"]["uncertainty"] for m in clean]))
                                if clean else None),
        "median_zp_scatter": float(np.median(zps)) if len(zps) else None,
        "outliers":        [{"fits": m["fits_file"], "residual": m["residual"],
                             "unc": m["measurement"]["uncertainty"]} for m in outliers],
        "pathology_missed": [x["fits_file"] for x in missed],
        "quality_counts":  {},
        "rejection_reasons": {},
        "catalog_sources": {},
    }

    for m in meas:
        q = m["measurement"]["quality_flag"]
        stats["quality_counts"][q] = stats["quality_counts"].get(q, 0) + 1
        for c in m["measurement"].get("provenance", {}).get("comparison_stars", []):
            if c.get("used"):
                s = c.get("source", "unknown")
                stats["catalog_sources"][s] = stats["catalog_sources"].get(s, 0) + 1
    for r in rej:
        code = r["rejection"]["reason_code"]
        stats["rejection_reasons"][code] = stats["rejection_reasons"].get(code, 0) + 1

    # Gate verdicts
    verdicts = {}
    if stats["median_residual"] is not None:
        verdicts["median_abs_residual_mag"] = (
            abs(stats["median_residual"]) <= GATES["median_abs_residual_mag"])
        verdicts["rms_residual_mag"] = stats["rms_residual"] <= GATES["rms_residual_mag"]
    if stats["z_rms"] is not None:
        verdicts["z_rms_max"] = stats["z_rms"] <= GATES["z_rms_max"]
    if stats["n_clean"]:
        verdicts["outlier_fraction_max"] = (
            len(stats["outliers"]) / stats["n_clean"] <= GATES["outlier_fraction_max"])
    verdicts["pathology_miss_max"] = (
        len(stats["pathology_missed"]) <= GATES["pathology_miss_max"])
    stats["gate_verdicts"] = verdicts
    stats["all_gates_pass"] = all(verdicts.values()) if verdicts else False
    return stats


# ── Synthetic corpus ───────────────────────────────────────────────────────────

def build_synthetic_corpus(work_dir: str) -> tuple:
    """Generate the deterministic self-validation corpus. Returns (frames, cfg)."""
    from tests.validation.synthimg import make_scene

    frames = []

    # Clean frames: 3 target magnitudes × 8 noise realisations
    for mag in (10.5, 11.5, 12.5):
        for seed in range(8):
            s = make_scene(work_dir, name=f"clean_m{mag}_s{seed}",
                           seed=100 + seed + int(mag * 10), target_mag=mag)
            frames.append({
                "fits_path": s.fits_path, "target_name": s.target_name,
                "reference_mag": s.target_mag, "expect": "good",
                "label": f"clean mag={mag}", "config": s.config(),
            })

    # Pathological frames — must be rejected or flagged, never 'good'
    s = make_scene(work_dir, name="path_saturated", seed=900,
                   target_mag=8.0, clip_adu=3000)
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": s.target_mag, "expect": "flagged",
                   "label": "saturated target",
                   "config": s.config(saturation_adu=3000)})

    s = make_scene(work_dir, name="path_brightsky", seed=901, sky_adu=5000.0)
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": s.target_mag, "expect": "flagged",
                   "label": "moon-bright sky", "config": s.config()})

    s = make_scene(work_dir, name="path_sparse", seed=902, n_comp=1)
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": s.target_mag, "expect": "rejected",
                   "label": "sparse comparison field", "config": s.config()})

    s = make_scene(work_dir, name="path_wcs", seed=903, wcs_offset_px=(60, 45))
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": s.target_mag, "expect": "rejected",
                   "label": "gross WCS error", "config": s.config()})

    s = make_scene(work_dir, name="path_dark", seed=904, imagetyp="DARK")
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": None, "expect": "rejected",
                   "label": "dark frame", "config": s.config()})

    s = make_scene(work_dir, name="path_faint", seed=905, target_mag=14.3)
    frames.append({"fits_path": s.fits_path, "target_name": s.target_name,
                   "reference_mag": s.target_mag, "expect": "flagged",
                   "label": "faint target (low SNR)", "config": s.config()})

    return frames, None


# ── Real-data corpus ───────────────────────────────────────────────────────────

def build_real_corpus(fits_dir: str, manifest_path: str) -> tuple:
    """Frames from a directory of FITS + a reference manifest. Returns (frames, cfg)."""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    import yaml
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    base = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            base = yaml.safe_load(fh) or {}
    for section, over in (manifest.get("config_overrides") or {}).items():
        base.setdefault(section, {}).update(over)
    phot = base.setdefault("photometry", {})
    if manifest.get("comparison_star_file"):
        # Pin the comparison sequence for a reproducible, offline run
        phot["comparison_catalogs"] = ["file"]
        phot["comparison_star_file"] = str(
            Path(manifest_path).parent / manifest["comparison_star_file"])

    targets = manifest.get("targets", {})
    frames = []
    from astropy.io import fits as _fits
    for p in sorted(Path(fits_dir).glob("**/*.fit*")):
        try:
            obj = str(_fits.getheader(p).get("OBJECT", "")).strip()
        except Exception:
            obj = ""
        ref = targets.get(obj, {})
        frames.append({
            "fits_path": str(p),
            "target_name": obj,
            "reference_mag": ref.get("reference_mag"),
            "expect": ref.get("expect", "good"),
            "label": ref.get("reference_source", ""),
        })
    return frames, base


# ── Reporting ──────────────────────────────────────────────────────────────────

def _fmt(v, spec=".4f"):
    return "—" if v is None else format(v, spec)


def write_report(out_dir: Path, corpus: dict, stats: dict, mode: str) -> None:
    lines = [
        "# Photometry Validation Run",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Mode: {mode}",
        f"- Frames: {stats['n_frames']} "
        f"(measured {stats['n_measured']}, rejected {stats['n_rejected']})",
        "",
        "## Accuracy vs reference",
        "",
        "| Statistic | Value | Gate | Pass |",
        "|---|---|---|---|",
    ]
    v = stats["gate_verdicts"]

    def _row(label, value, gate_key, gate_desc):
        ok = v.get(gate_key)
        badge = "—" if ok is None else ("✅" if ok else "❌")
        lines.append(f"| {label} | {value} | {gate_desc} | {badge} |")

    _row("Median residual (mag)", _fmt(stats["median_residual"]),
         "median_abs_residual_mag", f"|x| ≤ {GATES['median_abs_residual_mag']}")
    _row("RMS residual (mag)", _fmt(stats["rms_residual"]),
         "rms_residual_mag", f"≤ {GATES['rms_residual_mag']}")
    _row("Max |residual| (mag)", _fmt(stats["max_abs_residual"]), "", "—")
    _row("Uncertainty calibration RMS(z)", _fmt(stats["z_rms"], ".3f"),
         "z_rms_max", f"≤ {GATES['z_rms_max']}")
    _row("Outliers (>max(0.3, 3σ))", str(len(stats["outliers"])),
         "outlier_fraction_max", f"≤ {GATES['outlier_fraction_max']:.0%} of clean frames")
    _row("Pathological frames passed as good", str(len(stats["pathology_missed"])),
         "pathology_miss_max", f"= {GATES['pathology_miss_max']}")

    lines += [
        "",
        f"- Median reported uncertainty: {_fmt(stats['median_reported_unc'])} mag",
        f"- Median zero-point scatter:  {_fmt(stats['median_zp_scatter'])} mag",
        "",
        "## Quality flags",
        "",
        "| Flag | Count |", "|---|---|",
    ]
    for q, n in sorted(stats["quality_counts"].items()):
        lines.append(f"| {q} | {n} |")

    lines += ["", "## Rejection reasons (machine-readable)", "",
              "| reason_code | Count |", "|---|---|"]
    for code, n in sorted(stats["rejection_reasons"].items()):
        lines.append(f"| {code} | {n} |")

    lines += ["", "## Comparison-star provenance (used stars)", "",
              "| Catalog source | Stars used (frame-star pairs) |", "|---|---|"]
    for srce, n in sorted(stats["catalog_sources"].items()):
        lines.append(f"| {srce} | {n} |")

    if stats["outliers"]:
        lines += ["", "## Outlier frames", "",
                  "| FITS | Residual | Reported σ |", "|---|---|---|"]
        for o in stats["outliers"]:
            lines.append(f"| {o['fits']} | {o['residual']:+.3f} | {o['unc']:.3f} |")

    lines += [
        "",
        "## Verdict",
        "",
        ("**ALL GATES PASS** ✅" if stats["all_gates_pass"]
         else "**GATES FAILED** ❌ — see table above"),
        "",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_figures(out_dir: Path, corpus: dict) -> list:
    """Residual / z-score / uncertainty figures. Best-effort (needs matplotlib)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []

    clean = [m for m in corpus["measurements"]
             if m["expect"] == "good" and m.get("residual") is not None]
    if not clean:
        return []
    res = [m["residual"] for m in clean]
    zs  = [m["z"] for m in clean if "z" in m]
    unc = [m["measurement"]["uncertainty"] for m in clean]
    mag = [m["measurement"]["magnitude"] for m in clean]

    made = []
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8))
    axes[0].hist(res, bins=15, color="#4878a8", edgecolor="white")
    axes[0].axvline(0, color="k", lw=1)
    axes[0].set_xlabel("measured − reference (mag)")
    axes[0].set_title("Residuals")
    axes[1].hist(zs, bins=15, color="#4878a8", edgecolor="white")
    for x in (-1, 1):
        axes[1].axvline(x, color="k", lw=0.8, ls="--")
    axes[1].set_xlabel("residual / reported σ")
    axes[1].set_title("Uncertainty calibration (z)")
    axes[2].scatter(mag, unc, s=18, color="#4878a8")
    axes[2].set_xlabel("measured magnitude")
    axes[2].set_ylabel("reported σ (mag)")
    axes[2].set_title("Uncertainty vs magnitude")
    fig.tight_layout()
    path = out_dir / "validation_summary.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    made.append(str(path))
    return made


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fits-dir", help="directory of FITS files to validate")
    ap.add_argument("--manifest", help="reference manifest JSON (see FIXTURE_MANIFEST.md)")
    ap.add_argument("--synthetic", action="store_true",
                    help="run the deterministic synthetic self-validation corpus")
    ap.add_argument("--out", default="cloud_data/validation",
                    help="output directory (default: cloud_data/validation)")
    ap.add_argument("--keep-frames", action="store_true",
                    help="keep the generated synthetic FITS corpus (~30 MB)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        work = out_dir / "synthetic_corpus"
        frames, base_cfg = build_synthetic_corpus(str(work))
        mode = "synthetic self-validation (deterministic, offline)"
    elif args.fits_dir and args.manifest:
        frames, base_cfg = build_real_corpus(args.fits_dir, args.manifest)
        mode = f"real data: {args.fits_dir}"
    else:
        ap.error("either --synthetic or both --fits-dir and --manifest are required")
        return 2

    print(f"Validating {len(frames)} frames ({mode}) …")
    corpus = run_corpus(frames, base_cfg)
    stats = analyse(corpus)

    if args.synthetic and not args.keep_frames:
        import shutil
        shutil.rmtree(out_dir / "synthetic_corpus", ignore_errors=True)

    (out_dir / "results.json").write_text(
        json.dumps({"mode": mode, "gates": GATES, "stats": stats,
                    "corpus": corpus}, indent=1, default=str),
        encoding="utf-8")
    write_report(out_dir, corpus, stats, mode)
    figures = write_figures(out_dir, corpus)

    print(f"  measured: {stats['n_measured']}  rejected: {stats['n_rejected']}")
    print(f"  median residual: {_fmt(stats['median_residual'])} mag   "
          f"RMS: {_fmt(stats['rms_residual'])} mag   z-RMS: {_fmt(stats['z_rms'], '.3f')}")
    print(f"  report: {out_dir / 'report.md'}")
    for f in figures:
        print(f"  figure: {f}")
    print("  GATES:", "PASS ✅" if stats["all_gates_pass"] else "FAIL ❌",
          json.dumps(stats["gate_verdicts"]))
    return 0 if stats["all_gates_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
