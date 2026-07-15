# Future Program: Occultation Array

Status: **deferred design only — the current network is not occultation-ready**.

This document is a scientific and engineering readiness specification. It does
not describe an active product capability. There are no occultation scheduler
paths, prediction feeds, database records, APIs, configuration flags, timing
claims, or node runtime behavior in the current release.

## Scientific opportunity

A geographically distributed telescope network could observe an asteroid or
minor-body occulting a background star. Each site can measure whether the star
disappeared and, when it does, the disappearance and reappearance times. The
combination of positive and negative observations forms geographic chords
through the projected body. Multiple well-timed chords can constrain size,
shape, astrometry, rings, satellites, or atmospheric structure more strongly
than an ordinary image from one small telescope.

This is fundamentally different from ordinary scheduled photometry. Scientific
value depends on geographic placement, deterministic execution during a narrow
event window, sustained cadence, and independently demonstrated timing
traceability.

## Why the present network is not ready

- FITS timestamps currently depend on camera and host-system clocks and do not
  carry evidence for the camera's exposure-start latency.
- Normal 5–30 second integrations are too slow for many occultations and would
  smear disappearance and reappearance boundaries.
- Nodes do not have a certified GPS/1PPS timing path or hardware-inserted time
  evidence.
- The network does not ingest occultation predictions or calculate the moving
  geographic shadow and candidate chords.
- There is no occultation-specific high-cadence raw-sequence reduction,
  uncertainty model, negative-observation workflow, or reporting pipeline.

Ordinary heartbeat clock-skew qualification and signed offline CHORUS bundles
are not occultation-grade timing and must never be represented as such.

## Proposed timing-certified node class

A future implementation would introduce a separately certified hardware class,
not silently qualify ordinary nodes. A timing-certified node would require:

- GPS/1PPS or equivalent traceable hardware timing, preferably inserted into
  the video/frame stream rather than inferred only from host time;
- measured exposure-start, shutter, rolling-shutter, readout, buffering, and
  timestamp-placement latency for each supported acquisition mode;
- stable calibration evidence after camera, driver, firmware, cable, capture
  computer, or time-source changes;
- precisely surveyed latitude, longitude, elevation, and uncertainty;
- sufficient storage and write throughput for uninterrupted high-cadence raw
  capture;
- a lossless timing audit record containing clock source, lock state, offsets,
  dropped-frame evidence, cadence, and configuration fingerprint.

Certification would be scoped to a complete response fingerprint: node,
camera, acquisition mode, driver/firmware, time inserter, exposure mode, and
timing calibration version.

## Scheduler concept

A future prediction service would normalize externally supplied paths and
uncertainties, propagate them to the event epoch, and calculate each eligible
node's cross-track and along-track geometry. The scheduler would select a set of
geographically separated sites that samples the predicted shadow and its
uncertainty region rather than simply selecting the telescopes with the best
sky visibility.

Candidate assignments would account for:

- chord diversity and expected information gain;
- node-coordinate uncertainty and prediction uncertainty;
- star altitude, horizon, Moon, weather, aperture, and required cadence;
- the probability of positive and scientifically useful negative chords;
- pre-event baseline and post-event baseline duration;
- travel or portable-node state, if that capability is separately authorized;
- independent backup sites near high-value chord locations.

Plans would be issued early, prepoint the telescope, validate the target field,
and include a fully offline sequence around the event. A cloud replan could not
be allowed to disturb the protected capture window. No version of this concept
uses peer-to-peer scheduling or node bidding.

## Timing validation program

Before scientific use, the complete capture chain would be tested end to end:

1. Inject known PPS- or LED-derived transitions into recorded frames.
2. Compare recovered transition times with the traceable reference.
3. Repeat across cadence, exposure, gain, temperature, storage load, restart,
   clock-step, holdover, packet-loss, and dropped-frame conditions.
4. Measure systematic offset, scatter, drift, and failure-detection latency.
5. Reject any sequence whose timing lock, frame continuity, or audit trail is
   incomplete.
6. Version and retain the calibration evidence used for every reported chord.

The science program must choose its required end-to-end timing residual before
certification. This document intentionally makes no unsupported universal
millisecond threshold claim. IOTA's observing guidance emphasizes GPS-derived
timing and recording at a rate suitable for the event: [IOTA Occultation
Observing Primer](https://occultations.org/documents/OccultationObservingPrimer.pdf).

## Acquisition and local execution

The future node workflow would prepoint, acquire the correct star field, verify
the target, and begin a sustained raw sequence before the uncertainty window.
It would continue through a defined post-event baseline without depending on
cloud connectivity. Local safety would remain authoritative.

The capture record would include every attempted frame, detected drop or gap,
exposure and cadence, timing-source state, predicted event window, target-field
solution, node coordinates, and the exact signed plan and configuration
versions. Preview imagery or display rendering would never replace raw data.

## Reduction and validation

A dedicated reduction pipeline would perform raw calibration, track the target
star and suitable comparisons, derive a high-cadence differential light curve,
detect candidate disappearance/reappearance edges, and estimate uncertainty
without rounding away cadence or timing errors. It would preserve the original
sequence and an auditable derivation product.

Review would explicitly test clouds, guiding excursions, scintillation,
saturation, blending, dropped frames, rolling-shutter effects, comparison-star
behavior, timing-lock loss, and inconsistent positive/negative chords. A
negative result would be reported with the same coordinate, timing, weather,
equipment, and coverage evidence as a positive chord; silence is not a valid
negative observation.

## Reporting path

The program owner and an external scientific reviewer would select the relevant
prediction and reporting organizations and approve their required formats.
Reports would include site coordinates and uncertainty, equipment and cadence,
timing method and certification version, positive or negative result, event and
star identifiers, disappearance/reappearance times with uncertainty, quality
flags, and reviewer disposition. Submission would remain reviewed until the
full process has demonstrated reproducible scientific acceptance.

## Go/no-go gate

Runtime implementation may begin only when all of these are true:

- at least five nodes have completed timing certification;
- end-to-end timing residual is below the threshold selected for the specific
  science program;
- synthetic events, clock faults, holdover, restarts, dropped frames, and
  storage-pressure tests succeed;
- the network successfully observes known, non-critical occultations, including
  meaningful negative chords;
- an external scientific reviewer approves timing validation, reduction,
  uncertainty, chord construction, and reporting methods.

Until that gate is met, documentation is the only supported occultation work.
