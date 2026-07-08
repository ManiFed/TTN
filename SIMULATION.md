# The Telescope Net — Fleet Digital Twin (`sim/`)

A deterministic, offline simulation and backtesting environment for The
Telescope Net.  It generates synthetic fleets, target catalogs, weather, and
measurement outcomes; runs the **production CHORUS solver unchanged** over
many simulated nights; compares it against baseline schedulers on identical
worlds; and produces science-yield projections with uncertainty ranges.

Everything the twin outputs is **synthetic projection**, and every artifact
is stamped as such.  Nothing here reads or writes the production database,
calls a weather API, or touches telescope hardware.

## Quick start

```bash
# list scenarios
python -m sim list

# one full scenario, all 5 schedulers, 3 seed replicates → sim_results/
python -m sim run --scenario beta_5_nodes

# custom: two schedulers, your seeds, shorter run
python -m sim run --scenario launch_50_nodes --schedulers chorus,random \
    --seeds 42,43 --nights 7

# marginal value of scale: same catalog, growing fleet
python -m sim sweep-nodes --sizes 5,20,50,100 --nights 7

# CI-safe test suite (~8 s)
python -m pytest tests/test_sim.py
```

Outputs land in `sim_results/<scenario>/`: `summary.json` (config +
per-replicate metrics + aggregates), `nights.csv` (per-night records), and
`README.md` (human summary with the scheduler comparison table).

---

## 1. CHORUS audit — what the twin has to reproduce

The production night pipeline (`cloud/chorus/planner.py`) and its inputs and
outputs, with where the twin gets each input:

| Production input | Source in production | Source in the twin |
|---|---|---|
| Node rows (optics, sensor, site, limits) | `nodes` table | `sim.world.SimNode.as_row()` — same dict shape |
| Target rows (class, position, mag, priority, cadence) | `targets` table | `sim.world.SimTarget.as_row()` |
| Weather forecast per slot | 7timer/Open-Meteo via `cloud.conditions` | `sim.weather` synthetic forecast (truth hidden) |
| Night window, alt/az curves | astropy via `cloud.conditions` | `sim.skymath` pure-math equivalents (validated <0.3° vs astropy) |
| Reliability ledger (p_exec, p_accept, κ, explore) | `cloud.chorus.ledger` Beta posteriors from measurement history | `sim.engine.NodeBelief` — same conjugate updates, learned only from simulated outcomes |
| Target state (EB phase coverage, CV hazard clock/outburst, transient age) | `chorus_target_state` | `sim.engine.TargetBeliefState` — same fields, updated only from accepted simulated measurements |
| Transit ephemerides | `transit_ephemerides` table | `SimTarget.transit` truth → `sim.engine.transits_tonight` |
| Site weather calibration (a, b) | Ring-0 logistic fits | identity (young-site production default); see Limitations |
| Tuned hyperparameters | `tuning_state` chorus group | `cloud.chorus.params.DEFAULTS` (overridable per scenario) |

| Production output | The twin's equivalent |
|---|---|
| `ObservationPlan`/`PlanItem` JSON | placements (`assign.Placement`) — same information, not serialized to plan JSON |
| `plan_runs` telemetry (Φ, expected deliveries) | per-night records in results |
| contingency ladders | not simulated (see Limitations) |
| backtest archive | superseded by the twin itself (the twin is the forward-looking version of `backtest.py`'s replay) |

**Code reuse contract:** `cloud/chorus/assign.py` (lazy submodular greedy),
`cells.py` (templates/kernels), `physics.py` (σ model, exposure optimizer),
`horizon.py` (scarcity), and `cloud/network_planner.py::assign_network` (the
legacy solver) are imported and executed **unmodified**.  The twin
reimplements only the I/O layers around them (DB reads, astropy, live
forecasts), mirroring `planner.py` stage for stage.

### Assumption classification (production CHORUS)

**Physical** (derivable from optics/astronomy, credible a priori):
signal/noise electron budget scaled from the Seestar anchor; scintillation
(Young); airmass extinction; comparison-star ensemble floor vs FoV; field
rotation capping sub length; visibility geometry.

**Empirical** (measured in production, estimable): per-node p_exec /
p_accept / κ posteriors; per-site forecast calibration; monthly climatology;
CV hazard rates.  *The twin treats these as truth parameters the scheduler
must learn, which is exactly their production epistemic status.*

**Heuristic** (chosen, tunable, Ring-1): cell value densities per class
(eclipse bins 2.5×, quadrature 1.5×); σ_ref per class; kernel length scales;
scarcity γ and horizon; exploration β; `min_marginal` stop; portfolio caps;
transit sub-window weights.

**Currently unvalidated in production** (the twin's reason to exist):
whether expected Φ predicts realized yield; whether weather hedging pays at
network scale; whether the exploration bonus on-boards nodes efficiently;
how the solvers scale beyond ~10 nodes; whether `min_marginal` leaves
telescope capacity idle that cheap science could use.

**Audit finding (production, pre-existing):** in `planner.py`, transit event
cells are only registered when the exoplanet's `target_id` is *not* already
in `cells_by_target`; an exoplanet host that is also an active `targets` row
gets generic time cells and its transits are never valued as event
sub-windows.  The twin implements the documented intent (event cells for
transit hosts) rather than this accident.

---

## 2. The twin's model, layer by layer

Every parameter below is explicit in code and overridable per scenario
(`sim/scenarios.py::Scenario`).  Design rule: *simple credible models over
elaborate opaque ones* — each mechanism is a few lines you can read.

### 2.1 Nodes (`sim/world.py`)

Five hardware classes (Seestar S50/S30, 80 mm refractor, 150 mm cooled
Newtonian, 200 mm cooled SCT) with realistic declared specs, drawn with a
default mix of 70% smartscopes.  Twelve geographic regions with lat/lon
boxes, sky brightness ranges, and coarse seasonal clear-night climatology
(archetypes, not site forecasts).

Each node carries hidden **truth parameters** the schedulers never see:

| Truth parameter | Default range | Meaning |
|---|---|---|
| `p_exec_true` | 0.50–0.92 by class | P(delivers data \| clear sky, online) |
| `p_accept_true` | 0.70–0.95 by class | P(delivered data passes QC/AAVSO) |
| `kappa_true` | 1.0–4.0 by class | delivered/physics variance ratio |
| `p_night_up` | 0.80–0.97 | P(node is online at all tonight) |
| `forecast_skill` | 0.55–0.85 | how much the site forecast reflects truth |

Schedulers hold Beta(4,2) priors (production's "start at 2/3") and learn from
outcomes with production's 60-night half-life decay.

### 2.2 Targets (`sim/world.py`)

Default catalog mix: 22% EB (with ephemerides), 18% CV, 20% LPV,
10% exoplanet hosts, 30% generic variables, plus Poisson transient arrivals
(default 0.15/night).  Class truths: CV outbursts are a per-target Markov
process (rate 0.01–0.05/night, 3–10 night duration, 2–5 mag brightening) —
**the scheduler's value model only escalates after the network actually
catches the outburst**; transients fade at 0.08 mag/night after night 5 and
retire at mag 17.5; transits recur on their true ephemerides.

### 2.3 Weather (`sim/weather.py`)

Nightly regional driver + site noise (correlation ρ, default 0.5; 0.85 in
`bad_weather_week`), a logistic link to climatology, and a 2-state Markov
chain per 15-min slot (persistence 0.85 ≈ 2 h coherence).  The forecast is a
skill-weighted blend of smoothed truth and climatology plus noise and
optional bias — schedulers see only the forecast; outcomes use only the
truth.

### 2.4 Outcomes (`sim/outcomes.py`)

The causal chain per placement: node online? → sky clear over the dwell
(truth)? → executed (Bernoulli p_exec_true)? → realized
σ = σ_phys·√κ_true·e^ε, ε~N(0,0.12) → accepted (Bernoulli p_accept_true AND
σ_real ≤ 0.25 AND no catalog failure).  Time-series dwells ride through
partial cloud (≥25% clear) with value scaled by the clear fraction;
single-epoch dwells need ≥75% clear.

### 2.5 Determinism

Same (scenario, seed) → identical output, byte for byte.  All randomness
flows through `sub_rng(seed, *tags)` (crc32-keyed `random.Random`, immune to
`PYTHONHASHSEED`).  Weather and node-outage draws depend only on
(seed, night, region/node), so they are identical for every scheduler and
unchanged when other nodes are added — the guardrail tests rely on this.
The production solvers' *time-boxed* local searches are disabled by default
(`local_search_ms = 0`) because wall-clock budgets make results
machine-dependent; enable explicitly for solver-quality studies.

---

## 3. Schedulers compared (`sim/schedulers.py`)

| Name | What it is |
|---|---|
| `chorus` | production `cloud.chorus.assign.assign`, unchanged |
| `legacy` | production `cloud.network_planner.assign_network`, unchanged (redundancy decay, cadence bonus, longitude diversity); base scores reconstructed composite-style since the DB history it normally reads doesn't exist in a synthetic world |
| `greedy_value` | per-node independent greedy — every node takes its own best targets, no coordination (what an uncoordinated fleet does) |
| `greedy_nearest` | per-node nearest-good-target greedy (slew-miser heuristic) |
| `random` | uniform random feasible assignment — the floor |

Fairness policy: all schedulers receive the same contexts, opportunities,
forecasts, physics predictions, and reliability beliefs; all outputs pass the
same hard feasibility validator, the same realized-value accounting, and the
same outcome realizer.  Headline metrics include scheduler-agnostic counts
(accepted measurements, latency, transit coverage, wasted minutes) so CHORUS
is not judged solely by its own objective currency.

## 4. Metrics (`sim/metrics.py`)

Per run: planned/executed/accepted per night, AAVSO-submittable per month,
acceptance rate, expected-vs-realized calibration ratio, wasted minutes,
realized information-cell value and capture fraction, distinct targets,
redundancy rate, alert response fraction and median latency, transit events
covered (≥50% of the in-transit window) and mean coverage, UTC hours with
≥1 accepted measurement (geographic continuity), node-load Gini.
Aggregation across seed replicates reports mean ± sd and min/max.

## 5. Scenario library (`sim/scenarios.py`)

`beta_5_nodes`, `launch_50_nodes` (+ `_global` counterfactual),
`growth_200_nodes`, `global_1000_nodes`, `bad_weather_week`, `alert_storm`,
`southern_gap`, `unreliable_fleet`, `exoplanet_campaign`,
`photometry_quality_crisis`.  Each is a frozen dataclass; `.variant(**kw)`
derives counterfactuals (that is how the studies isolate one factor at a
time).

## 6. Validation status

* `sim.skymath` validated against the production astropy path: altitudes
  within 0.3°, identical night windows, moon illumination within 2%.
* Guardrail tests assert the twin reproduces obvious physics (reliability ↑
  → yield ↑; bad weather → yield ↓; longitude spread → wider UTC coverage;
  no scheduler can place an infeasible observation).
* **Not yet validated against production history**: absolute accepted-per-
  night rates.  The twin's relative comparisons (scheduler vs scheduler,
  scenario vs counterfactual) are its credible product; absolute numbers
  should be quoted as model projections with the stated assumptions.  Once
  ≥30 nights of production `plan_runs`/measurement history exist for ≥5
  nodes, calibrate `p_exec/p_accept/κ` ranges and the weather model against
  them (the CHORUS backtest archive already captures the needed inputs).

## 7. Known limitations

* No contingency-ladder execution: a clouded-out node fails its item rather
  than degrading to an alternate (pessimistic for all schedulers alike).
* Identity forecast calibration (no Ring-0 site fits) — matches young-site
  production behavior; understates mature-site CHORUS performance.
* No intra-night incremental repair: alerts arriving mid-night are planned
  from the next night (latency is measured accordingly; pessimistic).
* Horizon masks are flat 25° minimum altitude; no per-site obstructions.
* T1 scarcity sweeps sample ≤25 nodes (approximation; exact at ≤25 nodes).
* Per-node candidate shortlists (`max_candidates_per_node`, default 60 like
  production's `LIMIT 60`) are ranked node-specifically (residual cell value
  × the node's physics capture at transit altitude).  An earlier build ranked
  them node-independently, which made `shortlist × portfolio-cap` an
  artificial network-wide ceiling at ≥50 nodes — kept here as a worked
  example of why `planned_per_night` should always be sanity-checked against
  capacity when adding scenarios.
* Climatology is regional archetype, not site truth.
* Filter selection is first-available; band-cell routing is exercised only
  through fleets that carry B/V/R hardware classes.
