# ALPACA Skeleton

A lightweight Python implementation of the ALPACA protocol for reliable communication with networked astronomy devices. This skeleton automatically discovers ALPACA servers on the local network, connects to their components (telescope, camera, focuser, filter wheel), and executes basic remote commands with clear logging at every step.

**What is ALPACA?**  
ALPACA (Astronomy Low-level Control And Automation) is an open, standardized HTTP/JSON protocol defined by the [ASCOM Initiative](https://ascom-standards.org/). It allows astronomy software to talk to mounts, cameras, focusers, and other equipment over a network without worrying about proprietary drivers or serial ports.

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
   This installs `requests` (HTTP client) and `pyyaml` (config parsing).

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

logging:
  level: INFO                  # DEBUG, INFO, WARNING, ERROR
  format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
```

### 3. Run Commands

`main.py` is a CLI — each invocation runs one command and exits. Commands are mutually exclusive.

```bash
# Scan for ALPACA servers on the LAN
python main.py --discover

# Connect to all enabled devices and confirm they respond
python main.py --activate

# Print current telescope position and state
python main.py --status

# Slew to RA (decimal hours) and Dec (decimal degrees)
python main.py --slew 8.13 70.37

# Park the telescope
python main.py --park

# Take a camera exposure (seconds)
python main.py --expose 5.0
```

**What each command does:**

| Command | Behavior |
|---|---|
| `--discover` | Broadcasts discovery, prints all found servers. No device connections. |
| `--activate` | Connects to all enabled devices, confirms they respond, then disconnects. |
| `--status` | Connects to the telescope, prints RA/Dec/tracking/parked/slewing state. |
| `--slew RA DEC` | Unparks, enables tracking, slews to the given coordinates, then disconnects. |
| `--park` | Connects to the telescope and parks it. |
| `--expose N` | Connects to the camera and takes an N-second light exposure. |

Every command discovers the server automatically, connects only what it needs, and disconnects cleanly on exit or error.

**Sample output (`--slew 8.13 70.37`):**
```
2026-05-22 14:35:22,123 [INFO] alpaca.discovery: Broadcasting ALPACA discovery on port 32227
2026-05-22 14:35:24,456 [INFO] alpaca.discovery: Discovered ALPACA server at 192.168.1.100:11111
2026-05-22 14:35:24,500 [INFO] main: Using server 192.168.1.100:11111
2026-05-22 14:35:24,700 [INFO] alpaca.telescope: Telescope connected: Simulator Telescope
2026-05-22 14:35:25,100 [INFO] alpaca.telescope: Telescope unparked
2026-05-22 14:35:25,300 [INFO] alpaca.telescope: Slewing to RA=8.1300 h  Dec=70.3700 °
2026-05-22 14:35:27,500 [INFO] alpaca.telescope: Slew complete — RA=8.1300 h  Dec=70.3700 °
```

---

## How It Works

### Architecture

```
main.py (entry point)
  └─ discover_servers()  ← UDP broadcast discovery
       └─ DeviceManager  ← owns device lifecycle
            ├─ Telescope
            ├─ Camera
            ├─ Focuser
            └─ FilterWheel
                 └─ AlpacaClient (HTTP/JSON wrapper)
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

#### `main.py`
CLI entry point built with `argparse`. Each flag maps to a single command function:
- `--discover` → `cmd_discover()`: runs discovery only, no device connections
- `--activate` → `cmd_activate()`: connects all enabled devices and disconnects
- `--status` → `cmd_status()`: prints telescope RA/Dec/tracking/parked state
- `--slew RA DEC` → `cmd_slew()`: unpark → tracking on → slew → disconnect
- `--park` → `cmd_park()`: connects telescope and parks it
- `--expose N` → `cmd_expose()`: connects camera and takes an N-second exposure

Every command calls `disconnect_all()` in a `finally` block so devices are never left dangling, even on error or Ctrl-C.

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

### Logging
- Set `level: DEBUG` to see HTTP requests/responses
- Set `level: WARNING` to suppress verbose INFO messages

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

---

## Extending the Skeleton

### Adding a New Device Type
1. Create `alpaca/mydevice.py` modeled on `telescope.py`
2. Subclass or wrap `AlpacaClient` with your device-specific methods
3. Add it to `DeviceManager.connect_all()` and `disconnect_all()`
4. Add a config entry in `config.yaml`
5. Add a `--mydevice-action` flag and `cmd_mydevice_action()` function in `main.py`

### Adding Error Recovery
Wrap device calls in try-except blocks and retry with exponential backoff. The `AlpacaClient.wait_for()` method is a good model.

### Adding Automated Sequences
Add a new `--sequence` flag and a `cmd_sequence()` function in `main.py` that coordinates multiple devices — e.g., slew, focus, filter change, then expose.

### Adding a Web Dashboard
Use Flask or FastAPI to wrap `DeviceManager` and expose its state via REST or WebSocket. The discovery and device modules are already modular enough for this.

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

---

## License

This skeleton is provided as-is for educational and testing purposes. The ALPACA protocol is maintained by the [ASCOM Initiative](https://ascom-standards.org/) under the ASCOM License Agreement.

---

## Further Reading

- [ALPACA Specification](https://ascom-standards.org/Developer/Alpaca.pdf) (official ASCOM document)
- [ASCOM Standards](https://ascom-standards.org/) (device interface specs)
- [Stellarmate](https://www.stellarmate.com/) (common ALPACA server on SBC)
- [KStars/INDI](https://indilib.org/) (open-source observatory control)
