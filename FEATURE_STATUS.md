# Boundless Skies Node — ALPACA Automation Harness (Essential Features Only)

## 1. Discovery & Connection Management

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Manual IP:Port Connection** | ✅ | `config.yaml` — configure `alpaca.host` and `alpaca.port`. Ready. |
| **Device Discovery** (optional, for convenience) | ✅ | `alpaca/discovery.py` — fully implemented. Optional for automated discovery. |
| **Connect/Disconnect** | ✅ | `DeviceManager.connect_all()` / `disconnect_all()` — fully working. |
| **ClientID & TransactionID** | ✅ | `client.py` — auto-handled on every call. |

---

## 2. Telescope / Mount Control

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Slew to RA/Dec** | ✅ | `telescope.py:slew_to_coordinates(ra, dec)` — blocking call. Ready. |
| **Slew Status Check** | ✅ | `telescope.py:is_slewing()` — ready to poll. |
| **Get Current RA/Dec** | ✅ | `telescope.py:ra()`, `dec()` — ready. |
| **Enable/Disable Tracking** | ✅ | `telescope.py:set_tracking(bool)` — ready. |
| **Park / Unpark** | ✅ | `telescope.py:park()`, `unpark()` — ready. |
| **Safety Checks** (altitude limits) | ⚠️ | `safety_manager.py` exists. Use to validate slew targets before sending. |

---

## 3. Imaging & Camera Control

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Start Exposure** | ✅ | `camera.py:expose(duration_s)` — ready. |
| **Check Exposure Status** | ✅ | `camera.py:is_exposing()` — ready to poll. |
| **Image Download** | ✅ | `camera.py` integrates with `fits_export.py` — FITS files auto-saved. |
| **Exposure Duration (config)** | ✅ | `config.yaml: camera.exposure_duration` — ready. |
| **Binning (config)** | ✅ | `config.yaml: camera.binning` — ready. |

---

## 4. Focusing

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Move to Position** | ✅ | `focuser.py:move(position)` — blocking call. Ready. |
| **Check Focusing Status** | ✅ | `focuser.py:is_moving()` — ready to poll. |
| **Halt Focus** | ✅ | `focuser.py:halt()` — ready. |
| **Autofocus Routine** | ❌ | **NEW**: `autofocus.py` — algorithm: step focuser → expose short frame → measure FWHM → step → repeat → find peak position. |

---

## 5. Filter Wheel

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Set Position (by slot)** | ✅ | `filterwheel.py:set_position(slot)` — blocking call. Ready. |
| **Check Wheel Status** | ✅ | `filterwheel.py:is_moving()` — ready to poll. |
| **Get Available Filters** | ✅ | `filterwheel.py:filter_names()` — ready. |

---

## 6. Testing & Logging

| Feature | Status | How to Add |
|---------|--------|-----------|
| **Python Test Harness** | ⚠️ | Use `main.py` as entry point. Write test scripts that call `DeviceManager` methods. |
| **Logging (Python)** | ✅ | Standard logging configured in `main.py`. All device operations log to console/file. |
| **Error Handling** | ✅ | All device methods raise `AlpacaError` on server errors. Catch in test code. |

---

## Essential Automation Tests (Example Checklist)

```
☐ Connect to all enabled devices (telescope, camera, focuser, filter wheel)
☐ Slew telescope to known coordinates and wait for completion
☐ Verify RA/Dec within tolerance of target
☐ Take exposure and verify FITS file is saved
☐ Focus: move to known position, verify position query
☐ Autofocus: find focus peak
☐ Filter wheel: rotate through all positions
☐ Disconnect gracefully
```

---

## Quick Reference: Ready-to-Use Functions

| Component | Method | Status |
|-----------|--------|--------|
| **Telescope** | `slew_to_coordinates(ra, dec)` | ✅ |
| **Telescope** | `is_slewing()` | ✅ |
| **Telescope** | `ra()`, `dec()` | ✅ |
| **Telescope** | `park()`, `unpark()` | ✅ |
| **Telescope** | `set_tracking(bool)` | ✅ |
| **Camera** | `expose(duration_s)` | ✅ |
| **Camera** | `is_exposing()` | ✅ |
| **Focuser** | `move(position)` | ✅ |
| **Focuser** | `is_moving()` | ✅ |
| **Focuser** | `position()` | ✅ |
| **Focuser** | `halt()` | ✅ |
| **FilterWheel** | `set_position(slot)` | ✅ |
| **FilterWheel** | `is_moving()` | ✅ |
| **FilterWheel** | `filter_names()` | ✅ |
| **DeviceManager** | `connect_all()`, `disconnect_all()` | ✅ |

---

## What's Missing (for Automation)

| Feature | Priority | Implementation |
|---------|----------|-----------------|
| **Autofocus** | HIGH | `alpaca/autofocus.py` — step focuser, measure FWHM, find peak |
| **Safety Checks** | MEDIUM | Validate coordinates (altitude limits, etc.) before slew |
| **Better Timeouts** | MEDIUM | Tune ALPACA call timeouts in `client.py` based on network reliability |
| **Retry Logic** | MEDIUM | Handle transient connection errors with exponential backoff |

---

## How to Run Tests

1. Configure devices in `config.yaml` (set `enabled: true` and correct device numbers)
2. Ensure ALPACA server is running on the specified host:port
3. Write test script using `DeviceManager`:

```python
from alpaca.device_manager import DeviceManager
import yaml

with open('config.yaml') as f:
    config = yaml.safe_load(f)

dm = DeviceManager('192.168.1.100', 11111, config)
dm.connect_all()

# Run tests
dm.telescope.slew_to_coordinates(23.683, 80.269)
while dm.telescope.is_slewing():
    print(f"RA: {dm.telescope.ra()}, Dec: {dm.telescope.dec()}")
    
dm.camera.expose(1.0)
while dm.camera.is_exposing():
    pass

print("Test complete!")
dm.disconnect_all()
```

4. Check FITS files in `fits_export/` directory
