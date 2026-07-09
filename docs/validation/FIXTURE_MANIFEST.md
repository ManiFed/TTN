# Real-Data Validation: Fixture Manifest

What data beta nodes must collect, how it is organised, and how it feeds
`scripts/validate_photometry.py`. Until this corpus exists, validation
evidence comes from the deterministic synthetic corpus
(`--synthetic`); this manifest is the bridge from synthetic to real evidence.

## Directory layout

```
validation_corpus/
├── manifest.json                  # references + config (format below)
├── frozen_catalog.json            # pinned comparison stars (optional but preferred)
└── fits/
    ├── m67_2026-07-12_node042_stack300s.fits
    └── ...
```

## manifest.json format

```json
{
  "comparison_star_file": "frozen_catalog.json",
  "config_overrides": {
    "photometry": { "saturation_adu": 60000 }
  },
  "targets": {
    "SS Cyg": {
      "reference_mag": 8.72,
      "reference_source": "AAVSO LCGv2 visual+CV mean, JD 2461230–2461237",
      "tolerance_mag": 0.15,
      "expect": "good"
    },
    "HD 12345 (saturated test)": {
      "reference_mag": 6.1,
      "reference_source": "Tycho-2 VT->V",
      "expect": "flagged"
    }
  }
}
```

- `targets` is keyed by the FITS `OBJECT` header value.
- `expect`: `good` (should measure accurately), `flagged` (must not be
  quality=good), `rejected` (must produce a rejection record).
- `frozen_catalog.json` is a JSON list of
  `{auid, ra_deg, dec_deg, mag_v, mag_err, source}` — the pipeline's `file`
  catalog backend (`photometry.comparison_star_file`) reads it directly, so
  the run needs no network and is exactly reproducible later. Freeze it once
  from AAVSO VSP / APASS at collection time and commit it with the corpus.

## Collection campaigns (priority order)

### C1 — Header semantics ladder (unblocks timing validation)
One night, one node: 10 s, 30 s, 2 min, 5 min, 15 min Seestar stacks of any
bright field, with the node's system clock NTP-synced and the start/end wall
times logged manually. Purpose: confirm whether the Seestar's stacked-FITS
`DATE-OBS` is stack start, and whether `EXPTIME` is per-sub or accumulated.
Decides the correct setting of `photometry.bjd_midpoint_correction`.

### C2 — Linearity / saturation ladder
Same star field at exposures stepping peak flux from ~10% to hard clip
(e.g. 5/10/20/30 s subs). Purpose: measure the true usable full-scale
(`saturation_adu`) and any nonlinearity below it for the IMX462.

### C3 — Standard-field accuracy anchor
≥5 stacks per night, ≥3 nights, of an AAVSO-sequenced standard field
(M67 in season, or the NGC 7790 / SA 38 sequences). References: the AAVSO
sequence magnitudes themselves (frozen into `frozen_catalog.json`), with
2–3 sequence stars deliberately *excluded* from the catalog and treated as
targets. Purpose: end-to-end external accuracy, colour term vs B−V,
night-to-night repeatability.

### C4 — Known variable cross-check
Nightly stacks of 2–3 well-observed variables (e.g. SS Cyg, β Lyr, R Sct)
across ≥2 weeks. Reference: contemporaneous AAVSO light curve (CV/V mean of
other observers within ±0.5 d). Purpose: residual distribution on real
science targets, including colour extremes (R Sct is red — colour-term probe).

### C5 — Crowded field
Stacks at galactic latitude |b| < 10° with a catalogued target. Purpose:
centroid mis-lock rate, comparison-star blending.

### C6 — Multi-node co-observation
Same target, same hour, ≥2 nodes. Purpose: real cross-node scatter vs the
0.30 mag / 3σ cross-validation thresholds in `cloud/data_pipeline.py`.

### C7 — Flats / vignetting
Twilight flats or a heavily dithered bright-field sequence. Purpose:
illumination profile → position-dependent zero-point systematic bound.

## Running validation on a corpus

## One-command collection workflow

Create the campaign folders and reproducibility manifests:

```bash
make beta-init
python3 scripts/beta_capture.py add C1 /path/to/stack.fits \
  --node-id node042 \
  --started-utc 2026-07-12T02:10:00Z \
  --ended-utc 2026-07-12T02:15:00Z
make beta-audit
```

`add` copies the original FITS without modifying it, records the important
header values and independent wall-clock times, and pins the file with a
SHA-256 digest. Use C1–C7 to match the campaigns above.

Before leaving a node unattended, run:

```bash
make preflight
```

This read-only check fails unless configuration, scientific dependencies,
free disk, observer location, cloud identity/DNS, image watch path, and plate
solver are ready. Add `--json` when collecting the result remotely.

```bash
python3 scripts/validate_photometry.py \
    --fits-dir validation_corpus/fits \
    --manifest validation_corpus/manifest.json \
    --out cloud_data/validation/real_run_2026-07
```

Outputs `report.md` (gate verdicts), `results.json` (every measurement with
full provenance, every rejection with machine-readable reason), and summary
figures. Exit code 0 ⇔ all gates pass, so the same command gates CI once a
reference corpus is committed.
