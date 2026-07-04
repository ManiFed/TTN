# Node Lifecycle Audit & Failure-Mode Map

Audit date: 2026-07-04. Scope: everything between "member runs the installer" and
"member wakes up to a night summary", across macOS/Linux/Windows nodes and the
cloud (`api.thetelescope.net`).

---

## 1. Lifecycle trace (first install → first unattended night)

| # | Stage | Code | Notes |
|---|-------|------|-------|
| 1 | Installer writes config | `build/macos/postinstall.sh`, `build/linux/install.sh`, `build/windows/install.nsi` | Template copied to data dir, activation code substituted (or blanked), `chmod 600`. |
| 2 | Service start | launchd `KeepAlive` / systemd `Restart=always` / NSSM → `src/main_service.py` → `dashboard.launch()` | Runs headless (`--no-browser`), restarts on crash. |
| 3 | Sleep prevention | `src/sleep_prevention.py` + installer (`pmset -c sleep 0`, `systemctl mask sleep.target`) | Windows: `SetThreadExecutionState`; macOS: `caffeinate -s` (AC only). |
| 4 | Telescope discovery + connect | `alpaca/discovery.py`, `POST /api/connect` (`dashboard.py:1935`) | **Browser-JS only** — the saved `default_server` auto-connect lives in the dashboard page script (`dashboard.py:~7389`), not in the server process. |
| 5 | Cloud registration | `cloud_communicator._ensure_registered` → `POST /api/v1/nodes/register` | Activation code from config, or pair-token polling (`_pair_loop`). Credentials persisted to `data/cloud_state.json`. |
| 6 | Heartbeat loop | `_heartbeat_loop` (60 s) → `POST /api/v1/nodes/heartbeat` | Carries `conditions` snapshot (`_cloud_conditions`); flushes the disk-backed measurement queue after each success. |
| 7 | Plan polling | `_plan_loop` (300 s) → `GET /api/v1/plan` | New `plan_id` → `_on_cloud_plan` → validate → `_run_schedule_bg`. Also polls interrupts and config patches. |
| 8 | Plan execution | `_run_schedule_bg` / `_run_schedule_observation` (`dashboard.py:3060`) | Unpark → per-item wait/slew/expose. FITS saved to `data/fits/` and queued for photometry. |
| 9 | Image watching | `src/image_watcher.py` on the Seestar SMB mount | Mounted by `_auto_mount_and_watch` **only at connect time**. |
| 10 | Photometry | `_phot_worker` → `src/photometry.run_pipeline` | Plate solve (ASTAP/astrometry.net), comp stars, magnitude. |
| 11 | Upload | `submit_measurement` → `POST /api/v1/measurements`; AAVSO `.txt` → `/api/v1/aavso-files` | Failures queue to `data/cloud_upload_queue.json` (cap 500). |
| 12 | Dawn parking | `alpaca/safety_manager.py` | Solar-elevation watchdog, disconnect auto-park, SIGINT/SIGTERM park. Cloud-disconnect park after 30 min (`_cloud_disconnect_monitor_loop`). |
| 13 | Evidence | `logs/node.log` (local), heartbeat `conditions` (cloud `nodes.last_conditions`), `cloud/incidents.py`, `cloud/nights.py` missed-night push | Node itself emits **no structured incidents**; cloud infers from measurements/heartbeats. |

---

## 2. Failure-mode map (ranked: likelihood × harm)

Severity key — **P0**: silently loses whole nights or endangers hardware;
**P1**: loses data/science or blocks onboarding; **P2**: degrades quality or
diagnosability.

### P0 — critical

| ID | Failure mode | Trigger | Current behavior | Evidence trail | Gap |
|----|-------------|---------|------------------|----------------|-----|
| **F1. Headless restart never reconnects telescope** | Power blip, crash+respawn, OS update, service restart while member is asleep | `launch()` never connects devices; auto-connect to `alpaca.default_server` is browser JS. Service restarts, heartbeats resume, but `_tel is None` forever. Plans "run" with "telescope not connected — skipping slew". | Local log lines only. Cloud sees an *active, healthy* node. First signal = missed-night push next morning. | No server-side auto-connect/reconnect supervisor. |
| **F2. Plan consumed during daylight** | Cloud replans every 120 min around the clock (`cloud/main.py:104`); new `plan_id` arrives mid-day | Dawn latch → `_slew_rejection` returns "unsafe" → every item skipped in seconds → schedule "done"; `_last_plan_id` marks the plan consumed. Night salvaged only if a later replan lands after dusk. | `schedule_error` in next heartbeat conditions (transient), local log. | Schedule doesn't wait for darkness; no plan-outcome report to cloud. |
| **F3. Items >2 h in the future run immediately** | Any plan spanning a night (they all do) | `_run_schedule_observation` waits only if `0 < wait_s <= 7200` (`dashboard.py:3091`); otherwise slews now. Transit/time-series windows missed; targets observed at wrong airmass; whole plan executes back-to-back at dusk then idles. | None — looks like success. | Wait cap wrong for autonomous overnight use. |
| **F4. Time-series mode silently stripped** | Cloud sends `observation_mode: time_series` + `duration_minutes` (CHORUS `perform.py:90`) | `_validate_schedule_items` whitelists only 7 keys; executor then reads the stripped fields → every cloud plan degrades to single-epoch. Exoplanet transits quietly become ~20 snapshots. | None. | Validator/executor contract mismatch. |
| **F5. Telescope left tracking after node death** | Node process dies mid-observation and can't restart (disk full, corrupt config), or host sleeps | SafetyManager dies with the process; Seestar keeps tracking toward dawn. (Signal-path park only covers clean SIGTERM/SIGINT.) | Heartbeats stop; cloud marks node offline after 15 min — no push until missed-night next morning. | No prompt "node went dark" alert; no scope-side failsafe documented. |
| **F6. Corrupt `config.yaml` kills watchdog threads** | Crash mid-write (3 non-atomic writers: `api_connect` serial save, default-server save, `_apply_paired_code`), disk full, user edit | `_load_config` catches only `FileNotFoundError`; a YAML parse error propagates. `_cloud_disconnect_monitor_loop` calls it bare every 30 s → unhandled exception permanently kills that daemon thread. Other callers fail per-call. | Stack trace in local log once per caller. | Non-atomic writes; no parse-error fallback; monitor loops unwrapped. |

### P1 — loses data/science or blocks onboarding

| ID | Failure mode | Trigger | Current behavior | Gap |
|----|-------------|---------|------------------|-----|
| **F7. Activation bricked by lost registration response** | Server consumes one-time code (`_validate_and_consume_code`), response lost in transit; node retries with blank `node_id` | Server: "activation code already used" (409) forever, every 60 s. Nontechnical member is stuck; needs a new code and manual config edit. | No idempotent retry window server-side; no backoff or user-visible remediation node-side. |
| **F8. Disk fills over unattended weeks** | `data/fits/`, `data/images/`, `fits_export/`, `aavso_submissions/` grow unbounded; only logs rotate | Photometry, config writes, queue writes start failing with confusing per-subsystem errors. | No retention pruning, no free-space telemetry or threshold event. |
| **F9. SMB mount dies overnight** | Seestar reboot/Wi-Fi drop; kernel invalidates the CIFS/SMB mount | watchdog observer goes quiet forever; `_auto_mount_and_watch` runs only inside `api_connect`. Node exposes frames nobody processes (Seestar-side captures). | No watcher liveness check or re-mount loop. |
| **F10. Upload queue corruption / stall** | Crash during non-atomic `_QUEUE_FILE.write_text`; or 500-deep queue after an outage | Corrupt JSON → `_load_queue` returns `[]` → silent loss of ≤500 measurements. Flush is serial inside `_queue_lock` on the heartbeat thread: 500 items × 30 s timeout can block heartbeats (and `submit_measurement` callers) for hours. | Atomic write; flush batching/time-box; queue-drop event. |
| **F11. Registration failure loop is invisible** | Bad URL, expired code, cloud outage at first boot | `_ensure_registered` retries every 60 s, logs a warning locally. Dashboard shows error *if someone opens it*. Cloud can't see a node it doesn't know. | Installer/pair flow has no "phone home failed" surface; no backoff; pair token only printed to stdout. |
| **F12. Wrong host clock** | RPi/mini-PC without RTC, dead CMOS battery, DST edge | Dawn park mistimed (solar math uses `time.time()`); plan `startTime` (HH:MM local) waits are wrong; BJD stamps corrupt measurements. Heartbeat response includes `server_time` but the node ignores it. | No clock-skew check against cloud; no NTP health event. |
| **F13. Config patches silently require restart** | Remote operator queues a patch (e.g. enable photometry, change heartbeat interval) | `apply_config_patch` acks OK, but `CloudCommunicator` intervals, photometry worker, image watcher, safety thresholds were all read at startup. Operator believes the fix landed. | No patch→subsystem reload map; ack doesn't distinguish "applied" from "applied, restart required". |

### P2 — degrades quality or diagnosability

| ID | Failure mode | Trigger | Current behavior | Gap |
|----|-------------|---------|------------------|-----|
| **F14. Node emits no structured incidents** | Slew timeout, exposure failure, photometry crash, plate-solve fail, emergency park | All local-log only. Cloud incident types (`slew_failed`, `device_disconnect`, `plate_solve_failed`…) exist but are only ever created from measurement ingest. Remote diagnosis = asking the member to read logs. | Node→cloud event channel missing. |
| **F15. No plan execution acknowledgment** | Any | Cloud never learns a plan was received/started/completed/skipped; only indirect `conditions.schedule_*` snapshots at heartbeat instants. Missed-night detection is next-morning batch. | Plan lifecycle telemetry. |
| **F16. Restart mid-night replays the plan from item 1** | Any overnight restart | `_last_plan_id` is in-memory; the same plan re-runs from the top; completed targets re-observed, time budget wasted. | No per-item completion persistence. |
| **F17. macOS battery/lid sleep** | Laptop nodes: `caffeinate -s` and `pmset -c` cover AC only; lid close sleeps regardless of assertions on some configs | Night lost; heartbeats stop; no wake-gap detection on resume. | Detect monotonic-vs-wall clock gap on wake → event; document lid/UPS requirements in installer. |
| **F18. Photometry queue overflow drops frames** | Slow plate solves + fast captures (queue cap 50) | Frames dropped with a local warning; measurement never exists, so cloud can't miss it. | Drop counter in heartbeat conditions. |
| **F19. Interrupt runs bypass validation and safety context** | Malformed/hostile interrupt payload | `_on_cloud_interrupt` builds items directly (no `_validate_schedule_items`) and `_interrupt_dispatcher_loop` runs them. RA/Dec bounds unchecked. | Route interrupts through the validator. |
| **F20. `api_connect` writes enriched config back** | Any connect that discovers a serial | `_load_config()` output (geolocation/telescope-spec enriched) is dumped wholesale to `config.yaml`, baking derived values into user config; concurrent with patch writes → last-writer-wins races. | Targeted patch writes instead of full-dump. |

---

## 3. What already works well (keep)

- SafetyManager: latched emergency park, reconnect-with-backoff, dawn latch +
  auto-clear at dusk, signal-handler park, dedicated heartbeat HTTP session.
- Disk-backed measurement retry queue with heartbeat-cadence flush.
- Cloud-side: incident classification/attribution, auto-triage, reliability
  scoring, missed-night push notification, config-patch queue with acks.
- Installers: idempotent config write, service auto-restart, OS-level sleep
  masking, `chmod 600` config.
- Schedule executor: per-item exception isolation, cancellation checks between
  frames, slew-confirmation gate before exposing.

## 4. Remediation status

Implemented in this pass (regression-locked by `tests/gauntlet/`, run with
`make gauntlet`):

| Fix | Failure modes | Where |
|-----|---------------|-------|
| **NodeSupervisor** — headless auto-reconnect to the saved ALPACA server with exponential backoff, image-watcher revival + SMB re-mount, disk-low events, retention pruning (`storage.retention_days`, default 14), host-sleep detection via wall-vs-monotonic gap | F1, F8, F9, F17 | `src/node_supervisor.py`, wired in `dashboard.launch()`; connect path extracted to `dashboard._do_connect()` |
| **Wait-for-darkness gate** — cloud plans and interrupts wait for the sun to pass the dawn threshold before consuming items; a newer cloud plan supersedes one still waiting | F2 | `dashboard._wait_for_darkness`, `_run_schedule_bg(source, wait_for_dark)`, `_on_cloud_plan` |
| **Full startTime waits** — items later tonight are waited for in full (≤16 h); only items overdue by <8 h run immediately | F3 | `dashboard._start_wait_seconds` |
| **Time-series fields preserved** — validator passes `observation_mode`, `duration_minutes`, `filter`, `notes` through with bounds checks; interrupts go through the same validator | F4, F19 | `dashboard._validate_schedule_items`, `_on_cloud_interrupt` |
| **Config robustness** — `_load_config` falls back to the last good config on parse errors (with a one-shot event); all node config writes now go through atomic `apply_config_patch`; the cloud-disconnect watchdog survives any tick exception | F6, F20 | `dashboard._load_config`, `_cloud_disconnect_tick`, `cloud_communicator._clear_activation_code`/`_apply_paired_code` |
| **Structured telemetry** — JSONL event log + in-memory ring (`/api/events`), evidence summary in every heartbeat (`conditions.events`), error/critical events forwarded to the cloud incident API from a bounded background queue | F5*, F11, F14, F15, F18 | `src/telemetry.py`, `POST /api/v1/incidents` in `cloud/server.py`, events wired throughout `dashboard.py`/`cloud_communicator.py` |
| **Upload-queue hardening** — atomic queue writes, overflow evidence, flush time-boxed to 25 items/60 s per heartbeat so a deep backlog can't stall the heartbeat thread | F10 | `cloud_communicator._save_queue`/`_flush_queue` |
| **Registration resilience** — exponential backoff (60 s → 15 min) with `registration_failed` events; server-side 15-minute idempotent retry window returns existing credentials when a just-consumed activation code is re-presented (lost-response recovery) | F7, F11 | `cloud_communicator._ensure_registered`, `cloud/server.py _activation_retry_credentials` |

\* F5 is *partially* mitigated: an emergency park and heartbeats-gone-dark are
now visible evidence, but a hard host death still relies on the cloud noticing
the stale heartbeat. A prompt cloud-side "node went dark mid-plan" alert
(before the next-morning missed-night batch) remains open.

## 5. Remaining open items

- **F12 (clock skew)**: compare `server_time` from the heartbeat response
  against local time; emit `clock_skew` telemetry beyond ±60 s.
- **F13 (patches needing restart)**: config-patch ack should report
  `restart_required` for keys read only at startup, and the node should
  self-restart at a safe time (parked + no schedule).
- **F16 (plan replay after restart)**: persist per-item completion keyed by
  `plan_id` so a mid-night restart resumes instead of replaying from item 1.
- **F15 (plan lifecycle acks)**: plan `received/started/finished` events now
  exist node-side; a cloud-side view correlating them per plan_id would make
  missed-night triage immediate.
