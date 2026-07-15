# Network Science Expansion

This release adds three network capabilities: a photometric calibration mesh,
global transient-event tiling, and bounded disconnected-night autonomy. It
also publishes the occultation-array readiness design as a deferred program.

The scheduler remains cloud-directed. These features do not add node bidding,
peer-to-peer scheduling, weather-mesh coordination, follow-the-night relays,
or a separate display application. Local safety remains authoritative.

## Rollout state

| Capability | Code state | Production default |
|---|---|---|
| Calibration mesh | Collects, fits, validates, versions, and can roll back models | Shadow only; `apply_qualified: false` |
| Disconnected autonomy | Signs, verifies, journals, resumes, and reconciles plans | Disabled until keys and canaries are provisioned |
| GCN event tiling | Dedicated consumer, normalization, tiling, task dispatch, coverage | Separate worker; `live_dispatch: false` |
| Occultation array | Readiness and scientific design document | Documentation only |

The shadow and kill-switch defaults are intentional. Enabling one capability
does not enable the others.

## Self-calibrating photometric mesh

Survey uploads now carry stable-star instrumental photometry alongside the
existing frame data. The cloud stores only calibration-eligible samples:
catalog-matched, unsaturated, unblended, non-variable sources from good
zero-point frames with a trusted timestamp and response fingerprint.

Each response fingerprint captures the node, telescope/camera and sensor
identity, physical filter/effective response, gain, binning, processing mode,
and hardware-characterization version. A response-changing edit creates a new
fingerprint and starts a new shadow model; earlier measurements retain their
original model provenance.

The nightly worker fits robust weighted zero-point, color, extinction, and
slow-drift terms. It uses source-level held-out validation and treats each
fingerprint independently, while allowing sparse nodes to receive a cautious
family prior. A prior alone cannot qualify a model.

Qualification requires configured sample, star, night, overlap, color,
airmass, RMS, improvement, residual-color-slope, and consecutive-validation
gates. Standard bands require matching catalog-band reference photometry.
`CV` remains a fingerprinted natural system (`TN-CV/<fingerprint>`) unless a
future validated transformation explicitly qualifies it; it is never relabeled
as Johnson V by default.

Raw node values stay immutable in `measurements.magnitude` and
`measurements.uncertainty`. Network fields record the proposed or applied
correction, model version, uncertainty, state, and magnitude system. Existing
AAVSO records are never rewritten. Once enabled, qualified models affect only
new exports; a rollback immediately stops future application.

Optional CHORUS calibration-debt visits are disabled by default and require
operator-supplied standard fields. They are limited to 5% of usable night time
and two fields per node per week, and are suppressed when normal science needs
the capacity.

Relevant modules: `cloud/calibration.py`, `src/calibration_identity.py`,
`src/photometry.py`, `cloud/data_pipeline.py`, and `cloud/survey.py`.

## Global event tiling

`cloud.gcn_worker` is a dedicated long-lived NASA GCN Kafka consumer. It
subscribes to `gcn.heartbeat`, monitors consumer health, reconnects with
offset-backed recovery, and accepts JSON, Confluent Avro, VOEvent XML, and
Classic-over-Kafka key/value notices.

Notices normalize into immutable event revisions with source identity, role,
policy decision, localization, probability-map hash, credible area, distance
information, and cancellation generation. Duplicate deliveries are harmless;
updates replace future work while retaining history, and retractions cancel
waiting event work without interrupting an active exposure.

Policies are declarative in `gcn:` configuration:

- LVK: significant event plus `HasNS` threshold or external coincidence, with
  a credible-area cap.
- IceCube: Gold-class or `p_astro` threshold with a usable localization.
- GRB: configured area and optical-response-age limits.
- FRB: direct follow-up only for point/arcminute-scale localizations.
- SNEWS/Super-K: retain and correlate, but do not dispatch until an optical
  counterpart localization is available.
- Test/MDC: exercise the complete pipeline in shadow and never reach a node.

The tiler accepts point, ellipse, fixed HEALPix, and multi-order UNIQ maps. It
evaluates candidate tiles against field of view, darkness, altitude, Moon,
filters, depth, current occupancy, and CHORUS delivery reliability. It assigns
the highest marginal residual probability per occupied time, avoiding redundant
first-pass coverage. LVK maps with distance data can blend 70% locally indexed
galaxy-weighted probability with 30% raw-sky probability. A second epoch begins
at least 30 minutes later and is confirmation work, not new sky coverage.

Event tasks are not permanent science targets and are never included in an
offline autonomy bundle. Returned frames take the normal solve/extraction path
with event and tile provenance; coverage records probability, limiting depth,
latency, duplicate fraction, candidates, and failures.

Relevant modules: `cloud/gcn_consumer.py`, `cloud/gcn_events.py`,
`cloud/event_tiling.py`, `cloud/gcn_worker.py`, and `railway.gcn.toml`.

## Bounded disconnected-night autonomy

Before a night, the cloud can issue a signed, expiring Ed25519
`AutonomyBundle`. It contains absolute UTC plan windows, stable item IDs,
pre-ranked contingencies, resource budgets, node/config requirements, a
monotonic sequence, and an optional key-rotation record. The cloud keeps the
private key; nodes ship trusted public keys only.

Nodes reject invalid signatures, wrong-node bundles, unsupported schema,
expired or over-18-hour bundles, excessive budgets, insufficient agent
versions, bad commissioning/location state, and reused or rolled-back
sequences. A verified bundle is persisted atomically before acknowledgement.

While disconnected, a qualified node may only execute signed primary work,
skip unsafe or infeasible work, select signed contingencies, and resume the
first unfinished item after restart. It cannot invent targets or receive
dynamic GCN campaigns. Sun, horizon, mount, camera, disk, and emergency-park
gates always override bundle authority.

Clock qualification requires two recent heartbeat comparisons within 30
seconds and agreement between them. Skew greater than 60 seconds or a detected
wall-clock/monotonic discontinuity disables autonomy and returns the node to
the normal cloud-disconnect park policy. This is ordinary scheduling accuracy,
not occultation-grade timing.

The node uses two durable SQLite stores:

- `autonomy.db` records bundle acceptance, attempts, terminal state, frame
  counts, failure reason, checkpoint, and upload state.
- `node_outbox.db` stores measurements, survey batches, execution outcomes,
  and structured telemetry with byte quotas, priorities, and idempotency keys.

Scientific records are retained ahead of previews and telemetry. If retained
science reaches the configured budget, no new signed item starts; the node
parks through the existing safety path rather than discard data. FITS files
referenced by pending scientific payloads are protected from retention pruning.
On reconnection, uploads and outcomes reconcile idempotently and preserve the
offline execution span in the cloud bundle record.

Relevant modules: `cloud/autonomy.py`, `src/autonomy.py`,
`src/durable_outbox.py`, `src/cloud_communicator.py`, `src/dashboard.py`, and
`src/node_supervisor.py`.

## Safe deployment

1. Apply additive PostgreSQL migrations by starting the cloud service.
2. Install cloud dependencies: `cryptography`, `gcn-kafka`, `fastavro`, and
   `astropy-healpix`.
3. Provision the separate GCN worker with `GCN_CLIENT_ID` and
   `GCN_CLIENT_SECRET`; leave `gcn.live_dispatch: false` during replay.
4. Let calibration collect for the validation period, inspect
   `/api/v1/admin/calibration/models`, then enable one qualified fingerprint at
   a time. Roll back with the matching admin endpoint.
5. Provision an Ed25519 signing key only in the cloud secret environment and
   install the public key on canary nodes. Keep `autonomy.enabled: false` until
   forced-disconnect, restart, and reconciliation canaries pass.
6. Enable live GCN dispatch only after replaying official/test notices and
   confirming update/retraction behavior under configured fleet/node-hour caps.

## Occultation array

The occultation program is not implemented. Its scientific, timing, hardware,
offline-execution, reduction, reporting, and go/no-go requirements are in
[OCCULTATION_ARRAY.md](future/OCCULTATION_ARRAY.md). No current configuration,
API, database table, or scheduler path claims occultation support.

## Validation

`tests/test_network_expansion.py` covers synthetic calibration recovery,
qualification safeguards, GCN normalization, Classic notice parsing,
RA-wrap/polar deterministic tiling, signed-bundle tamper/rollback/key-rotation
handling, restart-safe journals, and durable outbox behavior beyond the legacy
500-record ceiling. Existing CHORUS, supervisor, telemetry, and survey-upload
tests remain the compatibility guardrails.
