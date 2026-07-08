# The Telescope Net — Fleet Digital Twin Report

**Status:** simulation study · **all quantitative results in this document
are synthetic projections** from the seeded digital twin in `sim/`, not
observed production data.  Production evidence is labeled as such where it
appears.

*Prepared for: partners, grant reviewers, AAVSO conversations, internal
roadmap planning.*

---

## 1. What this is

The Telescope Net is an autonomous network of member telescopes — mostly
Seestar-class smartscopes — whose cloud scheduler (CHORUS) assigns targets
nightly, whose node agents execute observations, and whose accepted
photometry is submitted to AAVSO.

Before recruiting the next fifty members or promising partners a given
science yield, we want to answer questions the live network is still too
small to answer empirically: *What does this network produce at 50, 200,
1,000 nodes?  Is reliability or geography the binding constraint?  Does the
scheduler's coordination actually pay?*

The fleet digital twin simulates the network as an evolving instrument:
synthetic fleets with realistic hardware physics and hidden reliability;
synthetic weather with regional correlation and imperfect forecasts;
synthetic target populations (eclipsing binaries, CV outbursts, transients,
exoplanet transits, LPVs); and the **production scheduler code executed
unchanged** over many simulated nights.  Every run is deterministic in its
seed, offline, and reproducible with one command
(`python -m sim run --scenario …`; see `SIMULATION.md`).

**How to read the numbers.**  The twin's *relative* comparisons — scheduler
A vs scheduler B on identical nights, scenario vs counterfactual — are its
credible product, because both sides share every assumption.  *Absolute*
rates (accepted measurements/night) inherit the stated assumptions about
reliability, weather, and QC, and have not yet been calibrated against
production history; quote them as model projections with error bars, not
commitments.

## 2. Model design in one page

- **Nodes** carry a declared layer (the exact hardware/site record the
  production planner reads: aperture, sensor, FoV, cooling, mount, sky
  brightness, exposure limits) and a hidden truth layer (execution
  reliability, QC acceptance, photometric efficiency κ, nightly uptime,
  forecast skill).  Schedulers never see truth; they learn it the way
  production does, through Beta-posterior reliability ledgers updated from
  realized outcomes.
- **Targets** follow class-specific processes: EBs with real ephemerides and
  persistent phase-coverage ledgers; CVs with hidden Markov outbursts the
  network must *catch* before its value model reacts; transients arriving as
  Poisson alerts and fading; exoplanet transits recurring on true
  ephemerides with ingress/egress/baseline structure; LPV and generic
  variable ballast.
- **Weather** is a regionally-correlated nightly process with slot-level
  (15-min) persistence; schedulers see only a skill-degraded forecast,
  outcomes use only the truth.
- **Outcomes** follow the production causal chain: node online → sky clear →
  executed → realized σ (physics × true κ × frame lottery) → QC/AAVSO gates.
- **Sky geometry** (night windows, alt/az, moon) is computed by pure-math
  formulas validated against the production astropy path to <0.3°.

Full assumptions, defaults, and knobs: `SIMULATION.md`.

## 3. Schedulers compared

All strategies receive identical inputs (contexts, opportunities, forecasts,
physics predictions, reliability beliefs) and pass identical feasibility
validation, value accounting, and outcome realization:

| | |
|---|---|
| **chorus** | production information-theoretic solver, unchanged |
| **legacy** | production composite-score network optimizer, unchanged solver and objective; base scores reconstructed (its DB history doesn't exist in a synthetic world) |
| **greedy_value** | per-node independent greedy (an uncoordinated fleet) |
| **greedy_nearest** | per-node nearest-good-target greedy (slew miser) |
| **random** | uniform random feasible assignment (floor) |

Because "science value" could bias toward CHORUS's own currency, every
comparison also reports scheduler-agnostic counts: accepted (AAVSO-
submittable) measurements, distinct targets, alert latency, transit
coverage, wasted telescope minutes, node-load fairness.

## 4. Scenario results

All values are mean ± sd over seed replicates {42, 43, 44} (1,000-node runs:
{42, 43}); full artifacts in `sim_results/<scenario>/`.  "Accepted" means a
measurement that executed, passed the σ ≤ 0.25 mag QC gate, and survived the
node's acceptance lottery — the twin's proxy for *AAVSO-submittable*.
"Value" is realized information-cell value, computed identically for every
scheduler after the fact.

### 4.1 Growth path (chorus scheduler)

| scenario | nodes | accepted/night | value/night | acceptance rate | transit events covered | UTC h covered | alert latency (median) |
|---|---|---|---|---|---|---|---|
| beta_5_nodes | 5 | 16.2 ± 3.0 | 6.9 ± 3.7 | 0.30 | 3% | 8.5 | 39 h |
| launch_50_nodes | 50 | 126.4 ± 5.9 | 39.1 ± 9.7 | 0.54 | 41% | 23.4 | 10 h |
| growth_200_nodes | 200 | 152.3 ± 5.7 | 65.0 ± 15.9 | 0.53 | 51% | 23.8 | — |
| global_1000_nodes | 1000 | 137.5 ± 12.5 | 81.2 ± 11.2 | 0.56 | 41% | 23.3 | — |

Two structural transitions stand out.  Between 5 and 50 nodes the network
changes kind, not just size: acceptance rate nearly doubles (weather/
reliability hedging starts working), round-the-clock coverage appears
(8.5 → 23.4 UTC-hours), transit coverage goes from negligible to ~40%, and
alert latency drops fourfold.  Between 200 and 1,000 nodes, accepted counts
*plateau* on this fixed 400-target catalog — the fleet stops being the
binding constraint (§6, recommendation 4) — while value per night keeps
rising because coordination routes each target to ever-better-matched
hardware and skies.

### 4.2 Scheduler comparison

At 5 nodes, coordination has little to coordinate: CHORUS is statistically
tied with the best baseline on raw counts (16.2 ± 3.0 vs greedy's
15.9 ± 3.1 accepted/night) and leads modestly on value (6.9 vs 6.3).  We
report this deliberately: a scheduler upgrade is not the beta network's
bottleneck, geography is (§6.1).

From 50 nodes upward the gap is decisive and grows with scale
(launch_50_nodes, 14 nights):

| scheduler | accepted/night | value/night | acceptance rate | transit coverage | alert latency | node-load Gini |
|---|---|---|---|---|---|---|
| chorus | **126.4 ± 5.9** | **39.1 ± 9.7** | **0.54** | **0.41** | **10.4 h** | **0.32** |
| greedy_value | 106.2 ± 9.3 | 27.3 ± 4.0 | 0.40 | 0.27 | 26.8 h | 0.58 |
| greedy_nearest | 102.3 ± 9.8 | 24.8 ± 3.3 | 0.39 | 0.14 | 23.2 h | 0.59 |
| random | 89.0 ± 7.6 | 22.9 ± 5.9 | 0.33 | 0.11 | 12.9 h | 0.29 |

At 200 nodes: chorus 152 accepted / 65.0 value vs ≈109 / ≈34 for the
greedies — and the greedies' node-load Gini reaches 0.88 (a minority of
well-placed nodes do nearly everything, the rest idle), vs 0.33 for CHORUS.
Load fairness is a member-retention issue, not just an efficiency number.

**The legacy scheduler** (production's composite-score optimizer, run
unchanged) tells a more interesting story (50 nodes, 7 identical nights):

| | chorus | legacy |
|---|---|---|
| planned/night | 250 | 600 (fills every slot) |
| accepted/night | 136.2 ± 2.4 | **210.1 ± 25.1** |
| value/night | **44.4 ± 13.0** | 32.6 ± 8.5 |
| redundancy (obs per target) | 3.6 | 8.3 |
| transit events covered | **42%** | **0%** |
| wasted minutes/night | 1,604 | 2,044 |

Legacy wins the raw count by re-observing the same ~72 targets 8× each; a
third more submittable measurements, a quarter less science value.  Its 0%
transit coverage reproduces a failure mode CHORUS was designed against
(documented in CHORUS.md §9.1): legacy's all-or-nothing multi-hour transit
windows always lose a slot-packing contest against many short observations.
Legacy also hit a computational wall in the twin: its assignment is
quadratic in fleet × targets (~2 minutes per simulated night at 50 nodes vs
~6 s for CHORUS), and is not practically runnable at 200+ nodes — CHORUS's
lazy submodular solver is the only one of the two that scales to the
network's ambitions.

### 4.3 Stress scenarios (50 nodes; chorus vs best baseline)

| scenario | chorus | best baseline | reading |
|---|---|---|---|
| bad_weather_week (26% clear, correlated storms, degraded forecasts) | 88.5 acc / 52.7 val | 55.8 acc / 30.7 val (greedy_value) | CHORUS loses 30% of its yield; uncoordinated fleets lose ~47%.  Cross-site weather hedging is arithmetic, and it pays exactly when weather is worst. |
| alert_storm (6 transients in one night) | median latency **4.3 h**, 102 SN measurements | latency 11.1–15.7 h, 55–69 SN measurements | Same fraction of alerts eventually observed (87%) — but CHORUS gets there the same night. |
| southern_gap (all-northern 50) | 103.5 acc, alert response 33% | — | vs 126–128 for a global 50: the southern blind spot costs ~18% of yield, ~20% of reachable targets, and two-thirds of alert responses. |
| unreliable_fleet (true reliability ×0.6, uptime ~65–85%) | 27.4 acc / 14.2 val | 21.4 / 8.9 | Everyone suffers; CHORUS's reliability ledger learns who actually delivers and hedges accordingly (+28% counts, +60% value over baseline). |
| exoplanet_campaign (30% transit hosts) | 34% of events covered | 22% (greedy_value) | Baselines *observe exoplanet hosts more* but *cover events less* — coverage needs coordinated sub-window composition, not raw pointings. |
| photometry_quality_crisis (κ×3, 15% catalog failures) | 110.1 acc (−13% vs base) | 90.6 (−15%) | Graceful degradation; no scheduler death-spirals on bad photometry, because the QC gate at 0.25 mag is forgiving for bright targets. |

## 5. Science-yield forecasts by network phase

**These are synthetic projections under the stated assumptions** (default
hardware mix ~70% smartscopes; true per-obs reliability 0.5–0.92 by class;
regional climate archetypes; σ ≤ 0.25 mag QC gate; 200–400-target catalog;
CHORUS with default parameters).  Ranges are ±1 sd across seed replicates —
they capture world-to-world variation, not model misspecification.  None of
these numbers has yet been calibrated against production history (§7.1).

| phase | accepted measurements/night | AAVSO-submittable/month | 24-h coverage | transit events covered | median alert response |
|---|---|---|---|---|---|
| **5 nodes** (today's shape) | 13–19 | ~400–580 | ~8.5 h/day | rare (<5%) | 1–2.5 nights |
| **50 nodes** | 120–133 | ~3,600–3,970 | 23.4 h/day | ~40% | ~10 h (same night for most alerts) |
| **50 nodes, retuned CHORUS** (§6.2) | 195–215 | ~5,900–6,500 | 23.4+ h/day | ~40% | ~10 h |
| **200 nodes** | 146–158 (catalog-limited) | ~4,400–4,740 | 23.8 h/day | ~51% | sub-night |
| **1,000 nodes** (400-target catalog) | 125–150 (catalog-limited) | ~3,750–4,500 | 23.3+ h/day | ~41% | sub-night |

Marginal value of a node (fixed 200-target catalog, from the fleet-size
sweep): **+4.4 accepted/night per node** when growing 5→10, +4.8 at 10→20,
+1.3 at 20→50, +0.18 at 50→100, **+0.04 at 100→200**.  Diminishing returns
set in between 50 and 100 nodes *for this catalog size*; they are a property
of the science program, not the network — which is why recommendation 4
(grow the catalog with the fleet) exists.

Marginal value of the improvement levers at 50 nodes (chorus, 14 nights):

| lever | accepted/night | Δ vs base | value/night | wasted min/night |
|---|---|---|---|---|
| base | 126.4 ± 5.9 | — | 39.1 | 1,519 |
| **reliability +15%, uptime →92–99%** | **157.1 ± 4.9** | **+24%** | 42.7 | **938 (−38%)** |
| southern rebalance (same size) | 128.2 ± 5.8 | +1% | 38.6 | 1,478 |
| better photometry (κ×0.6) | 125.3 ± 6.5 | 0% | 40.6 | 1,511 |

Geography stops mattering once you already span longitudes (the default
50-node fleet does); reliability never stops mattering.  Better photometry
buys measurement *quality* (value, faint-end reach) rather than count — it
matters for the science mix, not the AAVSO tally.

## 6. Strategic findings and recommendations

Ranked by evidence strength × leverage.  Every claim traces to a named
artifact in `sim_results/`.

**1. Recruit the next 10 nodes in the southern hemisphere — South America,
Australia, or southern Africa, in that order of yield; Australia if 24-h
continuity is the goal.**  From today's 5-node northern base, +10 southern
nodes deliver 64–66 accepted/night versus 51–54 for the best northern
options (+22%), and Australia stretches daily coverage to 17.1 UTC-hours
(vs 11.9 doubling down in the US Southwest).  The southern_gap scenario
shows the cost of not doing this at 50 nodes: −18% yield and alert response
collapsing from 67% to 33%.  *(recruitment_geography.json,
southern_gap/)*

**2. Retune CHORUS's utilization knobs through the existing Ring-1 backtest
gate.**  With default parameters (`min_marginal = 0.02`,
`max_obs_per_target = 4`), CHORUS leaves more than half the 50-node fleet's
capacity idle: 233 planned observations/night against 600 slots.  Lowering
the stop threshold to 0.005 and the portfolio cap to 8 yields **+63%
accepted measurements (126 → 205/night) with science value unchanged or
better** (39.1 → 43.3).  Pushing further (ε = 0.001, cap 12) adds counts but
no value — pure redundancy.  This single parameter change is worth more
submittable measurements than doubling the fleet from 50 to 100 nodes.
Longer term, make the stop threshold adaptive: idle capacity should lower ε
automatically, because an idle telescope has zero opportunity cost.
*(chorus_utilization_probe.json)*

**3. At ≥50 nodes, reliability is the bigger bottleneck than geography —
run a reliability program, not just a recruitment program.**  +15% true
execution/acceptance reliability with uptime pushed to 92–99% is worth +24%
yield and −38% wasted telescope minutes; a same-size geographic rebalance is
worth +1%.  Concretely: dew/power/enclosure guidance for members, node
health monitoring, and treating the reliability ledger's p_exec posteriors
as an operations dashboard, not just a scheduler input.  The unreliable_fleet
scenario shows the downside tail: fleet-wide reliability ×0.6 destroys 78%
of yield no matter who schedules.  *(improvement_levers.json,
unreliable_fleet/)*

**4. Scale the target catalog with the fleet.**  With 200–400 targets, the
network saturates at 50–100 nodes (+0.04 accepted/night per marginal node by
100→200); at 1,000 nodes the fleet runs at ~2% of slot capacity.  The
instrument outgrows the program long before it outgrows the hardware.
Growing toward 200 nodes should be paired with 3–5× more active science
targets (AAVSO campaigns, TOM programs, long-tail CV/EB monitoring lists) —
in the twin, value per night still rises with fleet size whenever the
catalog offers uncaptured cells.  *(node_scaling.json,
global_1000_nodes/)*

**5. Keep CHORUS as the default scheduler; the case strengthens with scale
and with bad weather.**  It matches baselines where coordination can't help
(5 nodes) and dominates everywhere else: +19–45% accepted and +43–102% value
over the best uncoordinated baseline at 50–1,000 nodes; the *only* scheduler
that covers transit events under capacity pressure (41–51% vs 0% for
legacy); ~3× faster alert response in an alert storm; and ~2.6× more even
member workload at 200 nodes.  Its advantage is largest exactly when
conditions are worst (bad_weather_week: −30% yield vs −47% for baselines).
*(launch_50_nodes/, growth_200_nodes/, bad_weather_week/, alert_storm/,
launch_50_nodes_legacy7/)*

**6. Retire the legacy solver before the fleet reaches ~100 nodes.**  Its
assignment loop is quadratic (≈2 min/night at 50 nodes in the twin,
projected hours at 200+); it produces volume (210/night) but 27% less
science value at 8.3× redundancy and misses every transit.  If raw
submission counts matter for AAVSO relations, recommendation 2 gets there
without legacy's pathologies.  *(launch_50_nodes_legacy7/)*

**7. Fix the transit event-cell registration bug found during the audit.**
In production `planner.py`, an exoplanet host that is also an active catalog
target never receives its ingress/egress event cells — transits are valued
against generic time bins.  The twin implements the documented intent, and
its exoplanet results (41–51% event coverage) therefore represent the fixed
behavior.  (Flagged as a separate work item.)

**8. Fragility watchlist (what the twin says to monitor in production).**
(a) *Expected-vs-realized calibration*: the planner's expected deliveries
run 23% under realized at 50 healthy nodes (conservative), 26% *over* at an
unreliable fleet, and 57% under at 1,000 nodes — Ring-0's calibration
machinery should track this ratio per fleet segment.  (b) *Class mix*:
CHORUS's phase-cell machinery concentrates heavily on eclipsing binaries
(~60% of accepted measurements at 50 nodes); if the science program wants a
different mix, the `value_scale_*` Ring-1 parameters are the lever — this is
a priority choice the twin can now price.  (c) *Scarcity-driven idling*
(recommendation 2) is the scheduler's most fragile behavior: it is invisible
in plan quality metrics and only shows up as capacity utilization.

## 7. Limitations

1. **No production calibration yet.**  Reliability ranges, κ ranges, and
   climatology are informed priors, not fits to Telescope Net history.  The
   CHORUS backtest archive already captures the inputs needed to calibrate
   the twin once ~30 nights × 5 nodes of production data accumulate.
2. **No contingency-ladder execution** — a clouded-out node fails its item
   rather than degrading to its precomputed alternate; realized yields are
   uniformly pessimistic (all schedulers equally).
3. **No intra-night repair** — alerts arriving mid-night are planned from
   the next night, so alert latencies are upper bounds.
4. **Identity forecast calibration** — Ring-0 per-site fits are not
   simulated; mature-site CHORUS performance is understated.
5. **Regional climate archetypes** — coarse; suitable for comparing
   geographies, not for siting a specific observatory.
6. **QC model is a threshold** (σ ≤ 0.25 + Bernoulli acceptance + catalog
   failures), simpler than AAVSO's real review path.
7.  The realized-value metric shares its cell/capture arithmetic with
   CHORUS's objective family; that is why count-based metrics are always
   reported alongside it.

## 8. Reproducing this report

```bash
python -m sim.studies core      # scenario library (fast schedulers)
python -m sim.studies legacy    # legacy-solver comparisons (slow)
python -m sim.studies scale     # 200/1,000-node + fleet-size sweep
python -m sim.studies strategy  # recruitment / reliability / parameter probes
```

Each chunk is deterministic in its seeds and writes JSON/CSV/Markdown under
`sim_results/`.  The tables in this report are transcriptions of those
artifacts.
