# THE ORGANISM + EVERY LENS ON EARTH

Two programs that change what the network *is*, not just what it schedules.
Both build on CHORUS (see [CHORUS.md](CHORUS.md)) and Open Aperture
(`cloud/survey.py`) without touching their internals.

> **Implementation status: built.** All six phases below are implemented and
> covered by tests (`tests/test_organism_cloud.py`, `tests/test_reflow_reflex.py`,
> `tests/test_ingest_worker.py`, `tests/test_triage.py`, `tests/test_historical.py`,
> `tests/gauntlet/test_organism.py`, `tests/gauntlet/test_companion.py`).
> Mid-night reflow is shipped **disabled** (`scheduler.reflow: false`) pending an
> operator decision to turn it on; reflex confirmation is enabled by default.
> The `realtime/` SSE gateway and the plate-solving `solver-worker` need their
> own Railway services provisioned (index-file volume, object storage) before
> production traffic — the code path degrades gracefully without them (nodes
> fall back to polling; contributions queue until a solver worker is running).

---

## Why

CHORUS plans once a night and nodes poll for it. Open Aperture only sees frames
from the fleet's own scheduled telescopes. Two structural limits followed from
that:

1. **The network is asleep between plans.** A node that clouds out mid-night
   just loses that block of science; nobody finds out until the morning
   summary. A promoted discovery candidate waits for the next scheduled
   observation — potentially a full night — before anything confirms it.
2. **The network only sees what it points at.** Every other astrophotographer's
   camera, and every frame ever taken before this network existed, is
   scientifically inert data sitting on someone's hard drive.

**THE ORGANISM** collapses the first limit: the network keeps a live,
second-scale picture of the fleet and reacts within seconds instead of within
a replan cycle. **EVERY LENS ON EARTH** collapses the second: any FITS frame,
from any camera, live or from years ago, becomes survey science.

---

## THE ORGANISM

### 1. Live nervous system

- `cloud/live.py` — `node_live_state` (one row per node: phase, target,
  darkness, sky clarity, heartbeat cadence) and `dispatch_events` (an
  append-only push log fed via Postgres `LISTEN/NOTIFY`).
- `realtime/` — a dedicated SSE gateway service (`Dockerfile.realtime`,
  `railway.realtime.toml`). Kept off the main API's small gunicorn pool
  deliberately: long-lived connections would starve request handling there.
  `GET /api/v1/stream` (node) and `GET /api/v1/stream/fleet` (member) with
  `Last-Event-ID` replay.
- Node side (`src/cloud_communicator.py`): heartbeat cadence adapts (5 s while
  observing, 60 s idle) and carries a `state` block; a background `_sse_loop`
  turns any push into an immediate authenticated re-fetch. The stream is a
  pure signal — content always comes over the normal API — so a node that
  never connects to SSE is unaffected; it just polls on the old cadence.
- `GET /api/v1/network/fleet`, `GET /api/v1/network/organism`,
  `GET /api/v1/me/nodes/<id>/live` expose the live map.

### 2. Mid-night reflow

`cloud/chorus/reflow.py`. When a node goes dark (offline, clouded, parked, or
carries an open critical incident) with unexecuted plan items still ahead of
it, reflow re-values those targets on the fleet's other currently-dark nodes
and dispatches the winners as interrupts.

**It does not reimplement CHORUS.** It calls `assign.build_opportunities` and
`assign.best_slot` — the same marginal-value function the nightly contingency
ladder already uses — against a residual ledger scoped to just the dropped
targets. `cloud/chorus/assign.py`, `cells.py`, and `ledger.py` are untouched.

Dispatch rides the existing interrupt path, so the node-side
never-preempt-an-active-exposure invariant applies automatically. Gated by
`scheduler.reflow` (off by default) and capped per night
(`scheduler.reflow_max_per_night`). `reflow_log` records every reassignment;
`reflow.reconcile_outcomes` (in the nightly maintenance loop) marks whether the
receiving node actually delivered, feeding — not altering — the CHORUS ledger's
realization data.

### 3. Reflex confirmation

`cloud/reflex.py`, hooked at the one place a discovery candidate is promoted
(`survey._record_detection`). Tasks 1–3 other dark, online nodes to confirm a
candidate within seconds instead of waiting for the next replan. Guarded by a
global nightly cap, a per-candidate cooldown, a minimum brightness step, and
dedup against any interrupt already open for that source — configured under
`survey.reflex` in `cloud/config.yaml`.

---

## EVERY LENS ON EARTH

### 1. Cloud plate solving

- `src/plate_solve.py` — a shared `solve-field` wrapper (blind or
  RA/Dec/scale-hinted), factored out of the node's existing photometry solver
  so both call the same tested code.
- `cloud/solver.py` — the cloud-side entry point: local `solve-field` first,
  optional `nova.astrometry.net` web fallback for frames outside the local
  index range (archive ingestion only — too slow for anything time-critical).

### 2. Universal ingestion

- `cloud/ingest_worker.py` — supersedes `cloud/contrib_worker.py` (now a thin
  re-export shim). Claims pending contributions with
  `FOR UPDATE SKIP LOCKED` so multiple worker replicas share the queue safely,
  then runs a staged pipeline: **triage → solve → extract → ingest**, recording
  progress per row (`contributions.stage`) so a failure is diagnosable.
- `POST /api/v1/me/contributions` no longer requires an incoming WCS — a
  no-WCS frame is queued for the cloud solver instead of rejected.
- `POST /api/v1/me/contributions/batch-manifest` — sha256 dedup so an archive
  upload never resends a frame the network already has.
- `src/companion.py` — a lightweight watcher mode (no telescope, no
  scheduler) that turns any astrophotographer's output folder into a feed:
  watches for new files and can sweep existing history in one pass, deduping
  locally by sha256 so a restart never re-uploads.

### 3. Triage

`cloud/triage.py` — a cheap heuristic gate before the expensive solver runs:
rejects frames with too few point sources, star-trailed frames (segmentation
elongation), and stretched/non-linear images (processing-software headers,
white-pixel pileup, 8-bit dynamic range with an elevated background) — because
photometry on already-processed pixels is not physically meaningful. No model
is used; the interface (`triage.classify`) is shaped so one could slot in
later for a class heuristics genuinely can't separate, but none has been
needed yet.

### 4. Historical ingestion + retrospective discovery

`cloud/survey.py::ingest_batch(..., historical=, provenance=)`. The Welford
running-mean/variance update is order-independent, so archive frames safely
enrich `survey_sources` baselines regardless of when they're uploaded relative
to their `DATE-OBS`. Two things are *not* safe by default, and are handled
explicitly:

- A deviant point from an archive frame (a nova sitting in a 2023 image) is
  **excluded from the baseline fold** — otherwise it would inflate the mean
  future measurements get compared against.
- It is written to `retro_discoveries`, a table entirely separate from the
  live `discovery_candidates` flow — it can never open an interrupt, trigger
  reflex confirmation, or otherwise touch a live node. `cloud/crossmatch.py`
  gained `run_pending_retro` (same VSX/TNS lookups, timestamp-agnostic) so an
  archive find still gets checked against known objects and surfaced for
  human review.

Frames route to this path when their `DATE-OBS` is older than
`survey.historical_days` (default 7). Provenance (`contribution_id`,
`user_id`) is carried on `survey_measurements` and `retro_discoveries` so a
confirmed find credits the person whose upload caught it;
`GET /api/v1/me/discoveries` unions both feeds.

---

## What was deliberately not touched

- **CHORUS's optimizer** — `cloud/chorus/assign.py` (greedy + local search),
  `cells.py`, `ledger.py`'s math. Reflow only *calls* `best_slot`.
- **Node safety invariants** — never preempt an active exposure, sun/horizon
  darkness gates, the supervisor watchdog.
- **Registration/auth**, and the disk-backed upload queues in
  `cloud_communicator.py` — SSE is additive; polling remains the correctness
  path if the realtime service is ever unavailable.

## File map

| Area | New | Modified |
|---|---|---|
| Live state / SSE | `cloud/live.py`, `realtime/` | `cloud/server.py`, `cloud/main.py`, `src/cloud_communicator.py`, `src/dashboard.py`, `cloud/db.py` |
| Reflow / reflex | `cloud/chorus/reflow.py`, `cloud/reflex.py` | `cloud/survey.py`, `cloud/main.py`, `cloud/db.py` |
| Plate solving | `src/plate_solve.py`, `cloud/solver.py` | `src/photometry.py` |
| Ingestion | `cloud/ingest_worker.py`, `src/companion.py` | `cloud/contrib_worker.py` (shim), `cloud/server.py`, `cloud/db.py` |
| Triage | `cloud/triage.py` | `cloud/ingest_worker.py` |
| Historical / retro | — | `cloud/survey.py`, `cloud/crossmatch.py`, `cloud/server.py`, `cloud/db.py` |

## Deploying

1. Provision a `solver-worker` Railway service (`Dockerfile.realtime` is the
   pattern to follow for `Dockerfile.solver` — not yet written; today
   `cloud/ingest_worker.py` runs on the main service, which is fine at low
   volume) with an astrometry.net index-file volume for the fleet's plate
   scales.
2. Provision the `realtime` service from `railway.realtime.toml` /
   `Dockerfile.realtime`; both services share `DATABASE_URL`.
3. Set `cloud.realtime_url` in node configs once the realtime service has a
   domain (nodes work fine without it — they just stay on polling).
4. Leave `scheduler.reflow: false` until you've watched `reflow_log` /
   `GET /api/v1/network/organism` under reflex-only operation and are ready
   for the network to retask nodes autonomously.
