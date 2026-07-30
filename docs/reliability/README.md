# Node Reliability: telemetry, supervision, and the gauntlet

Goal: *a nontechnical member installs the Node Agent, connects a telescope,
leaves it running overnight — and if something fails, the system either
recovers safely or produces enough structured evidence for remote diagnosis.*

Start with [failure_mode_map.md](failure_mode_map.md): the full lifecycle
audit, the ranked failure modes (F1–F20), and what has been fixed vs. what
remains open.

## The three reliability layers

### 1. Structured telemetry (`src/telemetry.py`)

Every operationally significant event is recorded as a structured event:

```python
from src import telemetry
telemetry.event("slew_failed", severity="error",
                target="T CrB", detail={"timeout_s": 180})
```

Each event lands in three places:

- **`logs/events.jsonl`** — size-capped JSON-lines file that survives
  restarts; attachable to a support request.
- **In-memory ring** — served by `GET /api/events` on the local dashboard
  (`?source=disk` reads the file; `?min_severity=warning` filters).
- **Cloud** — a compact summary (recent warning+ events, lifetime counters,
  uptime, free disk) rides in every heartbeat under `conditions.events`, and
  `error`/`critical` events are also forwarded to `POST /api/v1/incidents`,
  feeding the existing `reliability_incidents` table and incident triage.
  When the cloud is unavailable, forwarded incidents enter the node's durable
  SQLite outbox with an idempotency key and flush after reconnection; telemetry
  is lower priority than measurements, survey payloads, and execution outcomes.

Event names in use: `node_started`, `emergency_park`, `cloud_disconnect_park`,
`plan_received`, `plan_rejected`, `plan_deferred_auto_run_off`,
`schedule_started`, `schedule_finished`, `schedule_crashed`,
`schedule_abandoned_before_dark`, `slew_rejected`, `slew_failed`,
`exposure_failed`, `device_disconnect`, `device_reconnected`,
`device_connect_failed`, `image_watcher_restarted`, `image_watcher_down`,
`photometry_failed`, `photometry_queue_full`, `upload_queue_overflow`,
`registration_failed`, `registered`, `cloud_heartbeat_lost`,
`cloud_heartbeat_restored`, `config_parse_failed`, `config_parse_recovered`,
`interrupt_rejected`, `host_slept`, `disk_low`, `retention_pruned`.
Offline-autonomy additions include `clock_skew`, `clock_jump`, and
`offline_storage_exhausted`.

### 2. The NodeSupervisor (`src/node_supervisor.py`)

One periodic loop (30 s) that keeps a headless node observing:

- reconnects to the saved `alpaca.default_server` with exponential backoff
  when no devices are connected (a service restart at 2 a.m. now resumes
  observing without a browser);
- revives the image watcher and re-mounts the Seestar SMB share when the
  watch path dies;
- emits `disk_low` below 5 GB (critical below 1 GB) and prunes `data/fits/`,
  `data/images/`, `fits_export/`, `aavso_submissions/` past
  `storage.retention_days` (default 14; set `0` to disable);
- detects host sleep via wall-vs-monotonic clock divergence.

It respects an explicit user disconnect (`/api/disconnect`) and never dies:
every tick is exception-contained.

### 3. The reliability gauntlet (`tests/gauntlet/`)

Fault-injection tests that simulate field failures against real code paths
(a real HTTP fake cloud, real watchdog filesystem events, programmable
device doubles):

| Module | Injected faults |
|--------|----------------|
| `test_cloud_comm.py` | cloud outage / HTTP 500 / rejected registration, crash-corrupted queue files, 60-deep upload backlog, lost credentials |
| `test_schedule_contract.py` | daylight plan delivery, whole-night start times, time-series stripping, hostile interrupts, plan supersede races |
| `test_supervisor.py` | dead devices, driver panics, dead watcher, host sleep, low disk, ancient files |
| `test_safety_manager.py` | unreachable telescope, failing park commands, dawn/dusk transitions, exploding callbacks |
| `test_image_watcher.py` | partial writes, atomic renames, junk files, vanished watch paths, crashing photometry callbacks |
| `test_config_faults.py` | truncated/hand-mangled config.yaml, non-mapping roots, patch atomicity |
| `test_cloud_api.py` | unauthenticated incident posts, oversized payloads, lost registration responses, retired activation-code endpoints |
| `test_telemetry.py` | unwritable log dirs, corrupt event files, forwarder crashes, file growth |

Run it:

```
make gauntlet     # fault-injection tests only
make test         # entire suite (unit tests + gauntlet)
```

## Remote diagnosis playbook

When a member reports "it didn't observe last night":

1. **Cloud, no access needed**: check the node's `last_conditions.events` in
   the nodes table / app — recent warning+ events and counters tell you if it
   was a slew failure, daylight latch, disconnect, or dead disk. Check
   `reliability_incidents` for forwarded error/critical events with detail.
2. **Member one-liner**: have them open `http://localhost:5173/api/events?source=disk`
   and paste the output — the persisted JSONL covers restarts.
3. **Deep dive**: `logs/node.log` (rotating) plus `logs/events.jsonl` from the
   node's data directory.
