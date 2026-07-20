# Algorithm Improvement Plan — CrowdSky Merge & Science Portfolio Diversification

> Context: we are merging databases with CrowdSky / CrowdSci (University of
> Vienna, WWTF grant 10.47379/DARE25064). AAVSO submission ends. CrowdSci's
> broker today is exoplanet/EB-heavy (eclipsing_binary: ~28,800 targets,
> exoplanet_transit: ~90, sky_survey: 5,000, supernova: 1). The goal of this
> plan is to make CHORUS the diversified, portfolio-aware scheduling brain of
> the combined network — not just a transit machine.

---

## 1. Guiding principle

CrowdSky brings archive scale and target volume; we bring the only fleet-level
optimizer in the amateur smart-telescope space. The merge is most valuable if
CHORUS treats science programs as a **portfolio** with explicit allocation
targets, rather than letting whichever class has the most targets (EBs, by
300:1) dominate greedy selection. Every change below serves one of three aims:

1. **Decouple** science value from any single submission pipeline (AAVSO → CrowdSky).
2. **Diversify** the nightly plan across science classes deliberately, not accidentally.
3. **Exploit** what a coordinated fleet can do that CrowdSci's per-user random
   target picker cannot: longitude relay, simultaneous coverage, cadence guarantees.

---

## 2. Workstream A — Submission & data-model decoupling (prerequisite)

The AAVSO assumption is threaded through `cloud/alerts.py`, `external_coverage.py`,
`survey.py`, `db.py`, `server.py`, `data_pipeline.py`, `registry.py`,
`scoring.py`, `crossmatch.py`, and `aavso_submissions/`.

- **A1. Submission-sink abstraction.** Introduce a `SubmissionSink` interface
  (submit, status, attribution) with a CrowdSky implementation (their upload
  API / AstroWISE ingest) and retire the AAVSO WebObs path. Member attribution
  ("observed by X") must survive the transition — it is the product.
- **A2. Target-catalog federation.** Replace/augment our target tables with a
  sync from CrowdSci's `GET /api/modules.php` + `targets.php`. Map their
  modules → our `target_type` → CHORUS `family_of()` templates. Keep our own
  target metadata (ephemerides, state vectors, reliability) keyed by a stable
  cross-ID (their ID + our crossmatch).
- **A3. External-coverage rewrite.** `external_coverage.py` currently asks
  "has AAVSO already covered this?" Point it at the CrowdSky time-domain
  archive instead — and now it also sees *uncoordinated* Seestar uploads from
  non-fleet users, which is a major new signal (see C3).
- **A4. Feedback loop.** CHORUS Ring-0 calibration (`ledger.py`) learns from
  what the science sink actually accepted. Wire acceptance/QC results from
  CrowdSky stacking-quality metrics back into per-node reliability vectors.

Exit criterion: one full night scheduled, observed, stacked, and attributed
end-to-end through CrowdSky with AAVSO code paths archived.

## 3. Workstream B — Portfolio-aware objective (the core algorithmic change)

CHORUS already has per-class `value_scale_*` knobs, but those are *prices*,
not *allocations* — with 28k EBs vs 86 transits, flat prices will produce a
monoculture. Add an explicit portfolio layer:

- **B1. Class allocation targets.** New Ring-1 params: `portfolio_share_{class}`
  (e.g. EB 30%, exoplanet 20%, transient/SN 15%, survey 15%, moving-object 10%,
  exploratory 10%). Implemented as a concave (diminishing-returns) wrapper on
  cumulative per-class value inside the T2 greedy — submodularity is preserved,
  so the lazy-greedy machinery in `assign.py` is untouched. Under-allocated
  classes see inflated marginals; over-allocated ones deflate smoothly rather
  than hitting a hard cap.
- **B2. Scarcity-aware rebalancing.** Portfolio shares are *seasonal* — transit
  windows and EB eclipses are ephemeris-driven. The T1 horizon sweep
  (`horizon.py`) should compute achievable share per class over
  `scarcity_horizon_nights` and let tonight's effective shares deviate toward
  what the sky actually offers (don't burn 20% on exoplanets on a night with
  one shallow transit).
- **B3. Backtest gate extension.** `backtest.py` currently gates on aggregate
  utility. Add per-class capture metrics so a tuning step can't "improve" total
  value by silently zeroing a program. Report a diversification index
  (effective number of classes, exp of Shannon entropy of value share) per
  simulated night.
- **B4. Digital-twin scenarios.** Add `sim` scenarios seeded from real CrowdSci
  module counts (28.8k EB / 86 transit / 5k survey) to tune B1–B2 offline
  before the merge goes live.

## 4. Workstream C — Class-specific algorithm upgrades

### C1. Eclipsing binaries (the volume program)
28,832 targets is too many to treat individually. Add a **phase-completion
ledger** per EB: `_eb_phase_cells` already builds phase cells; extend state so
the fleet works toward *complete phase coverage per target* (esp. secondary
eclipses and minima timing) and then retires the target for a season. Add an
O–C (observed-minus-computed) minima-timing objective — period changes are the
publishable science, and it's exactly what a longitude-distributed fleet does
better than any single site. Prioritization: rank the 28k by (period-change
interest × brightness feasibility × phase-coverage deficit), not randomly.

### C2. Exoplanet transits (the depth program)
`transit_windows.py` + `transit_cells()` are solid. Upgrades:
- **Multi-node simultaneous transits**: schedule ≥2 nodes on the same transit
  when geometry allows — combined photometry beats Seestar single-scope
  precision, and it is a capability CrowdSci users can't self-organize.
- **Longitude relay for long-duration / long-period events**: chain nodes
  across longitudes to cover full transits (incl. TTV candidates) that no
  single site can.
- **Ephemeris maintenance targets**: transits with stale/drifting ephemerides
  (high mid-time uncertainty) get scarcity boosts — ephemeris refresh for the
  ~86-target list is high-value, low-glamour science.

### C3. Transients & supernovae (the story program)
The GCN/reflex/triage stack exists; repoint it:
- Feed the CrowdSky archive's *uncoordinated* uploads into `triage.py` as a
  second discovery stream: our fleet becomes the **confirmation engine** for
  anomalies detected in anyone's stacked frames (their "1 supernova" module
  shows the appetite — their SN 2026sqf analysis page is the template).
- Reflex confirmation slots become a reserved portfolio share (B1), so a live
  alert never has to fight the EB backlog for marginal value.

### C4. Moving objects / planetary defence (the grant-alignment program)
CrowdSky's WWTF summary explicitly names planetary defence. We already have
`moving_objects.py` + `mpc_report.py`. Add a class template for asteroid
astrometry: short-arc follow-up of NEOCP candidates, with the coverage-kernel
treating *parallax baselines* (simultaneous observation from distant nodes)
as a distinct cell type — distributed simultaneous astrometry is a genuinely
novel amateur capability and a strong joint-grant narrative.

### C5. Survey (the filler program)
Map their `sky_survey` module onto our night-filler pass
(`filler_min_marginal`): survey tiles become the guaranteed-useful floor under
the portfolio, replacing ad-hoc filler targets, with tile priority driven by
CrowdSky's coverage map (fill their archive's spatial/temporal gaps).

## 5. Workstream D — Fleet + non-fleet coordination

The merged network has two populations: our scheduled nodes and CrowdSky's
self-directed uploaders. Treat the latter as a stochastic background field:

- **D1.** Estimate per-region "free coverage" probability from CrowdSky upload
  history; CHORUS discounts cells likely to be covered for free and spends
  fleet time where coordination is required (exact transit timing, phase gaps,
  simultaneity). This drops straight into the existing weather-survival /
  repeat-discount machinery in §4.3 of CHORUS.md.
- **D2.** Publish our nightly plan *into* CrowdSci as suggested targets, so
  self-directed users can voluntarily fill our low-priority cells — the two
  systems become complementary rather than duplicative.

## 6. Sequencing & success metrics

| Phase | Contents | Gate |
|-------|----------|------|
| 1 (merge-critical) | A1–A4 | End-to-end night via CrowdSky; zero AAVSO deps |
| 2 | B1–B4 + C5 | Backtest: diversification index ≥ 4 effective classes with ≤5% total-utility loss vs current |
| 3 | C1 + C2 | EB phase-completion & O–C ledger live; first multi-node transit |
| 4 | C3 + C4 + D1–D2 | First fleet-confirmed archive anomaly; first MPC-accepted coordinated astrometry |

Ongoing metrics: per-class value share vs portfolio target, phase-coverage
completion rate (EB), transit ephemeris uncertainty reduction, alert→first-frame
latency (transients), fraction of fleet time spent on cells non-fleet uploads
would have covered anyway (should fall as D1 learns).

## 7. Open questions for the Vienna team

1. CrowdSky ingest API for programmatic (non-manual) submission — spec and auth?
2. Does attribution in AstroWISE support per-member credit on stacked products?
3. Can CrowdSci modules be extended (moving_object, cadence campaigns), and can
   we push coordinated target lists into their broker (D2)?
4. Access to their upload-history / coverage DB for the D1 background model?
5. QC / stacking-quality metrics available per frame for our Ring-0 feedback (A4)?
