# NODE v1 — ALPACA Agent with Safety Manager

A robust Python agent for automated observatory control via the ALPACA protocol, with an integrated **Safety Manager** that monitors telescope connectivity, parks automatically at dawn, and handles graceful shutdown on connection loss or system signals.

**What is ALPACA?**  
ALPACA (Astronomy Low-level Control And Automation) is an open, standardized HTTP/JSON protocol defined by the [ASCOM Initiative](https://ascom-standards.org/). It allows astronomy software to talk to mounts, cameras, focusers, and other equipment over a network without worrying about proprietary drivers or serial ports.

**What is the Safety Manager?**  
The Safety Manager is a protective watchdog that runs continuously in the background, ensuring the telescope is safely stowed whenever the system is at risk:
- **Heartbeat monitoring**: Pings the telescope every 30 seconds; auto-parks if unreachable for >10 minutes
- **Reconnect logic**: Automatically attempts to re-establish lost connections with retry backoff
- **Dawn parking**: Calculates solar elevation and parks the telescope at astronomical dawn (or nautical/civil dawn if configured)
- **Graceful signals**: On `SIGTERM` or `SIGINT` (Ctrl-C), parks the mount before exiting
- **Safe-to-operate check**: Other modules can query `is_safe()` to gate operations and avoid damage

---

## Prerequisites

- **Python 3.8+** (tested on 3.10+)
- **A networked ALPACA server** running on the same LAN
  - Examples: ASCOM Remote, Stellarmate, KStars via INDI, or a hardware simulator
  - The server must be reachable via UDP broadcast (port 32227) and HTTP (typically port 11111)
- **macOS/Linux/Windows** (no platform-specific code; tested on macOS)

---

## Installation

1. **Clone or navigate into this directory:**
   ```bash
   cd alpaca_test
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   This installs:
   - `requests` — HTTP client for ALPACA communication
   - `pyyaml` — config file parsing
   - `flask` — web server framework
   - `watchdog` — file system event monitoring (for ImageWatcher)
   - `astropy` — FITS file parsing (for ImageWatcher)
   - `numpy` — numerical array operations for image processing
   - `Pillow` — image display and format handling

3. **Verify imports work:**
   ```bash
   python3 -c "import alpaca; print('OK')"
   ```

---

## Quick Start

### 1. Start Your ALPACA Server

Before running the skeleton, ensure an ALPACA server is active on your network. For testing without hardware:

- **ASCOM Remote** (Windows/macOS): Free simulator in ASCOM hub
- **Stellarmate** (Raspberry Pi/SBC): Includes simulators, IP-based
- **KStars + INDI** (Linux/macOS): Via Ekos, broadcast on LAN
- **ASCOM.Remote.Client** (Windows): Simulator devices included

The server must advertise itself via UDP broadcast on port 32227 and listen for HTTP requests (usually port 11111).

### 2. Configure the Skeleton

Edit `config.yaml` to enable/disable devices and set slew targets:

```yaml
alpaca:
  discovery_port: 32227        # UDP broadcast port (ALPACA standard)
  discovery_timeout: 5         # Wait up to 5 seconds for responses
  api_version: 1               # ALPACA API v1

devices:
  telescope:
    enabled: true              # Enable mount commands
    device_number: 0
  camera:
    enabled: true              # Enable imaging
    device_number: 0
  focuser:
    enabled: false             # Disable if not present
    device_number: 0
  filterwheel:
    enabled: false
    device_number: 0

telescope:
  slew_ra: 10.6833             # RA in decimal hours (e.g., Andromeda at 10h 41m)
  slew_dec: 41.2692            # Dec in decimal degrees
  tracking_rate: 0             # 0=Sidereal, 1=Lunar, 2=Solar, 3=King

camera:
  exposure_duration: 1.0       # Seconds
  binning: 1                   # 1x1, 2x2, etc.

safety:
  enabled: true                                    # Enable the Safety Manager
  disconnect_timeout: 600                          # Park after 10 min without contact
  heartbeat_interval: 30                           # Ping every 30 seconds
  reconnect_attempts: 3                            # Try 3 times before giving up
  reconnect_delay: 10                              # 10 seconds between reconnect attempts
  park_at_dawn: true                               # Park at astronomical dawn
  dawn_type: astronomical                          # astronomical (-18°), nautical (-12°), civil (-6°)
  observer:
    latitude: 37.7749                              # ← SET TO YOUR LOCATION
    longitude: -122.4194                           # (negative = West)

logging:
  level: INFO                  # DEBUG, INFO, WARNING, ERROR
  format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
```

**Safety parameters:**
- **`disconnect_timeout`**: After this many seconds without telescope contact, park the mount (prevents damage if the network drops). Default 600 s (10 minutes).
- **`heartbeat_interval`**: Ping the telescope this often. Default 30 s; decrease for faster detection of dropouts (at the cost of more network traffic).
- **`reconnect_attempts` / `reconnect_delay`**: On heartbeat failure, retry this many times with this delay between attempts. E.g., 3 attempts × 10 s = up to 30 s spent trying to reconnect before starting the disconnect timer.
- **`park_at_dawn`**: Enable automatic parking at dawn. Disable if you want to run observations through twilight.
- **`dawn_type`**: Which solar elevation threshold defines dawn:
  - `astronomical`: −18° (civil twilight ends; stars invisible)
  - `nautical`: −12° (practical horizon visible, star count drops sharply)
  - `civil`: −6° (sunrise/sunset area, horizon clearly visible)
- **`observer.latitude` / `longitude`**: Your site coordinates (decimal degrees). **Set these for accurate dawn detection.** If left at `0/0`, dawn checking is disabled.

### 3. Run the Dashboard

```bash
python dashboard.py
```

**What happens:**

1. **Flask web server** starts on `http://localhost:5173`
2. **Safety Manager** starts (registers OS signal handlers, begins monitoring)
3. **Browser opens** automatically to the dashboard
4. **Automatic sequence** begins:
   - Discovers ALPACA servers on the LAN
   - Connects to the first server (telescope, camera, etc.)
   - Executes: unpark → track → slew → expose → park
   - All actions show live in the dashboard with timestamps and coordinates
5. **Concurrent monitoring**: While the sequence runs, the Safety Manager watches for:
   - Connection drops (auto-reconnects, parks after 10 minutes if unreachable)
   - Astronomical dawn (parks automatically at −18° solar elevation)
   - User abort or system signals (parks before exiting)

**Dashboard features:**
- **Live sequence progress** with step indicators (Discover → Connect → Unpark → Slew → Hold → Expose → Park)
- **Telescope state**: real-time RA/Dec coordinates, slewing status, tracking, park status, custom slew coordinate input
- **Camera state**: exposure mode, image readiness, captured image display
- **Safety status**: ✓ SAFE or ✗ UNSAFE (with reason), sun elevation, heartbeat indicator
- **Live log stream**: every action logged with timestamps and colours
- **Image watcher**: optional file monitoring for Seestar FITS files with automatic ingestion
- **Manual controls**: Discover, Run Sequence, Abort, custom slew coordinate entry buttons

**Sample dashboard log:**
```
14:35:22 [INFO] alpaca.discovery: Broadcasting ALPACA discovery on port 32227
14:35:24 [INFO] alpaca.discovery: Discovered ALPACA server at 192.168.1.100:11111
14:35:24 [INFO] dashboard: Connecting to ALPACA server 192.168.1.100:11111
14:35:24 [INFO] alpaca.telescope: Telescope connected: Simulator Telescope
14:35:24 [INFO] alpaca.camera: Camera connected: Simulator Camera
14:35:24 [INFO] image_watcher: Image watcher started, monitoring /mnt/seestar
14:35:25 [INFO] alpaca.telescope: Telescope unparked
14:35:25 [INFO] alpaca.telescope: Tracking set to True
14:35:25 [INFO] alpaca.telescope: Slewing to RA=10.6833 h  Dec=41.2692 °
14:35:27 [INFO] alpaca.telescope: Slew complete — RA=10.6833 h  Dec=41.2692 °
14:35:29 [INFO] alpaca.camera: Starting 1.00 s light exposure
14:35:30 [INFO] alpaca.camera: Exposure complete — image ready for download
14:35:31 [INFO] dashboard: Image captured (2048×1536, 3.2 MB)
14:35:31 [INFO] image_watcher: New FITS file detected: /mnt/seestar/observation_001.fits (4.5 MB, OBJECT=M31)
14:35:32 [INFO] alpaca.telescope: Parking telescope…
14:35:33 [INFO] alpaca.telescope: Telescope parked
14:35:33 [INFO] dashboard: Sequence complete.
```

---

## Image Watcher

The **Image Watcher** monitors a directory for new FITS files (e.g., from Seestar or other imaging systems) and processes them automatically:

### Enabling Image Watcher

Edit `config.yaml`:
```yaml
image_watcher:
  enabled: true                   # set true to enable monitoring
  watch_path: "/mnt/seestar"      # path to watch for new files
  debounce_delay: 2.0             # seconds to wait after last write before processing
```

### How It Works

1. Monitors the watch directory for new `.fits` or `.fit` files
2. Uses OS-level file system events (FSEvents/inotify/kqueue) — no polling overhead
3. Debounces partial writes to avoid processing incomplete files
4. On new file detection:
   - Extracts FITS header metadata (OBJECT, EXPTIME, etc.)
   - Reports file size and location
   - Broadcasts event to dashboard for live updates
5. Dashboard displays:
   - File path and size
   - FITS header information (if available)
   - Image preview (when applicable)

**Typical use case:**  
You have a separate imaging system (Seestar, Stellarmate, etc.) that writes FITS files to a network share. The Image Watcher picks up these files in real-time and integrates them into your observing session log.

---

## How It Works

### Architecture

```
dashboard.py (entry point)
  ├─ Flask web server (port 5173)
  │   ├─ /api/discover       — UDP broadcast discovery
  │   ├─ /api/connect        — manual server connect
  │   ├─ /api/run            — start sequence
  │   ├─ /api/abort          — stop sequence
  │   ├─ /api/slew           — custom coordinate slewing
  │   ├─ /api/image          — retrieve captured image
  │   ├─ /api/status         — current state (telescope, camera, phase, safety, image watcher)
  │   ├─ /api/safety         — safety manager status
  │   └─ /api/logs           — live log stream (Server-Sent Events)
  │
  ├─ SafetyManager (daemon thread)
  │   ├─ Heartbeat monitor (every 30s)
  │   ├─ Reconnect logic (retry + exponential backoff)
  │   ├─ Dawn calculator (solar elevation NOAA algorithm)
  │   ├─ Signal handlers (SIGTERM, SIGINT)
  │   └─ Emergency park on: timeout / dawn / disconnect / signals
  │
  ├─ ImageWatcher (daemon thread, if enabled)
  │   ├─ File system event monitoring
  │   ├─ Debounce logic for partial writes
  │   ├─ FITS header extraction
  │   └─ Event broadcast to dashboard
  │
  └─ Sequence runner (daemon thread)
       └─ DeviceManager (owns device lifecycle)
            ├─ Telescope
            │   └─ AlpacaClient (HTTP/JSON wrapper)
            ├─ Camera
            │   └─ AlpacaClient
            ├─ Focuser (optional)
            │   └─ AlpacaClient
            └─ FilterWheel (optional)
                 └─ AlpacaClient
```

### Module Breakdown

#### `alpaca/discovery.py`
Implements ALPACA autodiscovery (ASCOM spec §3):
- Sends UDP broadcast `"alpacadiscovery1"` to 255.255.255.255:32227
- Collects JSON responses: `{"AlpacaPort": 11111, ...}`
- Returns list of `{"address": "192.168.x.x", "port": 11111}` dicts
- Robust to malformed responses; logs warnings for dropped packets

**Why UDP broadcast?**  
Allows a single discovery call to find all ALPACA servers on the LAN without needing IP addresses or DNS.

#### `alpaca/client.py`
Low-level HTTP wrapper around the ALPACA REST API:
- Builds URLs: `http://<host>:<port>/api/v<version>/<device>/<number>/<action>`
- Adds required headers: `ClientID`, `ClientTransactionID` (auto-incrementing)
- Parses JSON responses; raises `AlpacaError` if `ErrorNumber ≠ 0`
- Provides `_get(attribute)` for queries and `_put(action, **data)` for commands
- Includes `wait_for()` polling helper for slew completion, exposure readiness, etc.

**Why a wrapper?**  
Centralizes ALPACA protocol bookkeeping (IDs, error handling, timeouts) so device modules stay clean and domain-focused.

#### `alpaca/telescope.py`
Mount/telescope device:
- **Queries**: `is_slewing()`, `is_parked()`, `is_tracking()`, `ra()`, `dec()`
- **Commands**: `connect()`, `disconnect()`, `set_tracking()`, `slew_to_coordinates()`, `park()`, `unpark()`
- `slew_to_coordinates(ra, dec)` is async: sends the slew command, then polls until `is_slewing()` returns False
- Each method logs at INFO level so the user sees what's happening

#### `alpaca/camera.py`
Imaging device:
- **Queries**: `camera_state()`, `image_ready()`, `sensor_name()`, `full_well_capacity()`, `pixel_size_x/y()`
- **Commands**: `connect()`, `disconnect()`, `set_binning()`, `expose()`, `abort_exposure()`, `image_array()`
- `expose(duration, light=True)` polls the camera state until the image lands in the download buffer
- Camera states: IDLE=0, WAITING=1, EXPOSING=2, READING=3, DOWNLOAD=4, ERROR=5
- `image_array()` downloads the raw pixel data as a nested Python list (large arrays will be slow over HTTP)

#### `alpaca/focuser.py` & `alpaca/filterwheel.py`
Optional devices for focus and filter selection:
- Focuser: `move(position)`, `halt()`, `is_moving()`, `position()`
- FilterWheel: `set_position(slot)`, `filter_names()`, `is_moving()`
- Both include automatic wait-for-completion polling

#### `alpaca/device_manager.py`
Owns all device objects and their lifecycle:
- Reads `config.yaml` to see which devices are `enabled: true`
- Instantiates only enabled devices (no need to edit code if you disable one)
- `connect_all()`: connects all devices in sequence, logs connection strings
- `disconnect_all()`: gracefully disconnects each device, catches exceptions so one failure doesn't break cleanup

#### `alpaca/safety_manager.py`
**Protective watchdog** that monitors the telescope and ensures safe operation:

**Key responsibilities:**
- **Heartbeat pings**: Every 30 seconds, queries `GET /connected` to verify the server is reachable
- **Reconnect logic**: On heartbeat failure, immediately tries up to 3 reconnection attempts with 10-second backoff
- **Connection timeout**: If the telescope remains unreachable for >10 minutes (configurable), sends an emergency park command
- **Dawn parking**: Calculates solar elevation using the NOAA algorithm; parks when the sun crosses the astronomical dawn threshold (−18°, or −12° for nautical, −6° for civil)
- **Signal handling**: On `SIGTERM` or `SIGINT` (Ctrl-C), parks the mount before exiting — prevents damaging slew if power/network is lost
- **Safe-to-operate API**: Other modules call `is_safe()` and `status()` to check if it's safe to continue operations

**Why a separate thread?**  
The Safety Manager runs continuously in the background, independent of the sequence. It can trigger an emergency park even if the sequence is hung or doesn't respond.

#### `dashboard.py`
Web-based sequence runner with live monitoring:
- **Flask server** on port 5173 with real-time log streaming (Server-Sent Events)
- **Background poller** updates telescope/camera state every second
- **Sequence runner thread** orchestrates: discover → connect → unpark → track → slew → expose → park
- **Safety integration**: SafetyManager is instantiated at startup (so signal handlers are registered from the main thread), and `on_unsafe()` callback aborts the sequence if the system becomes unsafe
- **Image watcher integration**: Launches ImageWatcher if enabled in config, displays captured images in real-time
- **Live UI**: Shows current phase, coordinates, camera state, safety status, captured images, and a live scrolling log
- **Custom coordinates**: Supports manual RA/Dec input for ad-hoc slewing during observations

#### `image_watcher.py`
File system monitoring for new FITS files:
- **Watchdog-based monitoring**: Uses OS-level file system events (FSEvents/inotify/kqueue) for efficient directory monitoring
- **Debounce logic**: Prevents processing incomplete files; configurable delay ensures full write before callback
- **FITS header parsing**: Extracts metadata (OBJECT, EXPTIME, etc.) for dashboard display
- **Event stream**: Reports file path, size, header, and modification time to callbacks

**Key endpoints:**
- `/api/status` — returns full system state (telescope, camera, sequence phase, safety flags, image watcher status)
- `/api/logs` — Server-Sent Events stream of all logged messages
- `/api/run` — POST to start the sequence
- `/api/abort` — POST to stop the sequence
- `/api/slew` — POST with `ra` and `dec` parameters for custom coordinate slewing
- `/api/image` — GET to retrieve the latest captured image (base64 encoded)

---

## Configuration Details

### ALPACA Discovery
- **Port**: 32227 (UDP). Some networks block broadcasts; if discovery times out, ensure your firewall/router allows UDP broadcast traffic.
- **Timeout**: 5 seconds. Increase if your server is slow to boot; decrease if you're on a fast LAN.

### Device Numbers
Most ALPACA servers have only one of each device (number 0). If yours supports multiples (e.g., two cameras on different USB ports), set the device number in `config.yaml`.

### Telescope Coordinates
- **RA**: decimal hours (0–24). Example: 10.6833 h = 10h 41m
- **Dec**: decimal degrees (-90 to +90). Example: 41.2692° = 41° 16' 09"
- **Tracking rate**: 0 = Sidereal (default), 1 = Lunar, 2 = Solar, 3 = King

### Safety Manager & Dawn Calculation
The Safety Manager calculates solar elevation at your site using the NOAA algorithm (accurate to ±0.3° for 2000–2050). To enable dawn parking:

1. **Set your location** in `config.yaml`:
   ```yaml
   observer:
     latitude: 37.7749       # decimal degrees N (or S if negative)
     longitude: -122.4194    # decimal degrees E (or W if negative)
   ```
   Use Google Maps or similar to find your coordinates.

2. **Pick a dawn type**:
   - `astronomical`: Sun at −18° → civil twilight completely ends; all stars visible (typical for deep-sky imaging)
   - `nautical`: Sun at −12° → horizon clearly visible; some faint stars disappear
   - `civil`: Sun at −6° → typical sunrise/sunset area

3. **The Safety Manager will:**
   - Calculate the current solar elevation every 5 seconds
   - Park the telescope when the sun crosses the threshold
   - Log: `SafetyManager: EMERGENCY PARK — dawn — sun 14.5° > threshold −18.0°`

**Why 5-second polling?**  
The sun moves ~0.01° per minute near dawn, so 5-second checks are more than sufficient to catch the exact moment. The overhead is negligible.

### Logging
- Set `level: DEBUG` to see HTTP requests/responses and detailed safety checks
- Set `level: WARNING` to suppress verbose INFO messages
- Logs appear both in the terminal and in the dashboard's **Live Log** pane

---

## Troubleshooting

### "No ALPACA servers found"
- **Check server is running**: Restart your ALPACA server (ASCOM Remote, Stellarmate, KStars, etc.)
- **Check network**: Server and this machine must be on the same subnet
- **Check UDP broadcast**: Some corporate networks block UDP 32227. Confirm with `tcpdump` or Wireshark if needed
- **Check firewall**: macOS may require allowing Python in System Preferences > Security

### "ErrorNumber 1: Device not connected"
- The server responded, but the device (telescope, camera) is not physically or virtually connected
- In ASCOM Remote or KStars/INDI, ensure the device is "Connected" in the GUI before running the skeleton

### "Timeout during slew / exposure"
- The mount or camera is genuinely slow (normal for real hardware over network)
- Increase the timeout in `alpaca/telescope.py` (default 120s) or `alpaca/camera.py` (60s + exposure duration)
- Or disable the device in `config.yaml` if you're testing only the devices that work

### "Connection refused on port 11111"
- The ALPACA server is not listening on its HTTP port
- Check the server's settings (ASCOM Remote shows the port in its UI; KStars/Ekos has a control panel)
- Some servers use port 8000 or 9000; update `alpaca_cfg` in `main.py` if needed (or hardcode in DeviceManager constructor)

### Large image download is slow
- Raw image data over HTTP is inherently slow. The `image_array()` method in `camera.py` does not optimize downloads
- For production, consider FITS file export or a binary protocol like ASCOM COM (Windows-only)
- For testing, skip image download in `run_smoke_test()` (it's already commented out)

### Safety Manager Never Parks (even though connection is lost)
- **Check observer location**: If `latitude: 0.0, longitude: 0.0`, dawn checking is disabled but connection monitoring should still work
- **Check `safety.enabled: true`**: The Safety Manager must be enabled in `config.yaml`
- **Increase disconnect_timeout**: Default is 600 seconds (10 min). If you set it too high, it may appear to not work
- **Check logs for heartbeat errors**: The dashboard's **Live Log** shows `SafetyManager: heartbeat failed` when disconnections are detected
- **Test with a manual disconnect**: Stop the ALPACA server and verify the Safety Manager tries to reconnect (check the logs)

### Safety Manager Parks Too Early (false positive)
- **Check network stability**: Transient dropouts (milliseconds to seconds) should not trigger parks, but sustained dropouts will
- **Reduce `disconnect_timeout`**: Default 600 s. If you want faster response to dropouts, try 300 s (5 minutes)
- **Check reconnect settings**: If `reconnect_attempts` or `reconnect_delay` are too short, valid reconnections might fail. Try increasing them

### Dawn Parking Not Triggering
- **Verify observer location**: Set `latitude` and `longitude` to your actual site (not 0, 0)
- **Check dawn_type**: Default is `astronomical` (−18°). In summer at mid-latitude sites, the sun may not dip to −18° during nautical twilight. Try `civil` (−6°) or `nautical` (−12°) instead
- **Check current sun elevation**: The dashboard header shows `☀ +14.5°` etc. If the sun is already above the threshold, it won't trigger until the next night
- **Disable and re-enable**: Stop the app, set a much higher `dawn_threshold` (e.g., `5.0°` to trigger while sun is still above horizon for testing), restart, and verify it parks immediately

### Dashboard Shows "UNSAFE" But Sequence Keeps Running
- This is the safety manager successfully **aborting** the sequence. Check:
  - Logs will show `SafetyManager: EMERGENCY PARK — <reason>` and `Safety manager triggered abort: <reason>`
  - The phase will change to `error`
  - The telescope should be parking or already parked
- If the telescope didn't park, check its connection and logs for errors

---

## Extending the Agent

### Adding a New Device Type
1. Create `alpaca/mydevice.py` modeled on `telescope.py`
2. Subclass or wrap `AlpacaClient` with your device-specific methods
3. Add it to `DeviceManager.connect_all()` and `disconnect_all()`
4. Add a config entry in `config.yaml`
5. Use it in the sequence (in `_run_sequence()` in `dashboard.py`)

### Gating Operations on Safety
If you add custom operations that might be dangerous (e.g., motor movement, focuser motion), **check the Safety Manager before starting:**

```python
if not _safety_mgr.is_safe():
    logger.critical("Cannot proceed: system is unsafe (%s)", 
                    _safety_mgr.status()["reason"])
    return

# Safe to proceed with movement
my_device.move()
```

This prevents operations while the mount is being parked, during signal shutdown, or if the connection is lost.

### Extending the Safety Manager
The Safety Manager is self-contained but extensible:

- **Add custom safety checks**: Subclass `SafetyManager` and override `_run_dawn_check()` to add, e.g., wind speed monitoring, temperature warnings, etc.
- **Change park behavior**: Override `_emergency_park()` to log to a database, send alerts, or execute custom shutdown procedures
- **Adjust heartbeat strategy**: Change `_heartbeat()` to use a different device method or add redundant checks (e.g., poll both telescope and camera)

Example (wind monitoring):
```python
class ExtendedSafetyManager(SafetyManager):
    def _run_wind_check(self):
        # Query weather API
        wind_speed = get_wind_speed()
        if wind_speed > 30:  # mph
            self._emergency_park(f"high wind ({wind_speed} mph)")

# In dashboard.py launch():
_safety_mgr = ExtendedSafetyManager(config=cfg, on_unsafe=_on_safety_unsafe)
_safety_mgr.start()
```

### Adding Automated Sequences
Modify `_run_sequence()` in `dashboard.py` to add more sophisticated workflows:
- Iterative exposure loops
- Focus optimization
- Dither patterns
- Thermal or guide camera loops

All sequences have access to `_safety_mgr` and should check `is_safe()` before starting long operations.

### Exposing Additional Status
Add new routes to `dashboard.py` to expose custom state. For example:
```python
@app.route("/api/wind")
def api_wind():
    return jsonify({"wind_speed": get_wind_speed(), "safe": get_wind_speed() < 30})
```

Update the dashboard HTML to display the new data (add panels or status pills like the safety indicator).

### Integrating Custom Image Processing
To add custom image processing to captured images, extend `dashboard.py`:

```python
from image_watcher import ImageWatcher

def process_fits_file(event_dict: dict) -> None:
    """Called when ImageWatcher detects a new FITS file."""
    path = event_dict["path"]
    header = event_dict["header"]
    
    # Custom processing (e.g., plate solving, astrometry)
    logger.info(f"Processing {path}: {header.get('OBJECT', 'Unknown')}")
    
    # Update dashboard state if needed
    with _state_lock:
        _state["image_watcher"]["last_file"] = path
        _state["image_watcher"]["last_header"] = header

# In launch():
if cfg.get("image_watcher", {}).get("enabled"):
    watcher = ImageWatcher(
        cfg["image_watcher"]["watch_path"],
        process_fits_file,
        cfg["image_watcher"]["debounce_delay"]
    )
    watcher.start()
```

---

## API Reference (Quick)

### Telescope
```python
telescope.connect()
telescope.disconnect()
telescope.is_parked() -> bool
telescope.is_slewing() -> bool
telescope.is_tracking() -> bool
telescope.ra() -> float         # decimal hours
telescope.dec() -> float        # decimal degrees
telescope.unpark()
telescope.park()
telescope.set_tracking(enabled: bool)
telescope.slew_to_coordinates(ra: float, dec: float)  # blocks until done
```

### Camera
```python
camera.connect()
camera.disconnect()
camera.camera_state() -> int    # 0=IDLE, 2=EXPOSING, 3=READING, etc.
camera.image_ready() -> bool
camera.sensor_name() -> str
camera.set_binning(bin_x: int, bin_y: int | None = None)
camera.expose(duration: float, light: bool = True)  # blocks until ready
camera.abort_exposure()
camera.image_array() -> list    # nested list [row][col]
```

### Focuser
```python
focuser.connect()
focuser.disconnect()
focuser.position() -> int
focuser.is_moving() -> bool
focuser.move(position: int)     # blocks until done
focuser.halt()
```

### FilterWheel
```python
filterwheel.connect()
filterwheel.disconnect()
filterwheel.position() -> int
filterwheel.is_moving() -> bool
filterwheel.filter_names() -> list[str]
filterwheel.set_position(slot: int)  # blocks until done
```

### Safety Manager
```python
safety_mgr = SafetyManager(telescope=None, config=cfg, on_unsafe=callback)
safety_mgr.attach_telescope(telescope)  # call after device connects
safety_mgr.start()                      # install signal handlers + start monitor thread
safety_mgr.stop()                       # request graceful shutdown
safety_mgr.is_safe() -> bool            # safe to continue operations?
safety_mgr.status() -> dict             # {safe, parked, reason, heartbeat_ok, ...}
```

**Safety status dict keys:**
- `safe`: bool — True if safe to operate
- `parked`: bool — True if mount was parked by safety manager
- `reason`: str — why system is unsafe (empty if safe)
- `heartbeat_ok`: bool — True if last heartbeat succeeded
- `last_heartbeat`: float | None — UTC Unix timestamp of last successful ping
- `disconnected_secs`: float | None — seconds since connection loss (None if connected)
- `sun_elevation`: float | None — current solar elevation in degrees at observer location
- `dawn_threshold`: float — elevation angle that triggers dawn parking

---

## License

This skeleton is provided as-is for educational and testing purposes. The ALPACA protocol is maintained by the [ASCOM Initiative](https://ascom-standards.org/) under the ASCOM License Agreement.

---

## Further Reading

- [ALPACA Specification](https://ascom-standards.org/Developer/Alpaca.pdf) (official ASCOM document)
- [ASCOM Standards](https://ascom-standards.org/) (device interface specs)
- [Stellarmate](https://www.stellarmate.com/) (common ALPACA server on SBC)
- [KStars/INDI](https://indilib.org/) (open-source observatory control)
