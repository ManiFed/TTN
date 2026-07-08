# CHORUS

**C**overage-kernel **H**ierarchical **O**ptimization for **R**eliability-weighted
**U**tility of **S**amples — the successor to the composite-score →
marginal-value-greedy → annealing pipeline in `cloud/scoring.py`,
`cloud/objective.py`, and `cloud/network_planner.py`.

The metaphor is deliberate: a chorus of small, individually unremarkable voices
(mostly Seestar-class telescopes) producing, under a single deterministic
conductor, a performance no soloist could — voices entering in canon across
longitudes, parts assigned by what each voice can actually sing, and the whole
score rewritten every night from what the audience (AAVSO, the science) actually
heard.

> **Implementation status: built.** The design below is implemented in
> `cloud/chorus/` — `params.py` (Ring-1 hyperparameters), `physics.py`
> (measurement model + exposure optimizer), `cells.py` (templates, compiler,
> kernels), `horizon.py` (T1 scarcity), `assign.py` (T2 lazy submodular
> greedy + seeded local search), `perform.py` (T3 sequencing + contingency
> ladders), `ledger.py` (T0 reliability vectors, target state, Ring-0
> calibration), `planner.py` (orchestration), `backtest.py` (Ring-1 gate) —
> with schema in `cloud/db.py`, the tuning "chorus" group + backtest gate in
> `cloud/tuning.py`, and tests in `tests/test_chorus.py`. CHORUS is now the
> default live scheduler via `scheduler.chorus: true`; the former per-node
> greedy packer is archived under `cloud/archive/`.
>
> **Mid-night reuse:** `cloud/chorus/reflow.py` (see
> [ORGANISM.md](ORGANISM.md)) calls `assign.build_opportunities` and
> `assign.best_slot` directly to re-value a dropped node's remaining targets
> on other dark nodes between nightly runs. It is a caller of this module, not
> a fork of it — nothing here changed to support it.

---

## 0. Executive summary

The current system asks, for every (target, node) pair, *"how good is this
pairing?"* — a weighted average of normalized heuristics — and then coordinates
by multiplying that score with hand-tuned decay/bonus factors (redundancy decay,
cadence bonus, longitude-diversity recovery). Every coordination behavior the
network exhibits is a knob someone (or the nightly Claude monitor) set.

CHORUS asks a different question: **"how much scientific information do we
expect this observation to deliver that the network would not otherwise
have?"** — and answers it with three first-class, physically grounded models:

1. **A measurement model** — per (node, target, time, exposure) predicted
   photometric variance σ², computed from the telescope's actual optics,
   sensor, cooling, filter set, field of view, and site sky, via the physics
   already anchored in `src/telescope_specs.py`.

2. **A delivery model** — per observation probability *p* that the data
   actually arrives and is accepted, factored into per-slot sky probability
   (calibrated forecasts), node execution probability, and node acceptance
   probability — the last two being exactly the autodetected reliability
   signals `registry.refresh_node_performance` already computes, upgraded from
   one scalar to a small posterior vector.

3. **An information model** — each target carries a set of *information cells*
   (time bins, phase bins, event sub-windows, filter bands) with science value
   densities set by target class and campaign state. An observation *captures*
   a fraction of each cell it touches, as a function of its precision and its
   proximity in time/phase. The network objective is the expected total value
   captured, in expectation over which observations actually succeed.

Everything the current system encodes as a tuned multiplier — redundancy decay,
cadence bonus, longitude diversity, weather robustness relaxation, telescope
match, trust scaling — **emerges** from this objective as a theorem rather than
a knob:

| Current hand-tuned mechanism | CHORUS emergent equivalent |
|---|---|
| `redundancy_decay = 0.55` geometric penalty | Second observation of an already-captured cell multiplies a residual that is already small → automatic diminishing returns, *scaled by how likely the first observation was to succeed* |
| longitude-diversity recovery (`× sep/90° × 0.5`) | Observations at different longitudes touch different time cells → full marginal value, no recovery hack needed |
| `cadence_bonus_strength` | Time cells at cadence resolution: clustered samples hit the same cell, spread samples hit empty ones |
| `robustness_cloud_relax` for enclosure nodes | Enclosure/dew-heater raise p_exec (measured, not declared) — robust nodes win marginal-value ties in bad weather automatically |
| trust multiplier `×(0.5 + 0.5·trust)` | Reliability enters *where it physically acts*: p (data may not arrive), κ (data may be noisier than physics predicts), θ_out (data may be discarded) |
| `telescope_match` heuristic (+0.1 for wide field) | σ² model: FoV sets the comparison-star ensemble floor; aperture/cooling set the faint-end noise; the big cooled scope wins faint targets because its information gain is 100× larger, not because a rule says so |
| mag-bucket `choose_exposure` | Per-opportunity exposure optimizer: maximize information per occupied minute under the field-rotation sub cap |

The objective is **monotone submodular** in the set of placed observations, so
the global assignment is solved by **lazy cost-benefit greedy with a
(1 − 1/e)-style near-optimality guarantee**, followed by a seeded deterministic
local search — strictly stronger machinery than the current greedy + annealing,
with the same runtime budget and full determinism.

Above the night sits a **horizon tier** that prices scarcity ("this transit
window won't recur for 43 days; this LPV can be sampled any of the next 90
nights"), and below it an **execution tier** that keeps the current
slew/filter/flip sequencing and adds precomputed *contingency ladders* so a
clouded-out node degrades to its next-best deterministic plan without a
round-trip to the cloud.

The tuning system is elevated from "Claude nudges 30 weights" to a three-ring
loop: **Ring 0** — closed-form, LLM-free nightly calibrations (forecast
reliability per site, predicted-vs-realized σ per node, class hazard rates);
**Ring 1** — the existing Claude advisory loop over a smaller set of genuinely
judgment-shaped hyperparameters, now gated by **deterministic counterfactual
backtests** before any change is applied; **Ring 2** — weekly structural
proposals (new class strategy templates) expressed as declarative data,
schema-validated and backtested, never code.

The hot path remains 100% procedural and deterministic. Inputs and outputs are
unchanged: same targets/nodes/measurements/conditions tables in, same
`ObservationPlan`/`PlanItem` JSON out, same `scores`, `plans`, `plan_runs`,
`tuning_state`, `weight_history` tables (extended, never broken).

---

## 1. Governing principles

**P1 — Value is information, not score.** The unit of account is *expected
utility-weighted information delivered to science*: how much better we know
the things the target's class makes worth knowing (an eclipse timing, a transit
depth, an outburst onset, a decline rate). A number on a 0–1 scale that blends
six heuristics cannot express "the second observation of this transit egress by
a node under 60% cloud is worth 0.31 of the first"; an expectation over an
explicit information functional can.

**P2 — Uncertainty is load-bearing, not noise.** Weather, node behavior, and
measurement quality are random variables with estimable distributions. The
planner optimizes the *expectation of realized* value, which means it natively
overprovisions fragile critical events, underprovisions safe routine ones, and
values a 70%-reliable node at 70% — with variance-aware exploration bonuses
for nodes we haven't measured yet.

**P3 — Hardware is physics, not a tier label.** Every telescope's worth for a
given observation is computed from its optics, sensor, and site through an SNR
model anchored on the Seestar S50 (the same anchoring discipline as
`telescope_specs.derive_params`). Heterogeneity stops being a complication and
becomes the network's principal asset: the fleet self-organizes into roles.

**P4 — Reliability is measured where it acts.** Autodetected performance
enters as (a) probability the data arrives, (b) probability it is accepted,
(c) how much noisier it is than physics predicts. Never as a generic score
multiplier. Crucially, precision skill is judged against the node's *own
physics prediction* — a 50 mm scope delivering 0.05 mag where physics says
0.04 is a good performer; a 200 mm cooled scope delivering 0.05 where physics
says 0.01 is a problem. The current `mean_uncertainty` term cannot see this.

**P5 — Coordination must be emergent.** If a desirable fleet behavior needs a
dedicated knob, the objective is wrong. CHORUS's only coordination "mechanism"
is that all nodes draw down the same per-target residual-value ledgers.

**P6 — Plan for the night you'll get, not the night you booked.** Plans carry
their own contingencies. The cloud plans a policy (shallow, precomputed), not
merely a schedule.

**P7 — Deterministic core, evidence-driven periphery.** No LLM, no wall-clock
nondeterminism, no unseeded randomness at decision time. All learning happens
in offline loops that update *parameters and declarative state* read by the
deterministic core on its next cycle — exactly the contract `tuning.py`
established, generalized.

---

## 2. Mathematical foundation

### 2.1 Information cells

Each active target *t* owns a finite set of **information cells**
U_t = {u₁ … u_m}. A cell is the atomic "thing worth knowing," with:

- a **locus** x_u — a time interval, a phase interval (for periodic targets),
  an event sub-window (ingress/egress/mid for transits), and optionally a
  filter band;
- a **value density** ν_u ≥ 0 — the science utility of fully capturing this
  cell tonight, set by the class template (§5) and modulated by campaign
  state and scarcity (§2.5);
- a **precision requirement** σ_ref(u) — the photometric error at which the
  cell's science is essentially achieved (e.g. 0.01 mag to resolve a 12 ppt
  transit; 0.1 mag to detect a CV outburst).

Cells are the *replacement for the composite score's coverage, cadence,
time-criticality, and science terms simultaneously*: a target's total
schedulable value tonight is Σ ν_u, and its structure encodes *when* and *how
well* it needs observing.

### 2.2 Capture coefficients

A candidate observation *o* (node n, start slot s, exposure plan e) captures a
fraction of each cell it touches:

```
ρ(u, o) = g(σ_o, u) · k_t(x_u, x_o) ∈ [0, 1]

g(σ, u)  = σ_ref(u)² / (σ_ref(u)² + σ²)          precision saturation
k_t(·,·) = class-specific proximity kernel        temporal/phase locality
```

- **g** is the fraction of the cell's information a single measurement of
  variance σ² extracts (the exact Gaussian posterior variance reduction for a
  one-dimensional cell state with prior variance σ_ref²). A measurement at
  σ = σ_ref captures 50%; at σ_ref/3 captures 90%; at 3σ_ref, 10%. This is
  where telescope hardware bites, continuously.
- **k_t** is 1 when the observation sits inside the cell's locus and decays
  with distance in time (aperiodic classes) or phase (periodic classes), with
  class-tuned length scales (Ring-1 hyperparameters). A time-series
  observation (transit dwell) touches every cell its span covers.

### 2.3 Delivery probability

Each candidate observation succeeds — produces an accepted measurement —
with probability

```
p_o = p_sky(n, s..s+need)          calibrated per-slot clear probability (§4.3)
    × p_exec(n)                    node executes & delivers, given clear sky
    × p_accept(n)                  delivered data passes QC / AAVSO / cross-val
```

p_exec and p_accept are posterior means of per-node Beta distributions
maintained by the reliability ledger (§4.1). Conditional on success, the
measurement's effective variance is the physics prediction inflated by the
node's measured efficiency: σ_o² = κ_n · σ_phys²(n, t, s, e).

### 2.4 The objective

Let A be a set of placed observations. Define per cell the **survival**
(probability-weighted fraction of the cell's value still uncaptured):

```
r_u(A) = Π_{o ∈ A_t(u)} (1 − p_o · ρ(u, o))
```

The network objective is expected captured value plus an exploration term,
minus nothing (feasibility, occupancy, and overhead are constraints, handled
in generation and sequencing):

```
Φ(A) = Σ_t Σ_{u ∈ U_t} ν_u · (1 − r_u(A))   +   β · Σ_{o ∈ A} ξ(o)
```

ξ(o) is the modular **exploration bonus** — the value of what the observation
teaches us about the *node* (§4.2). The expectation over the independent
Bernoulli success indicators is exact and closed-form because of the product
structure: no sampling, no simulation, fully deterministic.

**Theorem (why the solver is principled).** Each term
ν_u·(1 − Π(1 − p_o ρ_{u,o})) is a monotone submodular function of A (it is a
weighted probabilistic-coverage function), and ξ is modular; hence Φ is
monotone submodular. Greedy maximization subject to the interval-scheduling
and per-node capacity constraints inherits the classical near-optimality
guarantees of submodular maximization over independence systems, and *lazy*
greedy evaluation is exact because marginal gains only shrink as A grows.

**Marginal value of a placement** — the quantity the solver ranks — is:

```
Δ(o | A) = Σ_{u touched by o} ν_u · r_u(A) · p_o · ρ(u, o)   +   β·ξ(o)
```

Read it aloud and every current mechanism falls out: value of the cells it
touches (science, brightness feasibility via ρ→g, urgency via ν), times what's
*left* of them (redundancy, cadence, diversity — all through r_u), times the
chance it actually happens (weather, reliability), times how well this
hardware captures them (specs). One formula, no weight vector.

### 2.5 Scarcity: pricing tonight against the future

The composite score's `coverage_gap`/`time_criticality` look backward (how
neglected is it? how old is the alert?). CHORUS prices cells *forward*:

```
ν_u = ν_u^raw · S_u,     S_u = 1 / (1 + Σ_{d=1..H} γ^d · q_u(d))
```

q_u(d) is the probability the network could capture cell u on future night d —
computed once per target per day by a cheap deterministic sweep: visibility of
the target from each capable node on night d (analytic altitude), times the
node's *climatological* clear probability for that month, times its p_exec.
γ ∈ (0,1) is the horizon discount (Ring-1). H ≈ 45 nights.

Consequences, with no special-casing:

- A transit whose next visible event is in 43 days gets S ≈ 1 → full value
  tonight. One recurring nightly → S small → it competes fairly.
- A circumpolar LPV with 90 future chances is nearly free to skip — it becomes
  the natural filler and *training target* for new nodes.
- A target approaching the end of its observing season (solar conjunction)
  automatically heats up as q_u(d) collapses — behavior the current system
  simply does not have.
- Fresh transients get urgency not from an age-decay curve but from ν^raw set
  by the class template (early-lightcurve cells are intrinsically the
  valuable ones) times S ≈ 1 (those cells never recur).

---

## 3. The measurement model — hardware as physics

`sigma_phys(node, target, slot, exposure_plan)` is the load-bearing function
that makes telescope differentiation first-class. All inputs already exist in
the nodes table / `telescope_specs` (aperture, focal length, pixel scale, FoV,
sensor, gain, read noise, cooling, max_exposure_s, filter set, mpsas, tier).

```
Signal (per sub, electrons):
  N_e = t_sub · (π/4) D² · η_sys · Φ_0(band) · 10^(−0.4·(m + k_ext·(X−1)))
        D = aperture_mm, X = airmass(slot), k_ext per band (Ring-0 calibrated
        per site from the node's own frames; default 0.25 mag/airmass V)

Noise (per sub, electrons²), over the PSF aperture of n_pix pixels
  (n_pix from fwhm_fallback_px / measured FWHM and pixel_scale):
  σ_shot²  = N_e
  σ_sky²   = n_pix · sky_e(mpsas_eff, pixel_scale², t_sub)
             mpsas_eff = site mpsas darkened/brightened by moon model at slot
  σ_read²  = n_pix · read_noise²
  σ_dark²  = n_pix · dark_rate(cooled, month_climate_temp) · t_sub
  σ_scint² = (0.09 · D_cm^(−2/3) · X^1.75 · e^(−h/8000) / √(2 t_sub))² · N_e²

Instrumental (stack of n_sub subs, capped by max_exposure_s):
  σ_inst(mag) = 1.0857 · sqrt(Σ noise²) / N_e / √(n_sub)

Ensemble floor (this is where FoV earns its keep):
  N_comp = expected usable comparison stars = Λ(l, b, m_bright..m_faint) · FoV_eff
           Λ from a small static star-density grid by galactic latitude;
           m range from the node's own mag_bright_limit..mag_faint_limit
  σ_ens  = median_comp_σ_inst / √(max(1, N_comp))

Systematic floor:
  σ_sys  = flat-field/rotation residual term: base_sys(tier) · rot_penalty(alt_az, dec, t_dwell)

Total:  σ_phys² = σ_inst² + σ_ens² + σ_sys²
Effective (node-adjusted):  σ_o² = κ_n · σ_phys²
```

Every branch of this model is a hardware differentiator the current system
ignores or flattens to `telescope_match ∈ {0.35, 0.7, 1.0}`:

- **Aperture & cooling** move σ_inst by orders of magnitude at the faint end —
  a 200 mm cooled scope at mag 16.5 might predict 0.008 mag where a Seestar
  predicts 0.25. Under g(σ,u) with σ_ref = 0.05, that is capture 0.975 vs 0.038:
  a 25× value ratio, versus the current system's 1.0-vs-0.35 heuristic.
- **FoV** stops being a "+0.1 for wide field" bonus: a 1.27° Seestar in the
  galactic plane finds 40 comp stars (σ_ens negligible); a 0.3° f/10 SCT finds
  2 (σ_ens dominates) — so bright-star ensemble photometry *correctly* routes
  to smartscopes even when the SCT has more aperture.
- **Mount & max exposure**: alt-az rotation caps t_sub, raising read-noise
  share on faint targets; equatorial mounts unlock long subs and get valued
  for it — plus their meridian-flip cost still lives in sequencing.
- **Filters**: cells can be band-specific (§5); only nodes whose filter_set
  covers the band see nonzero ρ. A BVR-equipped tier-2 node is the only voice
  that can sing the color part — it stops wasting its filters on CV-band work
  automatically whenever color cells exist.
- **Saturation**: the bright limit stays a hard feasibility gate (ρ = 0), as
  today, computed from mag_bright_limit per derive_params.

### 3.1 Exposure as an optimization, not a lookup

`choose_exposure`'s four mag buckets become:

```
best_plan = argmax over e ∈ candidate_exposure_plans(node, target)
            Δ(o_e | A) / occupied_minutes(e)
```

with candidates generated deterministically: t_sub ∈ {5, 10, 15, 30, …,
max_exposure_s}, total dwell ∈ {5, 8, 12, 20, 35} min (plus the class
template's required dwell for time-series cells). The optimizer naturally
gives faint targets on small scopes *longer* dwells only up to the point where
σ crosses the g(σ) knee — beyond it the marginal information per minute
collapses and the slot is better spent elsewhere. Today the system spends a
fixed 20 minutes on any mag>13 target regardless of whether that yields
σ = 0.03 or hopeless 0.4.

---

## 4. Reliability: from a scalar to a ledger

### 4.1 The node reliability vector (autodetected, conjugate, deterministic)

`refresh_node_performance` currently compresses everything into
`reliability_score` and multiplies the composite by (0.5 + 0.5·trust). CHORUS
keeps the *same evidence sources* (measurements, AAVSO acceptance,
cross-validation outliers, incidents with the existing system/environmental/
node attribution from `incidents.py`) but maintains four conjugate posteriors
per node, updated nightly by counting — no optimizer, no LLM:

```
p_exec   ~ Beta(a_e, b_e)   success = plan item attempted on a clear night → data delivered
                            (weather- and system-attributed failures excluded — the
                            incident classifier already provides attribution)
p_accept ~ Beta(a_a, b_a)   success = delivered → aavso_submitted, non-outlier, good/acceptable
θ_out    ~ Beta(a_o, b_o)   cross-validation outlier rate (feeds p_accept and alerts)
κ        ~ shrunk ratio     mean((σ_delivered / σ_phys_predicted)²) over recent accepted
                            measurements, shrunk toward 1: κ = (m₀·1 + Σ ratios)/(m₀ + n)
```

Priors are Beta(3,3)-class — the principled version of "start at 0.5" — and
every count decays with a ~60-night half-life so nodes can redeem themselves
and regressions surface quickly. Incident classes map to targeted pseudo-count
penalties on the component they implicate (mount failures → p_exec; hot-pixel
plagues → κ; miscalibration → θ_out), replacing today's single
`incident_penalty` multiplier.

The legacy `reliability_score` and `scheduler_trust_score` columns are still
written (a fixed monotone map of the vector: trust =
E[p_exec]·E[p_accept]·min(1, κ^(−½))) so every existing UI, API, and member
app surface keeps working — but the planner never reads the scalar again.

**Why κ is the deep upgrade:** it is *hardware-normalized skill*. Today's
`precision_factor = 1 − mean_unc/0.30` punishes small telescopes for physics
and forgives big ones for sloppiness. κ compares each node to its own physics
prediction, so the planner learns "this Seestar is running at its limit"
(κ ≈ 1.05, trust its σ) vs "this C8 has collimation or flat problems"
(κ ≈ 6, treat its 0.01 predictions as 0.025 and route the truly demanding
cells elsewhere) — and κ feeds *back into σ_o and therefore into ρ*, so
degraded hardware loses exactly the assignments its degradation invalidates,
nothing more.

### 4.2 Exploration: scheduling as experiment design on the fleet itself

A new node's Beta posteriors are wide. Information about the node is worth
real future value, so each placement carries a modular bonus:

```
ξ(o) = w_e·sd(p_exec) + w_a·sd(p_accept) + w_κ·sd_κ(n) · demand_match(o)
```

sd(·) is the posterior standard deviation (closed form for Beta);
demand_match up-weights assignments that actually exercise the uncertain
component (a faint target teaches you about κ at the faint end; any target
teaches p_exec). β and the w's are Ring-1 hyperparameters with tight bounds.

Combined with scarcity (§2.5), this produces the correct onboarding behavior
*emergently*: new nodes get real but low-regret work — abundant cells
(circumpolar LPVs, bright well-covered EBs) where a failure costs little,
sized to shrink their posteriors fast. Today a new node at trust 0.5 just gets
a uniformly 0.75-damped copy of everyone's target list. Within ~10 nights the
posteriors tighten and the node graduates to whatever its physics says it
deserves — a deterministic, self-executing probation that no one configured.

### 4.3 Weather as a calibrated probability, not a factor

`weather_factor` (night-mean of forecast blends) and per-slot cloud fractions
already exist in `conditions.py`. CHORUS keeps the same 7timer/Open-Meteo
inputs but treats them as *raw scores to be calibrated per site* (Ring 0):

```
p_sky(site, slot) = logistic(a_site + b_site · logit(forecast_clear(slot)))
```

a_site, b_site fit nightly by counting realized outcomes (did scheduled slots
at this site produce frames?) against forecast — closed-form logistic
regression on two parameters, per site, LLM-free. Sites where the forecast
chronically lies (coastal microclimates, mountain nodes) get honest
probabilities within a couple of weeks. Missing forecast → monthly
climatological prior for the site (also maintained by counting).

Because p_sky is *inside* p_o, weather uncertainty propagates into every
marginal value: a 0.95-value observation under p_sky = 0.4 is worth 0.38, and
— this is the qualitative leap — **the residual r_u stays at 0.62 after
placing it, so a second node under independent sky still sees 62% of the
cell's value.** Cross-site weather redundancy on critical events is not a
feature; it is arithmetic.

(Within-site slot-to-slot correlation is handled conservatively: multiple
placements of the same target at one site share a per-night site random factor
approximated by capping the per-site product term — one line in the survival
update, noted in the pseudocode.)

---

## 5. Class strategy templates — cells per science type

Templates are **declarative data** (rows in `class_templates`, seeded from
config, editable by Ring 2 with validation), not code. The deterministic cell
compiler (§6, T0) reads them. Illustrative templates:

**EB (eclipsing binaries)** — locus space = phase.
32 phase bins from the ephemeris (period, epoch from the target row). ν^raw
concentrated on: primary/secondary eclipse bins (timing science, σ_ref
0.02–0.03), quadrature bins for the O'Connell effect, uniform low floor
elsewhere. The *phase-coverage ledger persists across nights*: bins captured
this season stay discounted (their r_u carries over as a stored
`phase_coverage` vector, refreshed by T0 from actual accepted measurements).
An EB observed every night at the same local sidereal time saturates the same
bins; CHORUS sees zero marginal value there and shifts the sample 3 h, or to a
node 90° west — the exact failure mode of the current time-gap
`cadence_bonus`, which is blind to phase.

**EXOPLANET (transits)** — locus = event sub-windows.
Cells: pre-ingress baseline (≥45 min, σ_ref from depth/3), ingress, mid,
egress, post-egress baseline. ν^raw ∝ depth-normalized timing value ×
scarcity (next-visible-event sweep makes rare windows precious). Baseline
cells make the planner *pay for out-of-transit coverage* — today baselines
exist only implicitly inside the fixed obs window. Partial coverage by
different nodes composes correctly: node A takes ingress+mid, node B (200 km
east under clearer sky) takes egress — the current pinned-slot,
all-or-nothing transit opportunity cannot express this; CHORUS emits
per-sub-window time-series items.

**CV (cataclysmic variables)** — locus = time, value = hazard.
Quiescent state: one detection-quality cell (σ_ref ~ 0.1 — small scopes fully
capture it) per hazard interval, ν^raw ∝ P(state change since last accepted
sample) = 1 − e^(−λ_cv·Δt), with λ_cv per target estimated by Ring 0 from that
target's/class's own outburst history. Outburst state (set by T0 when a
measurement or external alert crosses the threshold): template swaps to dense
time cells with tight σ_ref, ν^raw high, multi-night — an automatic campaign
escalation. Today a quiescent faint CV soaks big-scope time nightly via
`coverage_gap`; under CHORUS its quiescent cell is cheap, abundant
(S_u small), fully capturable by a Seestar — and the moment it brightens the
value landscape reshapes itself before the next planning cycle.

**SN / transient** — locus = time + band.
Early cells (rise/peak/early decline) carry ν^raw far above late-decline
cells; age enters through which cells still exist, not an exp(−age/12) fudge.
Band-specific cells (B, V, R) exist only when any node in the network can
capture them — the presence of one filtered tier-2 node changes the *global*
cell set, and that node alone sees ρ > 0 on color cells: hardware-driven role
assignment with zero routing rules.

**LPV / Mira** — sparse time cells, wide σ_ref, huge future opportunity count
→ tiny S_u: the network's ballast and its nursery for new nodes.

Adding a science program = adding a template row. No planner change.

---

## 6. Architecture and execution flow

Four deterministic tiers, replacing score_all → build_opportunities →
assign_network → sequence_node one-for-one:

```
T0 LEDGER   (continuous + nightly)  state update from evidence
T1 HORIZON  (daily, per target)     scarcity sweep, cell compilation
T2 SCORE    (per planning cycle)    fleet-wide stochastic-coverage assignment
T3 PERFORM  (per node)              sequencing + contingency ladders → plans
```

### T0 — Ledger (replaces/extends performance refresh + adds target state)

Inputs: new measurements, incidents, AAVSO results, weather realizations.
Outputs (all plain rows, conjugate/counting updates only):

- `node_ledger`: Beta counts for p_exec/p_accept/θ_out, κ ratio stats,
  per-site weather calibration (a, b), per-band extinction k_ext, monthly
  climatology. Legacy reliability columns mirrored.
- `target_state`: per-target captured-cell residuals that persist across
  nights (EB phase-coverage vector, CV hazard clock + state flag, transient
  age/segment, last-accepted-σ per band).
- Triggers: alert ingestion or an own-network detection flips template state
  and enqueues an incremental T2 repair.

### T1 — Horizon (new; the multi-night brain)

For each active target, a deterministic sweep over the next H=45 nights ×
capable nodes: analytic visibility, climatological p_sky, p_exec → q_u(d) →
scarcity S_u; compile the class template + target_state into tonight's cell
list U_t with final ν_u and σ_ref(u). Cost: O(targets × H × nodes) of pure
trig and lookups, run once per day, cached in `target_cells`.

This tier is also where **campaign commitments** live: a multi-night campaign
(e.g., 10 consecutive nights of an EB season finale, or a TOM-requested
transit chain) is just a set of pre-priced future cells; T1 raises tonight's ν
for cells whose future q is low *given the campaign deadline*. No separate
campaign scheduler.

### T2 — Score (replaces build_opportunities + assign_network)

**Opportunity generation** per node (parallel to today's Stage A): dark
window, alt/az curve, horizon mask, per-slot p_sky, per-slot σ_phys for each
(target, exposure-candidate), feasibility (bright limit, need ≤ slots). Each
opportunity knows which cells it touches with which ρ. Transit sub-windows
generate pinned time-series opportunities per sub-window.

**Assignment — lazy cost-benefit greedy on Φ:**

```
1. Prime a max-heap with optimistic Δ̂(o) for every opportunity
   (all r_u = 1, best slot, best exposure plan).
2. Pop o; recompute Δ(o | A) exactly against current residuals r_u
   (O(#cells touched)); if still ≥ next heap key → commit
   (lazy evaluation is exact under submodularity), update r_u ledgers,
   node occupancy, capacity; else re-key and push back.
3. Stop when Δ_best < ε or occupancy/capacity exhausted.
4. Deterministic seeded local search (same budget contract as today's
   local_search_ms): relocate / swap / drop-add moves accepted on exact
   Φ recomputation — kept because interval-packing constraints can strand
   value the greedy can't see; seeded RNG (as today) preserves determinism.
```

Per-target *portfolio caps* (max expected redundancy per cell, floor on
distinct targets per night) are independence-system constraints, not value
hacks — they replace `max_targets_per_night` semantics and prevent degenerate
all-in behavior under extreme ν.

**Compatibility writes:** for every (target, node) the best single-placement
Δ normalized to 0–1 is written to `scores.total` with a components breakdown
(expected_info, p_deliver, sigma_pred, top cells, scarcity) — so every
existing scores-reading surface (`server.py`, member app explanations,
`min_score` gates) keeps functioning, with strictly more honest numbers.

### T3 — Perform (extends sequence_node)

Sequencing keeps the current machinery — NN + 2-opt ordering,
`transition_overhead_seconds` slew/filter/flip model, meridian-side logic —
with overhead now also *charged back*: a placement whose realized overhead
eats into a neighboring cell's dwell gets its marginal value re-checked, and
the local search fixes orderings the greedy packed badly.

**Contingency ladders (new):** for each node, T3 precomputes a shallow policy:

- For each plan item, up to 2 **alternates** — the next-best placements from
  the *same* solved state (cheap: they're in the heap) that use the same or
  later slots, tagged with trigger conditions ("if sky closed until slot s",
  "if target unsolvable/saturated").
- A **late-start ladder**: the best truncated plan if observing begins at
  slot s, for s in {+1h, +2h, +3h} — a handful of extra greedy runs against
  frozen fleet residuals (other nodes' commitments held fixed), so a node that
  opens late executes a plan that is still globally coherent.

Emitted inside the existing plan JSON as an additive `contingencies` object —
old node agents ignore unknown keys (plans are parsed permissively), upgraded
agents execute the ladder locally without a cloud round-trip. `PlanItem`
shape, `plans` table, and `_save_plan` semantics are unchanged.

**Incremental repair:** alerts, a node dropping offline, or a hard forecast
change do not trigger a full re-solve. The affected placements are removed,
their r_u restored, and the greedy resumes from the heap — O(affected), and
because the objective is submodular the repaired solution retains its
guarantee relative to the surviving commitments.

### Persistence & telemetry

`plan_runs` gains: expected_info (Φ), expected_deliveries (Σp_o),
per-class Φ breakdown, exploration share, scarcity-weighted coverage. T0
later joins each run against what actually happened → the realized-vs-expected
ledger that powers Ring 1 (§7).

---

## 7. The tuning system, elevated: three rings around a deterministic core

The current `tuning.py` contract — procedural evidence, one advisory Claude
call, trust-region clamp, audit history, live DB read by the hot path — is
kept intact and generalized. What changes is *what* is tuned and *how changes
are validated*.

**Ring 0 — closed-form calibration (nightly, LLM-free, new).**
Everything that has a statistical estimator gets one, and stops being an LLM
knob: per-site forecast calibration (a,b), per-node κ, per-node/band
extinction and zero-point drift, class hazard rates λ, climatology tables.
Pure counting/least-squares in T0. Deterministic, auditable, no trust region
needed beyond sample-size gates. (Today the Claude monitor spends its delta
budget nudging `weather` weights to compensate for miscalibrated forecasts —
Ring 0 removes that entire error term at the source.)

**Ring 1 — LLM-advised hyperparameters (nightly, evolved from today's loop).**
The parameter surface shrinks from ~31 heuristic weights to ~15 genuinely
judgment-shaped hyperparameters, each with meaning and bounds:

```
kernel length scales per class        phase/time locality of information
class value scales (ν^raw multipliers) relative program priorities
scarcity discount γ, horizon H        how much tonight defers to the future
exploration β, w_e/w_a/w_κ            fleet-learning appetite
survival site-correlation cap         weather-redundancy conservatism
portfolio caps, ε stop threshold      breadth vs depth
sequencing overhead scalars           (unchanged from coordination group)
```

Same `tuning_state`/`weight_history` machinery, same trust-region clamps,
same admin notifications and rollback. Two upgrades:

1. **Evidence brief v2** — per-mechanism diagnostics instead of per-weight
   splits: calibration curves (predicted p_o deciles vs realized delivery),
   predicted-vs-realized σ by node class, expected-vs-realized Φ by target
   class, exploration ROI (posterior-variance reduction per exploration
   minute), scarcity regret (value of cells that expired uncaptured).
2. **Backtest gate (the decisive change):** because the planner is
   deterministic and every night's opportunity inputs are archived, any
   proposed θ′ is replayed over the last 14 nights' *actual* inputs, and the
   resulting plans are scored against *realized* outcomes (which slots were
   really clear, which nodes really delivered, what σ was really achieved) via
   the realized-coverage functional. θ′ is applied only if backtested realized
   Φ ≥ current θ's. The LLM proposes; arithmetic disposes. Today a plausible
   but wrong rationale ships at max_delta and takes nights to detect; under
   CHORUS it never ships.

**Ring 2 — structural evolution (weekly, LLM-advised, new but bounded).**
Claude may propose *new declarative artifacts*: a class template revision
(different cell layout for CVs), a new exposure-candidate family, a campaign
definition. All are data validated against schemas, backtested like Ring 1,
trust-staged (advisory → shadow-scored → live), and reversible via the same
audit table. The deterministic core never changes shape; its vocabulary grows.

---

## 8. Reference pseudocode

Compact but implementation-faithful; names map to intended modules
(`cloud/chorus/` package: `cells.py`, `physics.py`, `ledger.py`,
`horizon.py`, `assign.py`, `perform.py`).

```python
# ── data structures ─────────────────────────────────────────────────────────

@dataclass
class InfoCell:
    cell_id: str            # f"{target_id}:{kind}:{index}"
    target_id: str
    kind: str               # "time" | "phase" | "event" | "band"
    locus: Locus            # time interval | phase interval (+ band)
    nu: float               # final value density (raw × scarcity × state)
    sigma_ref: float        # mag precision at which cell science saturates
    residual: float = 1.0   # r_u — persisted for cross-night kinds (phase, campaign)

@dataclass
class Opportunity:          # generated per (node, target, exposure_plan)
    node_id: str; target_id: str
    slots: dict[int, SlotEval]      # start-slot → SlotEval
    need: int                       # occupancy slots
    exposure: ExposurePlan          # t_sub, n_sub, dwell_min, filter
    p_exec: float; p_accept: float  # node ledger means
    touches: dict[str, float]       # cell_id → rho at best slot (recomputed per slot)

@dataclass
class SlotEval:
    p_sky: float            # calibrated clear probability across the dwell
    sigma: float            # kappa_n · sigma_phys at this slot
    az: float               # for meridian logic (unchanged)

# ── physics (T2 generation) ─────────────────────────────────────────────────

def sigma_phys(node, target, slot_ctx, exp):        # §3, closed form
    Ne    = signal_electrons(node, target.mag, exp.t_sub, slot_ctx.airmass)
    var   = Ne + sky_var(node, slot_ctx) + read_var(node) \
              + dark_var(node, exp.t_sub) + scint_var(node, slot_ctx, exp.t_sub, Ne)
    inst  = 1.0857 * sqrt(var) / Ne / sqrt(exp.n_sub)
    ens   = ensemble_floor(node.fov_deg, target.gal_lat,
                           node.mag_bright_limit, node.mag_faint_limit, inst)
    return sqrt(inst**2 + ens**2 + sys_floor(node, exp)**2)

def rho(cell, opp, slot):                            # §2.2
    g = cell.sigma_ref**2 / (cell.sigma_ref**2 + opp.slots[slot].sigma**2)
    return g * kernel(cell, opp, slot)               # class kernel: time/phase/band

# ── assignment (T2) ─────────────────────────────────────────────────────────

def marginal(opp, slot, R, ledger):                  # Δ(o | A); O(#touched cells)
    p = opp.slots[slot].p_sky * opp.p_exec * opp.p_accept
    p = site_correlation_cap(p, opp, R)              # §4.3 conservatism, one line
    gain = sum(cell.nu * R[cell.cell_id] * p * rho(cell, opp, slot)
               for cell in cells_touched(opp, slot))
    return gain + BETA * exploration_bonus(opp, ledger)

def assign(contexts, opportunities, cells, params, seed):
    R = {c.cell_id: c.residual for c in cells}       # residual ledger (persists T0 state)
    occ, count = init_occupancy(contexts), defaultdict(int)
    heap = MaxHeap((optimistic_delta(o), o) for o in opportunities)
    A = []
    while heap:
        key, o = heap.pop()
        slot, d = best_slot(o, occ, lambda s: marginal(o, s, R, ledger))
        if slot is None or d < EPSILON: continue
        if d + 1e-12 < heap.peek_key():              # lazy re-key (exact: submodular)
            heap.push(d, o); continue
        if count[o.node_id] >= contexts[o.node_id].cap: continue
        commit(A, o, slot, occ, count)
        for cell in cells_touched(o, slot):          # draw down the shared ledger —
            p = effective_p(o, slot, R)              # this IS the coordination
            R[cell.cell_id] *= (1.0 - p * rho(cell, o, slot))
    A = local_search(A, R, opportunities, budget_ms=params.local_search_ms,
                     rng=Random(seed))               # seeded: deterministic, as today
    return A, R

# ── exposure selection (inside opportunity generation) ─────────────────────

def best_exposure(node, target, slot_ctx, cells, R):
    return max(candidate_plans(node, target),
               key=lambda e: marginal_for(e, slot_ctx, cells, R)
                             / (e.dwell_min + SLEW_RESERVE_MIN))

# ── nightly ledger update (T0) ──────────────────────────────────────────────

def update_node_ledger(node_id):
    ev = attempts_and_outcomes(node_id, half_life_nights=60)   # incident-attributed
    beta_update("p_exec",   ev.clear_attempts, ev.delivered)
    beta_update("p_accept", ev.delivered,      ev.accepted)
    beta_update("theta_out",ev.validated,      ev.outliers)
    kappa = shrink_toward_1(mean((m.sigma / m.sigma_predicted)**2
                                 for m in ev.accepted_meas), m0=8)
    mirror_legacy_columns(node_id)               # reliability_score / trust for UI

def calibrate_site_weather(site):                # Ring 0, closed form
    pairs = [(logit(f), realized) for f, realized in forecast_outcomes(site, 30)]
    a, b = two_param_logistic_fit(pairs)         # deterministic IRLS, few iterations
    store(site, a, b)

# ── horizon sweep (T1) ──────────────────────────────────────────────────────

def scarcity(target, cell, nodes, H=45, gamma=0.93):
    q = sum(gamma**d * max_over_nodes(
              visible(target, node, night=d) * climatology_p_sky(node, d)
              * node.p_exec_mean for node in nodes)
            for d in range(1, H+1))
    return 1.0 / (1.0 + q)

# ── tuning backtest gate (Ring 1) ───────────────────────────────────────────

def backtest(theta_prime, nights=14):
    total = {"cur": 0.0, "new": 0.0}
    for night in archived_inputs(nights):        # frozen opportunities + realizations
        for tag, th in (("cur", active_theta()), ("new", theta_prime)):
            plan, _ = assign(*rebuild(night, th), seed=night.seed)
            total[tag] += realized_coverage(plan, night.realized)   # actual clear/deliver/σ
    return total["new"] >= total["cur"]          # apply θ′ only if it wins on reality
```

---

## 9. How decisions change — five realistic nights

**(1) A shallow transit under a 55%-cloud forecast.**
*Today:* the transit is a pinned all-or-nothing opportunity at one node;
`redundancy_decay = 0.55` makes a second node's copy worth 55% before diversity
recovery, usually losing to fresh targets — one node is booked, and a
night-average `weather_factor` has already diluted the pick. Expected yield:
≈ 0.5 of a transit.
*CHORUS:* p_sky = 0.52 at node A leaves r = 0.48 on every transit cell after
booking A — node B (independent sky, 300 km away, p_sky 0.7) sees 48% of full
value and is booked; a third node grabs just the egress cell where A's
forecast dips. Expected event coverage rises from ~0.5 to ~0.86, and the
sub-window cells mean partial captures still compose into one usable light
curve.

**(2) A 200 mm cooled Newtonian joins a fleet of Seestars.**
*Today:* trust 0.5 → ×0.75 damping on the same composite everyone gets;
`telescope_match` gives it 1.0 on almost everything; it spends its first weeks
duplicating bright EBs the Seestars already saturate.
*CHORUS:* physics says σ(16.8 mag) ≈ 0.012 vs Seestar 0.3 → on faint neglected
CV cells its capture is ~0.95 vs ~0.03: it is the *only* node with material
marginal value there. Wide posteriors add exploration bonus, so its first
nights mix faint-end probes with abundant safe cells; ten nights later κ and
p_exec have tightened and it holds the faint portfolio outright — while
*losing* the bright-ensemble work to Seestars, whose 1.27° fields carry 20×
its comparison stars. Role differentiation, from arithmetic.

**(3) The EB whose secondary eclipse is never seen.**
*Today:* nightly observations at the same local sidereal time satisfy
`coverage_gap` and `cadence_bonus` (time gaps look perfect) while sampling the
same phases for a month.
*CHORUS:* the persistent phase ledger shows bins 0.48–0.55 (secondary) with
r = 1.0 while sampled bins sit at r ≈ 0.05. Marginal value tonight is
concentrated in a 40-minute window at 03:40 local — or at 22:10 for a node
90° west. The planner moves the sample or hands it across the ocean; the
secondary is captured within the week.

**(4) A quiescent faint CV vs the fleet's best hours.**
*Today:* faint mag pushes it to 20-minute dwells; neglect keeps re-raising its
score; big-scope prime time bleeds into a target that is doing nothing.
*CHORUS:* its quiescent template is a single 0.1-mag-σ_ref detection cell
whose ν grows with hazard 1 − e^(−λΔt): one cheap Seestar sample every second
night captures ~everything. The night a sample comes back 2.1 mag bright, T0
flips the template; by the next planning cycle it is a dense multi-longitude
time-series campaign, ingress-to-decay, with the cooled node on the faint
recovery tail.

**(5) A forecast that is always wrong.**
*Today:* a coastal node's optimistic 7timer feed inflates its scores; failures
depress `reliability_score`, punishing the *node* for the *forecaster*; the
Claude monitor eventually nudges the global `weather` weight, degrading
everyone.
*CHORUS:* Ring 0's per-site calibration maps "70% clear" at that site to
p_sky = 0.43 within two weeks; the incident classifier keeps weather failures
out of p_exec, so the node's reliability stays honest; and the marginal-value
math routes its bookings toward its genuinely reliable pre-midnight hours,
with cross-site backups on anything critical. No global weight moved; nothing
else got worse.

---

## 10. Guarantees, budgets, and failure modes

- **Determinism:** same inputs (DB rows, cached forecasts, seed) → same plans.
  All randomness is the seeded local-search RNG, as in the current annealer.
  No LLM anywhere in T0–T3 or Ring 0.
- **Complexity:** cells/target ≤ 64; opportunities ≈ nodes × 60 (unchanged
  query shape); marginal eval O(cells touched) with the residual ledger; lazy
  greedy ≈ O(N_opp log N_opp) pops in practice; T1 sweep O(targets × 45 ×
  nodes) of trig, daily. Comfortably inside the current planning cadence at
  10³ nodes × 10³ targets.
- **Approximation quality:** Φ is monotone submodular → lazy greedy is exact
  greedy; near-optimality per submodular-maximization theory over the
  packing constraints; the seeded local search only improves, never regresses
  (same "never worse than greedy" contract as today's annealer).
- **Cold starts and gaps:** unknown specs → Seestar-anchored defaults from
  `derive_params`; no forecast → site climatology; new target class → generic
  time-cell template; empty ledger → Beta(3,3)/κ=1 priors. Every fallback is
  the current system's behavior or better.
- **Compatibility:** `ObservationPlan`/`PlanItem` unchanged (additive
  `contingencies`, richer `explanation`); `scores` still written; legacy
  reliability columns mirrored; `tuning_state`/`weight_history` reused;
  `plan_runs` extended. The former per-node greedy packer remains archived in
  `cloud/archive/` for reference and explicit experiments.

---

## 11. What was kept, deliberately

CHORUS is a redesign, not amnesia. It keeps: the feasibility discipline of
Stage A (dark windows, horizon masks, altitude curves, bright limits); the
slew/filter/meridian overhead model and 2-opt sequencing; the incident
attribution taxonomy; the trust-region + audit + rollback machinery of
`tuning.py`; the external-coverage blend (now folded into ν via community
coverage discounting q_u); and the founding constraint that made this system
trustworthy in the first place — *the intelligence lives in the parameters
and the evidence loops, and the hot path is arithmetic anyone can audit.*
