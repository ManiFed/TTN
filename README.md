# Boundless Skies — Node Software & Cloud

Seestar owners donate their telescope's nights. The pipeline schedules observations of
scientifically valuable targets, processes the data into calibrated photometry, and
submits it to AAVSO under the contributor's name. Members do real astronomy — no
manual intervention required.

> **Boundless Skies** is an accessible astronomy charity that gives people with
> disabilities access to real telescope time. AI schedules variable-star observations,
> processes photometry, and submits to AAVSO on each member's behalf.

---

## Repository Contents

```
node_v1-main/
├── main.py               Dev-mode watchdog (auto-restarts src/dashboard.py)
├── config.yaml           Node configuration (edit before first run)
│
├── src/                  Node Agent source (moved for clean separation)
│   ├── dashboard.py          Flask dashboard + control loop
│   ├── main_service.py       Production entry point (used by installers, no subprocess)
│   ├── sleep_prevention.py   Cross-platform OS sleep prevention
│   ├── cloud_communicator.py Node → Cloud API client (heartbeat, plan, measurements)
│   ├── photometry.py         Aperture photometry pipeline (ASTAP → comp stars → mag)
│   ├── stacking.py           RANSAC live sub-pixel stacking
│   ├── image_watcher.py      FSEvents/inotify watcher on the Seestar SMB share
│   ├── aavso_submission.py   AAVSO Extended File Format + WebObs API (node-side)
│   ├── fits_export.py        Enhanced FITS header writer
│   ├── geolocation.py        IP-based location detection for auto-registration
│   ├── shared_models.py      NodeInfo, TargetInfo, PlanItem, ObservationPlan, Measurement
│   └── __init__.py           Package marker
│
├── alpaca/               ALPACA protocol abstraction layer
│   ├── telescope.py      Slew, track, park, RA/Dec query
│   ├── camera.py         Expose, status, FITS export
│   ├── focuser.py        Move, halt, position
│   ├── autofocus.py      V-curve sweep → parabolic minimum
│   ├── filterwheel.py    Filter position management
│   ├── platesolve.py     ASTAP → WCS → closed-loop centering
│   ├── safety_manager.py Altitude limits, auto-park, dawn detection
│   └── device_manager.py Connect / disconnect all devices
│
├── cloud/                Cloud server (runs on a VPS, not on the telescope host)
│   ├── main.py           Entry point — Flask API + background loops
│   ├── server.py         All API routes (nodes, members, public, admin)
│   ├── db.py             SQLite schema + migration system
│   ├── registry.py       Node registration, auth, heartbeat, performance refresh
│   ├── auth.py           Member registration, login, bearer token auth
│   ├── alerts.py         Alert ingestion (ALeRCE, Gaia, TNS, ATLAS, ASAS-SN, AAVSO)
│   ├── scoring.py        Composite (target × node) scoring engine
│   ├── scheduler.py      Nightly plan generation
│   ├── data_pipeline.py  Measurement ingestion, cross-validation, AAVSO batch
│   ├── nights.py         Night summary generation, notification dispatch
│   ├── conditions.py     Weather, moon, airmass utilities
│   ├── tuning.py         Claude-powered weight tuning monitor (observability scoring)
│   └── config.yaml       Cloud configuration (fill in AAVSO credentials here)
│
├── cloud_data/          Data files (database, batches, exports)
│   ├── cloud.db          SQLite database
│   └── aavso_batches/    Generated AAVSO submission batches
│
├── website/             Marketing site + member portal (Phase 1)
│   ├── tour.html         Interactive founding network experience (main site)
│   ├── README.md         Website documentation and API integration guide
│   └── future/           Phase 2+ portal and future interfaces
│
├── scripts/
│   ├── manage.py         Admin CLI (status, ingest, batch, submit, check-aavso, generate-code)
│   └── seed_demo.py      Demo data generator for testing
│
└── build/                Installer build system
    ├── node_agent.spec   PyInstaller spec
    ├── build.py          Cross-platform build orchestration
    ├── config.template.yaml  Config template written by installers
    ├── windows/install.nsi   NSIS Windows installer
    ├── macos/            macOS .pkg + launchd plist
    └── linux/            systemd unit + install.sh
```

---

## Phase Status

| Phase | Status | Goal |
|-------|--------|------|
| **0 — Proof of Concept** | ✅ Code complete | First AAVSO-accepted automated observation |
| **1 — Core System** | 🚀 In progress | Installers shipped, member accounts live, **founding network site live**, 3–5 beta nodes |
| **2 — Launch** | 🔜 Next | 50 nodes, member portal app, first ATel, first grant application |
| **3 — Growth** | Not started | 200 nodes, 25 countries, 10,000+ AAVSO submissions |

---

## Recent Changes (June 2026)

### Marketing Site Launch (`website/tour.html`)
- **Interactive founding network experience** — single-page scrollytelling site
  - Beautiful, responsive design with glassmorphism + dark theme
  - Animated sky visualization using Aladin Lite (live star field background)
  - Real-time stats pulled from the cloud API (`/api/v1/network/status`, `/api/v1/lightcurves/SS_Cyg`)
  - Seven narrative sections: why it matters, the network idea, how to join, real example (SS Cygni), science impact, accessibility, founding seats
  - Email signup + funding donation flow (JavaScript form handling)
  - Fully responsive — mobile, tablet, desktop
  - No build step, no dependencies — runs as static HTML in any browser or on the cloud server

### Directory Reorganization
- **Node Agent source files moved to `src/`** — cleaner separation of concerns
  - `src/dashboard.py`, `src/cloud_communicator.py`, `src/photometry.py`, etc.
  - Main entry point `main.py` and `config.yaml` remain at project root
  - Installer and launch configurations updated

- **Website structure** (`website/`)
  - `website/tour.html` — **Main public site** (replaces index.html)
  - `website/future/` — Phase 2+ member portal scaffolding
  - `website/README.md` — Site setup and API integration docs

- **Database and demo data organized into `cloud_data/`**
  - `cloud_data/cloud.db` — SQLite database
  - `cloud_data/aavso_batches/` — Generated submission files
  - `scripts/seed_demo.py` — Demo data generator for testing (23 nodes across 14 countries)

### Claude Weight-Tuning Monitor (New Feature)
- **`cloud/tuning.py`** — Nightly advisory monitor that reads observation outcomes and proposes adjustments to observability scoring weights
  - **Hot path unchanged** — alert ingestion, scoring, and planning remain 100% procedural
  - **Opt-in** — controlled by `tuning.enabled` in `cloud/config.yaml`
  - **Audited** — every weight change is logged with reasoning and can be rolled back
  - Requires `ANTHROPIC_API_KEY` environment variable when enabled
  - See [Weight Tuning Monitor](#weight-tuning-monitor--claude-powered-observability-optimization) section for full documentation

### Database Schema Extensions
- New `tuning_state` table — stores live observability weights
- New `weight_history` table — audit trail of all scoring weight changes
- Fixed missing `filters` column in `nodes` table
- All migrations applied automatically on cloud server startup

### Alert Integration Improvements
- **ATLAS** — fixed token-based authentication endpoint
- All alert sources now use modern auth patterns

### Other Updates
- `requirements.txt` — added `anthropic>=0.92` (optional, lazy-loaded)
- Admin CLI (`scripts/manage.py`) — added tuning monitoring support
- API server (`cloud/server.py`) — new tuning endpoints (`GET /api/v1/admin/tuning`, `POST /api/v1/admin/tuning/rollback`)

---

## Node Data Model

> This is the most important architectural document in the repo. The `nodes` table
> is the single source of truth the AI scheduler reads when deciding which telescope
> points where. Every column exists for a reason.

### Location (observability)

| Column | Type | Used for |
|--------|------|---------|
| `latitude`, `longitude` | REAL | Night window, altitude curve, airmass, moon angle |
| `elevation` | REAL | Pressure correction for airmass calculation |
| `utc_offset_hours` | REAL | Twilight times in local clock; auto-updated from heartbeat |
| `city`, `country` | TEXT | Display, geographic diversity tracking |
| `light_pollution_mpsas` | REAL | Sky background noise; faint-target magnitude limit adjustment |
| `bortle` | INT | Human-readable sky quality label; auto-fetched on registration |
| `horizon_mask` | JSON `[[alt,az],…]` | Local obstructions — the scheduler won't assign targets behind trees or buildings |

### Hardware: Telescope (capability)

| Column | Type | Used for |
|--------|------|---------|
| `tier` | INT 1–3 | 1=Seestar broadband, 2=Filtered BVRI, 3=Spectroscopy; controls which target classes get assigned |
| `telescope_model` | TEXT | Display; future per-model aperture lookup |
| `aperture_mm` | REAL | Faint magnitude limit, integration time scaling |
| `focal_length_mm`, `fov_deg`, `pixel_scale_arcsec` | REAL | Field-of-view matching for targets with extended structure |
| `mount_type` | TEXT `alt_az\|equatorial` | Equatorial mounts can take longer sub-frames; alt-az has field rotation |
| `max_exposure_s` | REAL | Sub-frame cap, primarily field-rotation limited for alt-az mounts |
| `mag_bright_limit`, `mag_faint_limit` | REAL | Direct inputs to the `brightness_match()` scorer |
| `min_altitude_deg` | REAL | Hard floor — targets below this altitude are never scheduled for this node |

### Hardware: Camera & Filters (photometry quality)

| Column | Type | Used for |
|--------|------|---------|
| `camera_model` | TEXT | Display; future per-camera calibration frames |
| `cooled_camera` | BOOL | TEC cooling lowers read noise → better faint limit; captured but not yet scored |
| `filter_set` | JSON `["CV","B","V","R","I"]` | Multi-band targets require a matching filter; broadband-only Seestars get `["CV"]` |
| `filters` | TEXT | Legacy comma-separated; kept for compatibility with older node agents |

### Hardware: Autonomy (unattended operation reliability)

These four flags determine whether a node can run an entire night without human
intervention. The scheduler does not assign a numeric penalty for missing them today,
but nodes with all four tend to produce better `clear_nights_30d` and higher
`aavso_accepted` counts — which flows directly into their `reliability_score` over time.

| Column | Why it matters for the scheduler |
|--------|----------------------------------|
| `has_dew_heater` | Prevents lens fogging in humid conditions. Without one, a node fails silently — images blur, plate-solving fails, the whole night is wasted with no measurement data. |
| `has_power_mgmt` | Smart power box lets the node agent remotely cycle a hung Seestar. Without it, a crashed scope means a missed assignment with no recovery until a human intervenes. |
| `has_enclosure` | Dome or minidome — the node can observe through light rain, wind, and heavy dew. Dramatically improves `clear_nights_30d` which feeds the reliability score. |
| `has_ups` | Brief power cuts don't kill the night. Especially relevant for nodes in regions with unstable grid power — protects both `clear_nights_30d` and `aavso_acceptance_rate`. |

### Scheduler Hints (operator-provided)

| Column | Example |
|--------|---------|
| `scheduling_notes` | `"south horizon blocked past az 195"`, `"WiFi drops after midnight"`, `"struggles above airmass 2.5"` — free text surfaced to the scheduler and admin |
| `preferred_targets` | `["SN","nova"]` — scheduler gives this node a soft preference for these target types when scores are close |

### Performance Metrics (recomputed nightly)

These columns are **never set by the node agent**. The nightly maintenance loop calls
`registry.refresh_all_performance()` which recomputes them from the `measurements`
table. They represent ground truth — what a node has actually delivered, not what it
claims it can deliver.

| Column | Formula | What it tells the scheduler |
|--------|---------|----------------------------|
| `total_observations` | `COUNT(*)` in measurements | Activity level; nodes with < 10 observations stay at neutral 0.50 reliability |
| `aavso_accepted` | `SUM(aavso_submitted = 1)` | How much science has been delivered — the ultimate output metric |
| `aavso_rejected` | Outlier count with good quality flag | How often this node produces data the network can't corroborate |
| `mean_uncertainty` | `AVG(uncertainty)` for non-poor measurements | Typical photometric precision; AAVSO quality ceiling is 0.30 mag |
| `mean_fwhm` | `AVG(fwhm)` for non-poor measurements | Typical image quality / seeing; proxy for focus and atmospheric stability |
| `clear_nights_30d` | `COUNT(DISTINCT date)` in last 30 days | How often the node actually observes; captures weather + hardware reliability combined |
| `outlier_rate` | `outliers / total` | Cross-validation disagreement rate; high values signal that data can't be trusted |

### Reliability Score

`reliability_score` is a composite 0..1 value stored per-node and applied as a
**multiplier** on every scheduler score for that node:

```
total_score = theoretical_score × (0.5 + 0.5 × reliability_score)
```

| reliability | multiplier | meaning |
|-------------|-----------|---------|
| 1.0 | ×1.00 | proven node — no penalty |
| 0.5 | ×0.75 | new or data-sparse node — slight preference for proven peers |
| 0.0 | ×0.50 | persistently poor data — still gets some assignments (floor prevents starvation) |

**Formula** (computed in `cloud/registry.py` → `refresh_node_performance()`):
```
reliability = 0.40 × aavso_acceptance_rate
            + 0.25 × (1 − outlier_rate)
            + 0.20 × min(1, clear_nights_30d / 30)
            + 0.15 × precision_factor

precision_factor = max(0, 1 − mean_uncertainty / 0.30)
```

New nodes (< 10 observations) start at **0.50** and converge toward their true value
over roughly 20–30 observations. The formula is intentionally multi-dimensional: a
node can't inflate its score by gaming one metric — it has to deliver across all four
simultaneously.

---

## Activation Code System

Every node is registered with a **Node Activation Code** (`BS-YYYY-XXXXXXXX`). This
is the link between a member's account, their telescope hardware, and the cloud
scheduler. It is also the data that seeds the `nodes` row — every field group
described above gets populated from this first registration.

```
Member signs up on the website
         ↓
Member requests a code (POST /api/v1/me/activation-code)
or admin bulk-generates codes (scripts/manage.py generate-code)
         ↓
Code issued: BS-2026-ABCD1234  (expires in 90 days by default)
         ↓
Member downloads the installer for their OS
Installer prompts for the activation code during setup
Writes it to config.yaml:
    cloud:
      activation_code: 'BS-2026-ABCD1234'
         ↓
Node Agent starts for the first time
First heartbeat sends POST /api/v1/nodes/register
with the activation code + full hardware payload
         ↓
Cloud validates the code (not expired, not previously used)
Cloud auto-generates node_id (e.g. node_a3f9b2c1) + api_key
Cloud populates the nodes row with all hardware fields
Cloud links node to the member's account (node_members table)
Cloud marks code used (activation_codes.used_at = now)
         ↓
Node saves node_id + api_key to data/cloud_state.json
Subsequent API calls use node_id + api_key directly
The activation code is never sent again
```

**Codes are single-use.** A shared or leaked code lets someone else claim your node
registration slot. If a code is compromised before use, generate a replacement.

**Generating codes:**
```bash
# Admin CLI — bulk generation (server-side)
python3 scripts/manage.py generate-code --count 5 --expires-days 90

# Pre-link codes to a specific member account
python3 scripts/manage.py generate-code --count 1 --user u_abc123

# Member self-service via the API
curl -X POST https://cloud.boundlessskies.org/api/v1/me/activation-code \
     -H "Authorization: Bearer <your_token>"
# → {"code": "BS-2026-ABCD1234", "expires_at": "2026-09-08T..."}
```

---

## Quick Start — Node Agent (Development)

```bash
# 1. Clone and set up
git clone https://github.com/boundlessskies/node_v1 && cd node_v1-main
python3 -m venv venv && source venv/bin/activate
pip install flask pyyaml numpy astropy pillow requests watchdog photutils astroquery pyongc

# 2. Install ASTAP (required for plate-solving and photometry)
#    https://www.hnsky.org/astap.htm — install binary + G18 star catalogue

# 3. Configure (for a dry run, only these are required)
nano config.yaml
#    observatory.latitude / longitude
#    image_watcher.watch_path (Seestar SMB share mount point)
#    cloud.url + cloud.activation_code

# 4. Run (dev mode — auto-restarts src/dashboard.py on file changes)
python3 main.py
# Dashboard: http://localhost:5173
```

### First-Time Dry Run (no hardware)

Set these in `config.yaml` to test the dashboard without a Seestar:

```yaml
cloud:
  enabled: false

image_watcher:
  enabled: false

photometry:
  enabled: false

aavso:
  dry_run: true
```

Then `python3 main.py`. Everything in the dashboard works except hardware
control. Object catalog, config editor, logs, and API endpoints are all live.

---

## Quick Start — Cloud Server (Development)

```bash
cd cloud/
pip install flask pyyaml requests

# Optional: enable Claude weight tuning (requires ANTHROPIC_API_KEY env var)
export ANTHROPIC_API_KEY="sk-..."
pip install anthropic

# Edit cloud/config.yaml:
#   aavso.observer_code: MXXX
#   aavso.username / aavso.password
#   server.admin_key
#   tuning.enabled: true  (optional, defaults to false)

python3 -m cloud.main
# API: http://localhost:8800
# Health check: http://localhost:8800/api/v1/health
```

---

## Marketing Site (`website/tour.html`) — Running the Founding Network Experience

The `tour.html` is a standalone, zero-dependency HTML+CSS+JS site that tells the Boundless Skies story and drives member acquisition. It pulls **live data from the cloud API** but renders gracefully even when the API is offline.

### Quick Start — Website Dev

```bash
# Terminal 1: Cloud API server (provides data)
cd cloud/
PYTHONPATH="$PWD:$PWD/src" python3 -m cloud.main
# → serves on http://localhost:8800

# Terminal 2: Website (static file server)
cd website/
python3 -m http.server 4180 --directory .
# → open http://localhost:4180/tour.html
#   or just http://localhost:4180 (if index.html points to tour.html)
```

### What the Site Shows

| Section | Data source | Fallback |
|---------|-------------|----------|
| **Founding seats** (7 / 100) | `/api/v1/network/status` → `founded_member_count` | Static "07 / 100" |
| **Online nodes** | `/api/v1/network/status` → `nodes_online` | "7 online" |
| **AAVSO submissions** | `/api/v1/network/status` → `aavso_submitted` | "—" |
| **Network reliability** | `/api/v1/network/status` → highest `reliability_score` | Sensible defaults |
| **Live example** (SS Cygni) | `/api/v1/lightcurves/SS_Cyg` → recent observations | Empty chart |
| **Network map dots** | `/api/v1/network/status` → node locations | No pins shown |
| **Sky background** | Aladin Lite + RA/Dec per section | Star field rendered |

### Architecture

```
tour.html
├─ Embedded CSS (glassmorphism, dark theme, responsive)
├─ Aladin Lite integration (CDN, sky visualization per scroll section)
├─ API calls (fetch from cloud server, graceful fallback if offline)
├─ Canvas visualization (Earth globe rotation, observer animations)
└─ Email signup form + donation flow (JavaScript state, no backend)
```

**Browser compatibility:** Modern browsers (Chrome 90+, Firefox 88+, Safari 14+). Fallback text renders on older clients.

### Seeding Demo Data

The site looks better with realistic stats. Populate the database once:

```bash
python3 scripts/seed_demo.py --wipe
# Creates 23 nodes across 14 countries + light curve for SS Cyg
# Nodes are "online" for 15 minutes after seeding — re-run to refresh
```

Then refresh your browser and watch the stats, map, and light curve populate.

---

## First AAVSO Submission — Step by Step

```bash
# 1. Set AAVSO credentials in cloud/config.yaml
#    aavso.observer_code: MXXX
#    aavso.username: ...
#    aavso.password: ...

# 2. Verify the Extended File Format looks correct (no live POST)
python3 scripts/manage.py check-aavso
# Prints a formatted test record with your observer code filled in

# 3. Check that the network has active nodes
python3 scripts/manage.py status

# 4. Trigger alert ingestion (or wait for the hourly background loop)
python3 scripts/manage.py ingest

# 5. Run a night's observations with a connected node

# 6. Preview the AAVSO batch before committing (dry run)
python3 scripts/manage.py batch
# Prints the exact Extended File Format that would be POSTed to WebObs

# 7. Submit live (calls AAVSO WebObs API)
python3 scripts/manage.py submit

# 8. Verify
python3 scripts/manage.py status
```

**Success criterion (Phase 0):** One observation accepted by AAVSO with magnitude
agreeing within 0.15 mag of the known value for the target.

---

## Weight Tuning Monitor — Claude-Powered Observability Optimization

The scoring engine is built on **six observability sub-weights** that balance how heavily each factor influences target assignment:

```
{
  "light_pollution":  0.20,   Weight of sky brightness on assignment
  "weather":          0.25,   Clearness likelihood (historical)
  "moon":             0.15,   Lunar illumination + position
  "airmass":          0.15,   Atmospheric extinction + target altitude
  "window":           0.15,   Hours the target is visible each night
  "telescope":        0.10,   Hardware capability match to target
}
```

By default these are **static**. When `tuning.enabled: true` in `cloud/config.yaml`, a nightly monitor wakes after maintenance and:

1. **Gathers evidence** from the last N nights of observations
   - Measures ingested, quality flags, AAVSO acceptance rate
   - Correlates outcomes with the factors above
   - Identifies which weights may be misaligned

2. **Proposes adjustments** (Claude API call)
   - Reads the evidence brief + current weights
   - Returns proposed values for each sub-weight
   - Reasoning is logged but not applied automatically

3. **Applies with safeguards** (procedural)
   - Clamps each weight to ±5% change per night (trust-region limit)
   - Renormalizes to sum 1.0
   - Persists to the database with full audit trail
   - Notifies admins via the API if changes exceed 2% on any weight

The **hot path** (alert ingestion, scoring, planning) remains 100% procedural — no Claude calls, no latency added. Scoring reads live weights from `tuning_state` table on every run via `scoring.score_all()`, so a change takes effect on the next rescore with no restart.

**Key points:**
- Only the advisory monitor calls Claude; schedulers and scorers use fixed formulas
- Tuning is **opt-in** and ships disabled; behavior is identical to static weights when disabled
- All weight changes are audited in `weight_history` table — rollback is a single API call
- Configuration lives in both `config.yaml` (seed values) and the database (live values)

### Tuning Configuration

```yaml
tuning:
  enabled: false                    # set true to enable Claude-powered tuning
  model: claude-opus-4-8            # Claude model for proposals (impacts cost/latency)
  lookback_days: 14                 # analyze the last N nights for evidence
  max_delta_per_night: 0.05         # trust-region clamp (±5% per weight per night)
  min_nights_for_tuning: 7          # don't tune until ≥ 7 nights of data exist
  min_measurements: 30              # require ≥ 30 measurements to tune
  min_weight_change: 0.005          # skip notify if no weight moves > 0.5%

# Seed values — used as defaults if tuning is disabled or on fresh install
scoring:
  observability_weights:
    light_pollution: 0.20
    weather: 0.25
    moon: 0.15
    airmass: 0.15
    window: 0.15
    telescope: 0.10
```

### Monitoring the Tuning Monitor

```bash
# Check current live weights
curl -H "X-Admin-Key: your-admin-key" \
     https://cloud.boundlessskies.org/api/v1/admin/tuning

# Response:
# {
#   "active_weights": { … },
#   "last_tuning_run": "2026-06-15T02:30:00Z",
#   "weight_history": [ … ],
#   "recent_changes": [ … ]
# }

# Rollback to a prior weight set (manual safety valve)
curl -X POST -H "X-Admin-Key: your-admin-key" \
     -H "Content-Type: application/json" \
     -d '{"rollback_to_id": 5}' \
     https://cloud.boundlessskies.org/api/v1/admin/tuning/rollback
```

---

## Admin CLI (`scripts/manage.py`)

```
python3 scripts/manage.py [--config PATH] <command>

status          Node count, measurement queue, AAVSO batch history, config check
ingest          Trigger alert ingestion + scoring + plan regeneration
batch           Dry-run preview of the pending AAVSO batch (does not POST)
submit          Live AAVSO submission (ignores config dry_run setting)
check-aavso     Verify credentials config, print a formatted test observation
nights          Generate/backfill night summaries for all active nodes

generate-code   Create activation codes
  --count N       Number of codes to generate (default 1, max 100)
  --user USER_ID  Pre-link to a specific member (optional)
  --expires-days  Days until expiry (default 90)
```

---

## Building Installers

```bash
pip install pyinstaller

# Auto-detect platform and build installer
python3 build/build.py

# Bundle only (PyInstaller one-file, no OS installer wrapper)
python3 build/build.py --bundle-only

# Outputs:
#   Windows  → dist/BoundlessSkiesNode-Setup.exe   (requires NSIS + NSSM on PATH)
#   macOS    → dist/BoundlessSkiesNode-X.Y.Z.pkg
#   Linux    → dist/BoundlessSkiesNode-linux-x86_64
```

**Windows (NSIS + NSSM)**
- Installer prompts for activation code; writes it to `config.yaml`
- Registers the agent as a Windows service via NSSM; auto-starts at boot
- Calls `powercfg /change standby-timeout-ac 0` to disable AC idle sleep

**macOS (.pkg)**
- Installs to `/Applications/BoundlessSkiesNode/`
- Installs a launchd plist → `/Library/LaunchDaemons/` (auto-start at boot)
- Calls `pmset -c sleep 0` to disable AC idle sleep
- Data: `/Library/Application Support/BoundlessSkies/NodeAgent/`

**Linux (systemd)**
```bash
# One-line install
curl -sSL https://boundlessskies.org/install.sh | sudo bash --code BS-2026-XXXXXXXX

# Or manually
sudo bash build/linux/install.sh --code BS-2026-XXXXXXXX
```
- Creates `boundlessskies` service user; installs systemd unit; enables at boot
- Masks `sleep.target`, `suspend.target`, `hibernate.target`, `hybrid-sleep.target`

---

## Cloud API Reference

### Node-authenticated (headers: `X-Node-Id` + `X-Api-Key`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/nodes/register` | First-boot registration; accepts `activation_code` in JSON body |
| POST | `/api/v1/nodes/heartbeat` | 60-second keepalive with optional conditions JSON |
| GET  | `/api/v1/nodes/me` | Own registry entry (all columns including performance metrics) |
| GET  | `/api/v1/plan` | Tonight's observation plan |
| POST | `/api/v1/measurements` | Upload a photometry result |
| POST | `/api/v1/images` | Upload raw FITS (retained 14 days) |
| GET  | `/api/v1/interrupts` | High-priority target alerts |

### Member-authenticated (header: `Authorization: Bearer <token>`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/auth/register` | Create member account |
| POST | `/api/v1/auth/login` | Authenticate; returns bearer token |
| GET  | `/api/v1/me` | Member profile |
| GET  | `/api/v1/me/nodes` | My registered nodes |
| POST | `/api/v1/me/nodes/<id>` | Claim an existing node by api_key |
| POST | `/api/v1/me/activation-code` | Generate a personal activation code |
| GET  | `/api/v1/me/observations` | Observation history |
| GET  | `/api/v1/me/stats` | Cumulative totals (observations, AAVSO accepted, targets covered) |
| GET  | `/api/v1/me/nights` | Night-by-night summaries |
| GET  | `/api/v1/me/notifications` | Notification inbox |
| POST | `/api/v1/me/notifications/<id>/read` | Mark a notification read |
| PUT  | `/api/v1/me/notifications/prefs` | Update notification preferences |

### Public (no auth required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/targets` | Active target queue with current composite scores |
| GET | `/api/v1/lightcurves/<name>` | Historical photometry for a named target |
| GET | `/api/v1/network/status` | Live node count, submission totals |
| GET | `/api/v1/health` | Uptime check |

### Admin (header: `X-Admin-Key`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/admin/ingest` | Trigger alert ingestion + scoring |
| POST | `/api/v1/admin/replan` | Trigger rescoring + plan regeneration |
| POST | `/api/v1/admin/activation-codes` | Generate codes in bulk |
| POST | `/api/v1/interrupts` | Broadcast a high-priority target interrupt to nodes |
| GET | `/api/v1/admin/tuning` | Get current active weights, history, and last run timestamp |
| POST | `/api/v1/admin/tuning/rollback` | Rollback to a previous weight set by history ID |

---

## Database Schema Updates

### New Tables (Phase 0.5+)

| Table | Purpose | Columns |
|-------|---------|---------|
| `tuning_state` | Active observability sub-weights | `id`, `obs_weights` (JSON), `updated_at` |
| `weight_history` | Audit trail of all weight changes | `id`, `timestamp`, `old_weights`, `new_weights`, `reason`, `claude_reasoning` |

### Schema Changes to Existing Tables

| Table | Change | Notes |
|-------|--------|-------|
| `nodes` | Added `filters` column | Previously missing, needed for multi-band target assignment |

**Migration:** All schema updates are applied automatically on cloud server startup via `db.py` migration system.

---

## Photometry Pipeline (`src/photometry.py`)

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
    "fwhm":             3.8,
    "snr":              52.0,
    "comparison_stars": 9,
    "quality_flag":     "good",
    "node_id":          "node_001",
    "zero_point":       22.413,
    "zp_scatter":       0.028,
    "fits_file":        "seestar_image.fits",
}
```

---

## Architecture Overview

```
src/dashboard.py (Flask, port 5173)
  │
  ├─ API endpoints (telescope, camera, schedule, photometry, config, logs, …)
  │
  ├─ SafetyManager (daemon thread)
  │   heartbeat monitor, reconnect, dawn parking, horizon mask, SIGTERM handler
  │
  ├─ ImageWatcher (daemon thread, when enabled)
  │   OS filesystem events → debounce → photometry pipeline
  │
  ├─ Photometry pipeline (photometry.py)
  │   WCS → comp stars → aperture phot → differential → AAVSO submission
  │
  ├─ CloudCommunicator (daemon threads, when cloud.enabled)
  │   auto-register → heartbeats → plan polling → measurement upload (retry queue)
  │
  └─ DeviceManager → AlpacaClient (HTTP/JSON)
       Telescope, Camera, Focuser, FilterWheel

cloud/ (Flask, port 8800)
  │
  ├─ Background loops
  │   alert-ingest → scoring → (optionally) replan  every 60 min
  │   replan                                         every 120 min
  │   aavso-batch                                    every 360 min
  │   weight-tuning (Claude advisory monitor)        daily
  │   maintenance (image pruning, night summaries,   daily
  │                performance refresh, LP refresh)
  │
  ├─ nodes table — the AI scheduler's view of each telescope
  │   location → observability windows, airmass, moon
  │   hardware → what the scope can see and how long it can expose
  │   autonomy → likelihood of completing a night unattended
  │   performance → what it has actually delivered to AAVSO
  │   reliability_score → multiplier on every (target, node) score pair
  │
  └─ tuning_state + weight_history tables (when tuning enabled)
      active observability sub-weights, audit trail of all changes
```

---

## Configuration Reference (`config.yaml`)

### Cloud Configuration (when `enabled: true`)

```yaml
cloud:
  enabled: true
  url: https://cloud.boundlessskies.org
  activation_code: ''      # BS-YYYY-XXXXXXXX from your account page; used once on first boot
  auto_run_plans: true     # automatically execute observation plans from the cloud
```

### Tuning (Claude weight monitor — requires Anthropic API key in environment)

**Note:** See [Weight Tuning Monitor](#weight-tuning-monitor--claude-powered-observability-optimization) section above for detailed documentation.

```yaml
tuning:
  enabled: false                    # set true to enable Claude-powered observability weight tuning
  model: claude-opus-4-8            # Claude model for advisory proposals
  lookback_days: 14                 # analyze the last N nights of observations
  max_delta_per_night: 0.05         # clamp changes to ±5% per weight per night
  min_nights_for_tuning: 7          # require ≥ 7 nights of historical data
  min_measurements: 30              # require ≥ 30 measurements to enable tuning
  min_weight_change: 0.005          # skip notifications if max change < 0.5%
```

**Environment variable:** `ANTHROPIC_API_KEY` must be set for tuning to function.

### Photometry

```yaml
photometry:
  enabled: false           # set true to auto-run on each new FITS file
  node_id: "node_001"
  filter_name: "CV"
  gain: 1.0                # e-/ADU
  read_noise: 5.0
  target:
    name: ""               # leave blank to use FITS header values
    ra_deg: ~
    dec_deg: ~
  astap_path: "astap"
  astap_search_radius: 10
  aperture_factor: 2.5
  annulus_inner: 4.0
  annulus_outer: 6.0
  field_radius: 0.5
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
  name: ""
  latitude: ~              # decimal degrees N
  longitude: ~             # decimal degrees E (negative = West)
  elevation: 0.0           # metres
  telescope: "ZWO Seestar S50"
  instrument: "ZWO Seestar S50 IMX462"
  observer: ""
```

### Cloud connection

```yaml
cloud:
  enabled: true
  url: https://cloud.boundlessskies.org
  activation_code: ''      # BS-YYYY-XXXXXXXX from your account page; used once on first boot
  auto_run_plans: true
```

### Safety

```yaml
safety:
  enabled: true
  disconnect_timeout: 600
  heartbeat_interval: 30
  reconnect_attempts: 3
  reconnect_delay: 10
  park_at_dawn: true
  dawn_type: astronomical  # astronomical (-18°), nautical (-12°), civil (-6°)
  observer:
    latitude: 0.0
    longitude: 0.0
```

### Image Watcher

```yaml
image_watcher:
  enabled: false
  watch_path: "/mnt/seestar"
  debounce_delay: 2.0
```

---

## Troubleshooting

### "No ALPACA servers found"
- Verify the Seestar is powered on and on the same subnet
- Some routers block UDP broadcast; check with `tcpdump -i en0 udp port 32227`
- macOS may require allowing Python in System Preferences → Privacy & Security

### "ErrorNumber 1: Device not connected"
The ALPACA server responded but the device isn't initialised inside the Seestar app yet. Open the Seestar app first.

### Plate solve fails
- ASTAP not in PATH — set `photometry.astap_path` to the full binary path
- G18 star catalogue not installed — download from the ASTAP site
- Increase `astap_search_radius` if the image is very wide field

### AAVSO submission rejected
Check `aavso_submissions/YYYY-MM-DD/*_response.txt` for the raw WebObs error message. Common causes: invalid observer code, target name not in AAVSO VSX, date out of range. Use `dry_run: true` to validate format before a live submission.

### Photometry quality always "poor"
- Low SNR: increase exposure time or check sky conditions
- Too few comparison stars: Gaia DR3 fallback should help for targets outside AAVSO VSX coverage
- Large ZP scatter: poor seeing or clouds; scatter contributes directly to uncertainty

### "Host is down" despite Seestar app showing connected
The ALPACA HTTP server has wedged while the core firmware remains operational. The Seestar app talks to the firmware directly; ALPACA is a separate HTTP service that can fail independently. Recovery: **Settings → Restart** in the Seestar app (a full firmware reboot, not just app relaunch).

### SafetyManager stuck in unsafe state after reconnect
The SafetyManager lost contact for longer than `disconnect_timeout` and triggered emergency park. The unsafe state does not self-clear. Recovery: restart `dashboard.py`. The manager initialises safe on startup and re-attaches cleanly.

Root causes: Seestar WiFi drop, Seestar app crash, host machine sleep. Prevention: increase `disconnect_timeout`, fix WiFi signal quality, ensure `sleep_prevention` is active.

### "ErrorNumber 1024: Method Unpark is not implemented"
The Seestar S50 ALPACA driver does not implement `Unpark`. Unpark from within the Seestar app. The dashboard Unpark button has no effect on this device.

### "CoverCalibrator connect failed: 400 Client Error"
Non-fatal — no cover calibrator device is configured at ALPACA index 0. Suppress with `devices.covercalibrator.enabled: false` in `config.yaml`.

### Dawn parking not triggering
- `observer.latitude` and `observer.longitude` must be non-zero
- At mid-latitudes in summer the sun may not reach −18° — try `dawn_type: nautical`

### Weight-tuning monitor not running
- Check that `ANTHROPIC_API_KEY` is set in the environment: `echo $ANTHROPIC_API_KEY`
- Verify `tuning.enabled: true` in `cloud/config.yaml`
- Tuning is a daily task — runs during the maintenance window, typically 2–3 AM UTC
- Check cloud server logs: `cloud.tuning` logger will show evidence gathered and Claude responses
- Insufficient data: tuning requires at least `min_measurements` (default 30) and `min_nights_for_tuning` (default 7) of observations

### Weight changes seem to have no effect on plan
- Plans are built during the `replan` loop (every 120 min); a weight change takes effect on the **next** replan, not retroactively
- To see weights take effect immediately, trigger a manual rescore: `curl -X POST -H "X-Admin-Key: ..." http://localhost:8800/api/v1/admin/replan`
- Check the `GET /api/v1/admin/tuning` response to confirm weights were actually updated

### Claude API errors during tuning
- **429 (rate limited):** reduce frequency by increasing `tuning.lookback_days` or temporarily disabling
- **401 (auth failed):** verify `ANTHROPIC_API_KEY` is correct and has quota remaining
- **Content policy violations:** evidence brief triggers a refusal (rare); tuning skips apply and notifies in logs

---

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Node Agent | Python 3.10+ / Flask | Runs Windows, macOS, Linux; pip only |
| Telescope control | ALPACA REST via seestar_alp | Vendor-neutral; replaceable in Phase 4 |
| Plate solving | ASTAP + G18 catalogue | Fast, offline, accurate on Seestar images |
| Image stacking | NumPy + RANSAC | Sub-pixel alignment, no external binaries |
| Photometry | astropy + custom aperture code | Differential against AAVSO/Gaia comp stars |
| Cloud server | Python / Flask | Upgrade to FastAPI + async at Phase 2 scale |
| Scoring weights | Claude API (opt-in) | Advisory monitor for observability tuning; hot path stays procedural |
| Database | SQLite (WAL mode) | Zero-config; migrate to Postgres at ~50 nodes |
| Member auth | PBKDF2-SHA256 + bearer tokens | 260K rounds, per-user salt, hashed token storage |
| Sleep prevention | OS-native APIs | `SetThreadExecutionState` (Win), `caffeinate` (Mac), `systemd-inhibit` (Linux) |
| Packaging | PyInstaller one-file | NSIS (Win), pkgbuild/productbuild (Mac), systemd install.sh (Linux) |
| Marketing site | HTML/CSS/JavaScript (static) | `tour.html` — interactive scrollytelling, Aladin Lite sky vis, live API data |
| Member portal | Flutter + HTML/CSS/JS (Phase 2) | Phase 1 scaffolding in `website/future/`; accessibility-first design |

---

## Further Reading

- [ALPACA Specification](https://ascom-standards.org/Developer/Alpaca.pdf)
- [AAVSO Extended File Format](https://www.aavso.org/aavso-extended-file-format)
- [AAVSO WebObs](https://www.aavso.org/webobs)
- [ASTAP plate solver](https://www.hnsky.org/astap.htm)
- [photutils documentation](https://photutils.readthedocs.io/)
- [Astroquery (Gaia / AAVSO VSP)](https://astroquery.readthedocs.io/)

---

*Boundless Skies — The night sky belongs to everyone.*
*Founded 2025.*
