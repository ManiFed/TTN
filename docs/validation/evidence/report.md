# Photometry Validation Run

- Generated: 2026-07-04T19:47:02+00:00
- Mode: synthetic self-validation (deterministic, offline)
- Frames: 30 (measured 27, rejected 3)

## Accuracy vs reference

| Statistic | Value | Gate | Pass |
|---|---|---|---|
| Median residual (mag) | -0.0086 | |x| ≤ 0.05 | ✅ |
| RMS residual (mag) | 0.0398 | ≤ 0.1 | ✅ |
| Max |residual| (mag) | 0.1004 | — | — |
| Uncertainty calibration RMS(z) | 0.624 | ≤ 1.5 | ✅ |
| Outliers (>max(0.3, 3σ)) | 0 | ≤ 5% of clean frames | ✅ |
| Pathological frames passed as good | 0 | = 0 | ✅ |

- Median reported uncertainty: 0.0581 mag
- Median zero-point scatter:  0.0460 mag

## Quality flags

| Flag | Count |
|---|---|
| acceptable | 8 |
| good | 16 |
| poor | 3 |

## Rejection reasons (machine-readable)

| reason_code | Count |
|---|---|
| non_light_frame | 1 |
| nonpositive_target_flux | 1 |
| too_few_comparison_stars | 1 |

## Comparison-star provenance (used stars)

| Catalog source | Stars used (frame-star pairs) |
|---|---|
| synthetic_truth | 204 |

## Verdict

**ALL GATES PASS** ✅
