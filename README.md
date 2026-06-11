# Boundless Skies — Node v1

**Node Software (Layer 4)** for the Boundless Skies automated telescope network. Runs on a member's computer, connects to a Seestar S50 via the ALPACA API, monitors for new FITS files, runs local differential photometry, and submits calibrated magnitudes to AAVSO.

> **Boundless Skies** is an accessible astronomy charity that gives people with disabilities access to real telescope time. Seestar owners donate their nights; AI schedules variable-star observations, processes photometry, and submits to AAVSO on the member's behalf.

---

## Phase 0 status

| Milestone | Description | Status |
|-----------|-------------|--------|
| M1 | ALPACA API client layer (telescope / camera / focuser / filterwheel) | ✅ Done |
| M2 | ImageWatcher — detect new FITS files from Seestar SMB share | ✅ Done |
| M3 | Local photometry pipeline (plate-solve → comp stars → aperture photometry → magnitude) | ✅ Done |
| M4 | First AAVSO-accepted automated observation (magnitude within 0.15 mag of known) | 🔄 In progress |

**Success criterion for Phase 0:** one AAVSO-accepted observation with magnitude agreeing within 0.15 mag of a known standard.

---

## What is ALPACA?

ALPACA (Astronomy Low-level Control And Automation) is an open HTTP/JSON protocol by the [ASCOM Initiative](https://ascom-standards.org/). It lets astronomy software talk to mounts, cameras, focusers, and other equipment over a network without proprietary drivers.

---

## Prerequisites

- **Python 3.10+**
- **ZWO Seestar S50** (or any ALPACA-compatible telescope) on the same LAN
  - Server must be reachable via UDP broadcast (port 32227) and HTTP (typically port 11111)
- **ASTAP** plate solver — download from [hnsky.org](https://www.hnsky.org/astap.htm) and put it in your `PATH` (only needed if FITS files lack WCS; Seestar usually solves onboard)
- **macOS / Linux / Windows** — no platform-specific code; tested on macOS

---

## Installation

1. **Clone or navigate into this directory.**

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   Key packages: `requests`, `pyyaml`, `flask`, `watchdog`, `astropy`, `photutils`, `astroquery`, `numpy`, `Pillow`, `zwoasi`, `pyongc`

4. **Edit `config.yaml`** — at minimum, set:
   - `observatory.latitude` / `longitude` (your site coordinates)
   - `photometry.node_id` (unique ID for your node)
   - `aavso.observer_code` / `username` / `password` (your AAVSO credentials)
   - `image_watcher.watch_path` (SMB share path where Seestar writes FITS files)

---

## Quick Start

```bash
python dashboard.py
```

The Flask server starts on `http://localhost:5173` and opens a browser automatically.

**First-run checklist:**
1. Click **Discover** to find the Seestar on your LAN
2. Click **Connect** to establish the ALPACA connection
3. Enable `image_watcher.enabled: true` in config to start watching for new FITS files
4. Enable `photometry.enabled: true` to run the pipeline automatically on each new file
5. Set `aavso.dry_run: true` for a test run before live submission

---

## Dashboard

The web dashboard provides full observatory control from a browser.

### Telescope controls
- **Discover / Connect / Disconnect** — ALPACA server management
- **Unpark / Park** — mount stowing
- **Tracking on/off** — sidereal tracking toggle
- **Slew** — RA/Dec (decimal or sexagesimal) or Alt/Az coordinate entry
- **Joystick / nudge** — real-time directional control
- **Telescope modal** — live coordinate display and slew history

### Scheduling
- **Object catalog** — browse Messier / NGC objects (powered by `pyongc`)
- **Scheduling modal** — queue targets for an observing run with per-target exposure count and filter selection
- **Schedule runner** — executes the queue: unpark → slew → expose × N → next target

### Imaging
- **Manual exposure** — trigger a single exposure from the dashboard
- **FITS browser** — list and download captured FITS files
- **Observation history** — thumbnail gallery of completed observations
- **Pier cam** — optional live video feed from a ZWO guide/pier camera

### Safety
- **Safety Manager** — continuous background watchdog (heartbeat, reconnect, dawn parking, OS signal handling)
- **Sky area safety monitor** — auto-detect horizon obstructions; block slews into masked zones
- **Horizon scan** — drive the telescope along the horizon to map the local obstruction profile

### Configuration
- **Config editor modal** — live edit `config.yaml` from the browser (parsed YAML, validated on save)
- **Config reload** — apply changes without restarting the server

### Photometry & submission
- **Photometry status** — last pipeline result (target, magnitude, uncertainty, SNR, quality flag)
- **AAVSO submission status** — last submission outcome (accepted / rejected / dry_run)

### Live log
Server-Sent Events stream of all log messages with timestamps and level colours.

---

## Photometry Pipeline (`photometry.py`)

Runs automatically on each new FITS file when `photometry.enabled: true`.

```
FITS file
  → 1. Ensure WCS         (check header; run ASTAP plate solve if absent)
  → 2. Locate target      (world_to_pixel; reject if too close to edge)
  → 3. Estimate FWHM      (DAOStarFinder + second-moment Gaussian stamps)
  → 4. Comparison stars   (AAVSO VSP API → Gaia DR3 fallback; merge, deduplicate)
  → 5. Aperture photometry (CircularAperture + sigma-clipped annulus background)
  → 6. Differential photometry (weighted zero-point ensemble; Poisson + ZP scatter)
  → 7. Ancillary data     (BJD_TCB via astropy, airmass from Alt/Az or header)
  → 8. Quality flag       (good / acceptable / poor based on SNR, uncertainty, comp stars)
  → result dict
```

**Output:**
```python
{
    "target_name":      "SS Cyg",
    "bjd":              2460500.123456,
    "magnitude":        12.341,
    "uncertainty":      0.031,
    "filter":           "CV",
    "airmass":          1.24,
    "fwhm":             3.8,        # pixels
    "snr":              52.0,
    "comparison_stars": 9,
    "quality_flag":     "good",
    "node_id":          "node_001",
    "zero_point":       22.413,
    "zp_scatter":       0.028,
    "fits_file":        "seestar_image.fits",
}
```

**Public API:**
```python
from photometry import run_pipeline
result = run_pipeline(fits_path, config)   # returns dict or None
```

---

## AAVSO Submission (`aavso_submission.py`)

Takes a photometry result dict, formats it as AAVSO Extended File Format, POSTs it to WebObs, and writes a full audit trail to disk.

```python
from aavso_submission import submit
result = submit(measurement, config)
# result["status"] → "accepted" | "rejected" | "skipped" | "dry_run" | "error"
```

**Audit trail** (written to `aavso_submissions/YYYY-MM-DD/`):
- `<target>_<bjd>.txt` — the AAVSO Extended Format submission text
- `<target>_<bjd>_response.txt` — raw WebObs HTTP response
- `<target>_<bjd>_record.json` — complete audit record with all measurement fields

**Config keys** (`config["aavso"]`):

| Key | Description |
|-----|-------------|
| `observer_code` | AAVSO OBSCODE (required) |
| `username` / `password` | AAVSO login credentials (required to POST) |
| `dry_run` | If `true`, format and save but do not POST |
| `submit_poor_quality` | If `true`, submit even `quality=poor` measurements |
| `audit_dir` | Local directory for audit trail (default: `aavso_submissions`) |
| `chart_id` | VSP chart ID to include in submission |

---

## FITS Export (`fits_export.py`)

After a successful photometry run, writes a science-ready copy of the FITS file enriched with observatory, detector, and processing headers. Original Seestar headers are never overwritten.

Added keywords include: `TELESCOP`, `INSTRUME`, `OBSERVER`, `SITELAT`, `SITELONG`, `GAIN`, `RDNOISE`, `BJD-OBS`, `AIRMASS`, `SWCREATE`, `HISTORY` entries for the photometry result and WCS provenance.

Enabled via `photometry.fits_export.enabled: true` in `config.yaml`.

---

## Architecture

```
dashboard.py (Flask, port 5173)
  │
  ├─ API endpoints
  │   ├─ /api/discover, /api/connect, /api/disconnect
  │   ├─ /api/telescope/unpark, park, tracking, nudge, moveaxis
  │   ├─ /api/slew                        — RA/Dec or Alt/Az
  │   ├─ /api/camera/expose, abort
  │   ├─ /api/schedule/run, status, abort — multi-target queue
  │   ├─ /api/catalog                     — object catalog (pyongc)
  │   ├─ /api/photometry, /api/aavso       — pipeline status
  │   ├─ /api/fits/list, download          — FITS file browser
  │   ├─ /api/history, history/<id>        — observation gallery
  │   ├─ /api/safety, horizon-mask, horizon-scan
  │   ├─ /api/pier-cam/stream, snapshot
  │   ├─ /api/config (GET/POST)            — live config editor
  │   ├─ /api/status                       — full system state
  │   └─ /api/logs                         — SSE log stream
  │
  ├─ SafetyManager (daemon thread)
  │   ├─ Heartbeat monitor (every 30 s)
  │   ├─ Reconnect with backoff
  │   ├─ Dawn parking (NOAA solar elevation algorithm)
  │   ├─ Sky area / horizon mask enforcement
  │   └─ SIGTERM / SIGINT → emergency park
  │
  ├─ ImageWatcher (daemon thread, when enabled)
  │   ├─ OS file system events (FSEvents / inotify / kqueue)
  │   ├─ Debounce for partial writes
  │   ├─ FITS header extraction
  │   └─ Triggers photometry pipeline on new file
  │
  ├─ Photometry pipeline (photometry.py)
  │   ├─ WCS check / ASTAP plate solve
  │   ├─ AAVSO VSP + Gaia DR3 comparison stars
  │   ├─ Aperture photometry (photutils)
  │   ├─ Differential photometry + quality flag
  │   └─ → AAVSO submission + FITS export
  │
  ├─ CloudCommunicator (daemon threads, when cloud.enabled)
  │   ├─ Auto-registration with the Boundless Skies cloud
  │   ├─ Heartbeats with local conditions
  │   ├─ Observation-plan polling → schedule runner
  │   ├─ Interrupt polling (high-priority targets)
  │   └─ Measurement upload after each photometry run (disk-backed retry queue)
  │
  └─ DeviceManager → AlpacaClient (HTTP/JSON)
       ├─ Telescope
       ├─ Camera
       ├─ Focuser (optional)
       └─ FilterWheel (optional)
```

---

## Cloud Layer (`cloud/`)

The cloud coordinates many nodes into one network. Run it anywhere with
Python + this repo:

```bash
python -m cloud.main              # uses cloud/config.yaml, serves on :8800
```

What it does:

- **Node registry** — nodes auto-register with location + telescope details
  and get an API key; light pollution is fetched automatically for each
  location (`lightpollutionmap.info` key optional).
- **Alert ingestion** — pulls ALeRCE, Gaia Alerts, TNS, ATLAS, ASAS-SN, and
  the AAVSO/VSX watch list on an interval, deduplicating by 3″ cross-match.
- **Scoring engine** — composite score per (target, node): brightness match,
  scientific value, time criticality, network coverage gap, and observability
  (light pollution, weather forecast, moon, airmass, visibility window,
  telescope match).
- **Scheduler** — generates a nightly plan per node in the exact JSON the
  node schedule runner consumes, packed by altitude inside the node's dark
  window, with start times in the node's local clock.
- **Data pipeline** — ingests every measurement with capture-time conditions,
  cross-validates co-temporal measurements across nodes, serves aggregated
  light curves, and batch-submits validated results to AAVSO in Extended
  Format under the network observer code (dry-run by default).
- **APIs** — node endpoints (register/heartbeat/plan/measurements/images/
  interrupts) plus query endpoints (`/api/v1/targets`, `/api/v1/lightcurves/
  <name>`, `/api/v1/network/status`) for the future member dashboard and app.

To connect this node to a cloud, set in `config.yaml`:

```yaml
cloud:
  enabled: true
  url: https://cloud.example.org
  auto_run_plans: true     # execute the nightly plan automatically
```

Leave `node_id`/`api_key` blank — the node registers itself on first start
and persists its credentials in `data/cloud_state.json`. Cloud status is
visible at `/api/cloud` on the dashboard.

---

## Configuration Reference (`config.yaml`)

### Photometry

```yaml
photometry:
  enabled: false           # set true to auto-run on each new FITS file

  node_id: "node_001"      # unique node identifier — set before joining network
  filter_name: "CV"        # AAVSO filter code (CV=broadband, V=Johnson-V)

  gain: 1.0                # e-/ADU  (Seestar S50 ≈ 1.0)
  read_noise: 5.0          # ADU

  target:                  # leave blank to use FITS header values (normal operation)
    name: ""               # e.g. "SS Cyg"
    ra_deg: ~
    dec_deg: ~

  astap_path: "astap"      # path to ASTAP executable
  astap_search_radius: 10  # degrees

  aperture_factor: 2.5     # aperture radius = factor × FWHM
  annulus_inner:   4.0
  annulus_outer:   6.0

  field_radius: 0.5        # degrees for comparison star search
  mag_limit: 15.0

  min_comparison_stars: 3
  snr_threshold: 20
  max_uncertainty: 0.3
  max_airmass: 3.0

  fits_export:
    enabled: true
    export_dir: "fits_export"
```

### AAVSO

```yaml
aavso:
  observer_code: ""        # AAVSO OBSCODE  ← required
  username: ""             # AAVSO login    ← required to POST
  password: ""

  audit_dir: "aavso_submissions"
  dry_run: false
  submit_poor_quality: false
  chart_id: ""
```

### Observatory

```yaml
observatory:
  name: ""                        # e.g. "Boundless Skies Node 001"
  latitude: ~                     # decimal degrees N
  longitude: ~                    # decimal degrees E (negative = West)
  elevation: 0.0                  # metres
  telescope: "ZWO Seestar S50"
  instrument: "ZWO Seestar S50 IMX462"
  observer: ""                    # AAVSO observer code or name
```

### Safety

```yaml
safety:
  enabled: true
  disconnect_timeout: 600     # seconds — park after this long without contact
  heartbeat_interval: 30      # seconds between pings
  reconnect_attempts: 3
  reconnect_delay: 10
  park_at_dawn: true
  dawn_type: astronomical     # astronomical (-18°), nautical (-12°), civil (-6°)
  observer:
    latitude: 0.0             # ← set your location for dawn detection
    longitude: 0.0
```

### Image Watcher

```yaml
image_watcher:
  enabled: false
  watch_path: "/mnt/seestar"   # SMB share mount point
  debounce_delay: 2.0          # seconds
```

### Pier Cam (optional ZWO guide camera)

```yaml
pier_cam:
  enabled: false
  device_index: 0
  exposure_ms: 80
  gain: 200
  bin: 2
  target_fps: 10
```

---

## Module Reference

### `photometry.run_pipeline(fits_path, config) → dict | None`

Runs the full pipeline on one FITS file. Returns the measurement dict on success, `None` on unrecoverable failure (bad WCS, no comp stars, non-positive flux).

### `aavso_submission.submit(measurement, config) → dict`

Formats and POSTs one observation to AAVSO WebObs. Never raises — returns `status="error"` on failure so the caller can continue. Writes the audit trail regardless of POST outcome.

### `fits_export.export_enhanced_fits(source_fits, result, config) → str | None`

Copies `source_fits` to `fits_export/YYYY-MM-DD/` and enriches headers. Returns destination path or `None` on failure.

### `alpaca/safety_manager.SafetyManager`

```python
sm = SafetyManager(telescope=None, config=cfg, on_unsafe=callback)
sm.attach_telescope(telescope)   # call after device connects
sm.start()                       # install signal handlers + begin monitoring
sm.stop()
sm.is_safe() -> bool
sm.status() -> dict              # safe, parked, reason, heartbeat_ok, sun_elevation, ...
```

### `image_watcher.ImageWatcher`

```python
watcher = ImageWatcher(watch_path, callback, debounce_delay=2.0)
watcher.start()
# callback(event_dict) called on each new .fits/.fit file
# event_dict keys: path, size, header, mtime
```

---

## Troubleshooting

### "No ALPACA servers found"
- Verify the Seestar is powered on and connected to the same subnet
- Some routers block UDP broadcast; confirm with `tcpdump -i en0 udp port 32227`
- macOS may require allowing Python in System Preferences → Privacy & Security

### "ErrorNumber 1: Device not connected"
- The ALPACA server responded but the device is not yet connected inside the Seestar app
- Ensure the Seestar app is running and the telescope is initialised

### Plate solve fails
- ASTAP not in PATH — set `photometry.astap_path` to the full binary path
- ASTAP star catalog not installed — download the D50 or H18 catalog from the ASTAP site
- Search radius too small for a wide-field image — increase `astap_search_radius`

### AAVSO submission rejected
- Check `aavso_submissions/YYYY-MM-DD/*_response.txt` for the raw WebObs error message
- Common causes: invalid observer code, target name not in AAVSO VSX, date out of range
- Use `dry_run: true` to validate the format before live submission

### Photometry quality always "poor"
- Low SNR: increase exposure time or check sky conditions
- Too few comparison stars: target may be outside AAVSO VSX coverage — Gaia DR3 fallback should help
- Large ZP scatter: poor seeing or clouds; the scatter contributes directly to uncertainty

### "Host is down" errors despite Seestar app showing connected

**Symptom:** Dashboard logs show repeated `[Errno 64] Host is down` or `ConnectTimeoutError` on port 32323, but the Seestar app displays the telescope as fully connected and operational.

**What happened:** The ALPACA HTTP server on the Seestar has wedged (hung, deadlocked, or crashed), while the core device firmware remains operational. The Seestar app talks to the firmware directly; the ALPACA endpoint is a separate HTTP service that can fail independently.

**Recovery:**

1. Open the Seestar app and restart the device: **Settings → Restart**.
2. A simple app quit and relaunch will not fix this — the HTTP daemon is built into the firmware and requires a full device reboot.
3. Once the Seestar finishes restarting, `dashboard.py` should reconnect cleanly.

This is a firmware quirk, typically triggered by rapid successive API calls or memory pressure. If it recurs frequently, check whether the node software is firing commands too fast (e.g., rapid slew tests or a tight loop without delays between API calls).

### SafetyManager: "telescope unreachable" / stuck unsafe state after reconnect

**Symptom:** Logs show repeated heartbeat failures, an emergency park attempt, and then all slews rejected with `Slew rejected — system is in an unsafe state (telescope unreachable for Xs)` even after the Seestar comes back online.

**What happened:** The SafetyManager lost contact with the ALPACA server for longer than `safety.disconnect_timeout` (default 600 s) and triggered an emergency park. Once the unsafe state is set it does not self-clear — slews remain blocked until the session is restarted.

**Likely root causes:**

- **Seestar dropped off WiFi** — the most common cause. `[Errno 64] Host is down` followed by `ConnectTimeoutError` in the same reconnect cycle indicates intermittent wireless loss rather than a clean disconnect. Check the Seestar's WiFi signal strength and whether your router rate-limits or disconnects idle devices.
- **Seestar app crashed or was suspended** — the device stayed on the network but the ALPACA HTTP server stopped responding.
- **Mac or host machine went to sleep** — the node software paused, accumulated a gap, and woke up past the timeout threshold.

**Recovery:**

1. Confirm the Seestar is back online — open a browser to `http://<seestar-ip>:32323/api/v1/telescope/0/connected` and check for a valid JSON response.
2. Restart `dashboard.py`. The SafetyManager initialises in a safe state on startup and will re-attach cleanly.
3. If the emergency park command also failed (logged as `park command failed`), the mount may still be physically unparked — verify its position in the Seestar app before slewing.

**Preventing recurrence:**

- Place the Seestar closer to the access point or use a 2.4 GHz band for better range.
- Increase `safety.disconnect_timeout` if brief network glitches are common at your site (e.g. `900` or `1200` s).
- Disable Wi-Fi sleep / power-save on your router for the Seestar's MAC address.
- Prevent the host Mac from sleeping during a session: `sudo pmset -b sleep 0` or use Amphetamine/Caffeinate.

### "ErrorNumber 1024: Method Unpark is not implemented in this driver"

The Seestar S50 ALPACA driver does not implement the `Unpark` command. Unparking must be done from within the Seestar iOS/Android app. After unparking in the app, the dashboard's telescope controls will work normally — the `Unpark` button in the dashboard has no effect on this device.

### "CoverCalibrator connect failed: 400 Client Error"

A 400 response (rather than a connection error) means the ALPACA server is reachable but rejects the request — typically because no cover/calibrator device is configured at index 0 on this firmware version. This is a non-fatal warning; arm control is listed as unavailable in the dashboard but all other functions continue normally. If you do not have a cover calibrator attached, you can suppress these messages by setting `devices.covercalibrator.enabled: false` in `config.yaml`.

### Dawn parking not triggering
- `observer.latitude` and `observer.longitude` must be non-zero
- `dawn_type: astronomical` requires the sun to reach −18°; at mid-latitudes in summer it may not — try `nautical` or `civil`
- Dashboard header shows current sun elevation — compare against the configured threshold

---

## Extending

### Adding a custom safety check

```python
from alpaca.safety_manager import SafetyManager

class WindSafetyManager(SafetyManager):
    def _run_dawn_check(self):
        super()._run_dawn_check()
        if get_wind_speed() > 30:
            self._emergency_park("high wind")
```

### Gating operations on safety

```python
if not _safety_mgr.is_safe():
    logger.critical("Cannot proceed: %s", _safety_mgr.status()["reason"])
    return
my_device.move()
```

### Adding a new device type

1. Create `alpaca/mydevice.py` modelled on `telescope.py`
2. Register it in `DeviceManager.connect_all()` / `disconnect_all()`
3. Add a config entry under `devices:` in `config.yaml`

---

## License

Provided for the Boundless Skies project. ALPACA protocol maintained by the [ASCOM Initiative](https://ascom-standards.org/).

## Further Reading

- [ALPACA Specification](https://ascom-standards.org/Developer/Alpaca.pdf)
- [AAVSO Extended File Format](https://www.aavso.org/aavso-extended-file-format)
- [AAVSO WebObs](https://www.aavso.org/webobs)
- [ASTAP plate solver](https://www.hnsky.org/astap.htm)
- [photutils documentation](https://photutils.readthedocs.io/)
- [Astroquery (Gaia / AAVSO VSP)](https://astroquery.readthedocs.io/)
