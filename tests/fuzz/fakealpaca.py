"""FakeObservatory: an in-process ALPACA HTTP server with a full device set.

Serves the real ALPACA URL scheme (/api/v1/<type>/<num>/<attr> plus the
management API) on an ephemeral port, so the node agent's actual
AlpacaClient/requests stack — timeouts, sessions, JSON parsing, error
handling — is exercised end to end. A FaultInjector (tests/fuzz/faults.py)
can corrupt any exchange or put a device into a bad behavioral state.
"""

from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tests.fuzz.faults import FaultInjector, FaultPlan


class FakeTelescopeDev:
    def __init__(self, slew_s: float = 1.0, park_s: float = 0.5):
        self.lock = threading.Lock()
        self.slew_s = slew_s
        self.park_s = park_s
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.connected = False
            self.ra, self.dec = 5.0, 40.0
            self.tracking = False
            self.atpark = True
            self._slew_end = 0.0
            self._park_end = 0.0
            self._target = (5.0, 40.0)
            self.park_calls = 0
            self.slew_calls = 0
            self.abort_calls = 0

    def _tick(self):
        now = time.monotonic()
        if self._slew_end and now >= self._slew_end:
            self.ra, self.dec = self._target
            self._slew_end = 0.0
        if self._park_end and now >= self._park_end:
            self.atpark = True
            self._park_end = 0.0

    def get(self, attr: str):
        with self.lock:
            self._tick()
            return {
                "connected": self.connected,
                "slewing": bool(self._slew_end),
                "atpark": self.atpark and not self._park_end,
                "tracking": self.tracking,
                "rightascension": self.ra,
                "declination": self.dec,
                "name": "Fake Telescope",
                "description": "fuzz double",
            }.get(attr, 0)

    def put(self, action: str, form: dict):
        with self.lock:
            self._tick()
            if action == "connected":
                self.connected = str(form.get("Connected", "")).lower() == "true"
            elif action in ("slewtocoordinatesasync", "slewtoaltazasync"):
                self.slew_calls += 1
                self.atpark = False
                ra = float(form.get("RightAscension", form.get("Azimuth", 0)))
                dec = float(form.get("Declination", form.get("Altitude", 0)))
                self._target = (ra, dec)
                self._slew_end = time.monotonic() + self.slew_s
            elif action == "abortslew":
                self.abort_calls += 1
                self._slew_end = 0.0
            elif action == "park":
                self.park_calls += 1
                self._slew_end = 0.0
                self._park_end = time.monotonic() + self.park_s
            elif action == "unpark":
                self.atpark = False
            elif action == "tracking":
                self.tracking = str(form.get("Tracking", "")).lower() == "true"
            elif action == "moveaxis":
                pass


class FakeCameraDev:
    W, H = 16, 12

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.connected = False
            self._exp_end = 0.0
            self._ready = False
            self.exposures = 0
            self.aborts = 0
            self.binx = self.biny = 1
            self.gain = 100

    def get(self, attr: str):
        with self.lock:
            now = time.monotonic()
            if self._exp_end and now >= self._exp_end:
                self._ready = True
                self._exp_end = 0.0
            if attr == "camerastate":
                return 2 if self._exp_end else 0
            if attr == "imageready":
                return self._ready
            if attr == "imagearray":
                # tiny synthetic star field: flat background + one bright pixel
                frame = [[100 for _ in range(self.H)] for _ in range(self.W)]
                frame[self.W // 2][self.H // 2] = 30000
                return frame
            return {
                "connected": self.connected, "name": "Fake Camera",
                "sensorname": "FUZZCCD", "cameraxsize": self.W,
                "cameraysize": self.H, "pixelsizex": 2.9, "pixelsizey": 2.9,
                "gain": self.gain, "offset": 10, "ccdtemperature": -10.0,
                "setccdtemperature": -10.0, "cooleron": False,
                "fullwellcapacity": 50000.0, "description": "fuzz double",
            }.get(attr, 0)

    def put(self, action: str, form: dict):
        with self.lock:
            if action == "connected":
                self.connected = str(form.get("Connected", "")).lower() == "true"
            elif action == "startexposure":
                self.exposures += 1
                self._ready = False
                self._exp_end = time.monotonic() + min(
                    float(form.get("Duration", 0.1)), 0.5)
            elif action == "abortexposure":
                self.aborts += 1
                self._exp_end = 0.0
                self._ready = False
            elif action == "binx":
                self.binx = int(form.get("BinX", 1))
            elif action == "biny":
                self.biny = int(form.get("BinY", 1))
            elif action == "gain":
                self.gain = int(form.get("Gain", 100))


class FakeFilterWheelDev:
    def __init__(self, move_s: float = 0.3):
        self.lock = threading.Lock()
        self.move_s = move_s
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.connected = False
            self.position_val = 0
            self._move_end = 0.0
            self._target = 0
            self.moves = 0

    def get(self, attr: str):
        with self.lock:
            now = time.monotonic()
            if self._move_end and now >= self._move_end:
                self.position_val = self._target
                self._move_end = 0.0
            if attr == "position":
                return -1 if self._move_end else self.position_val
            return {
                "connected": self.connected, "name": "Fake Wheel",
                "names": ["CV", "B", "V", "R"], "description": "fuzz double",
            }.get(attr, 0)

    def put(self, action: str, form: dict):
        with self.lock:
            if action == "connected":
                self.connected = str(form.get("Connected", "")).lower() == "true"
            elif action == "position":
                self.moves += 1
                self._target = int(form.get("Position", 0))
                self._move_end = time.monotonic() + self.move_s


class FakeSimpleDev:
    """Focuser / covercalibrator: enough surface to connect and idle."""

    def __init__(self, extra: dict | None = None):
        self.lock = threading.Lock()
        self.connected = False
        self.extra = extra or {}

    def reset(self):
        self.connected = False

    def get(self, attr: str):
        with self.lock:
            base = {"connected": self.connected, "name": "Fake Device",
                    "description": "fuzz double", "position": 5000,
                    "ismoving": False, "coverstate": 3, "maxstep": 10000,
                    "temperature": 10.0}
            base.update(self.extra)
            return base.get(attr, 0)

    def put(self, action: str, form: dict):
        with self.lock:
            if action == "connected":
                self.connected = str(form.get("Connected", "")).lower() == "true"


class FakeObservatory:
    """The ALPACA server plus its devices and the fault injector."""

    def __init__(self, plan: FaultPlan | None = None):
        self.injector = FaultInjector(plan or FaultPlan())
        self.telescope = FakeTelescopeDev()
        self.camera = FakeCameraDev()
        self.filterwheel = FakeFilterWheelDev()
        self.focuser = FakeSimpleDev()
        self.covercalibrator = FakeSimpleDev()
        self.devices = {
            "telescope": self.telescope, "camera": self.camera,
            "filterwheel": self.filterwheel, "focuser": self.focuser,
            "covercalibrator": self.covercalibrator,
        }
        self.request_log: list[tuple[float, str, str, str]] = []
        self._log_lock = threading.Lock()
        self._flap = {}

        obs = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, value, error_number=0, error_message=""):
                body = json.dumps({
                    "Value": value, "ErrorNumber": error_number,
                    "ErrorMessage": error_message,
                    "ClientTransactionID": 0, "ServerTransactionID": 1,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _raw(self, status: int, body: bytes,
                     ctype="application/json", truncate=False):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2] if truncate else body)
                if truncate:
                    self.close_connection = True

            def _dispatch(self):
                parsed = urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                # management API
                if parts[:2] == ["management", "v1"]:
                    return self._json([
                        {"DeviceType": "Telescope", "DeviceNumber": 0,
                         "DeviceName": "Fake Telescope", "UniqueID": "FUZZ-0001"},
                        {"DeviceType": "Camera", "DeviceNumber": 0,
                         "DeviceName": "Fake Camera", "UniqueID": "FUZZ-0002"},
                    ])
                if len(parts) < 4 or parts[0] != "api":
                    return self._raw(404, b'{"error":"not found"}')
                dtype, attr = parts[2], parts[4] if len(parts) > 4 else parts[3]
                dtype = parts[2]
                attr = parts[4] if len(parts) >= 5 else ""
                dev = obs.devices.get(dtype)
                if dev is None or not attr:
                    return self._raw(400, b'{"error":"bad device"}')

                with obs._log_lock:
                    obs.request_log.append(
                        (obs.injector.elapsed(), self.command, dtype, attr))

                # ── Behavioral episodes ─────────────────────────────────────
                episodes = {e.kind for e in obs.injector.active_episodes(dtype)}
                if "device_down" in episodes:
                    self.close_connection = True
                    self.connection.close()
                    return None
                if "device_reboot" in episodes and not getattr(
                        self, "_rebooted", False):
                    dev.reset()
                if "commands_fail" in episodes and self.command == "PUT":
                    return self._json(None, 1035, "invalid while parked (fuzz)")

                # ── Per-request faults ──────────────────────────────────────
                fault = obs.injector.request_fault()
                if fault == "http500":
                    return self._raw(500, b"Internal Server Error",
                                     ctype="text/plain")
                if fault == "hang":
                    time.sleep(12)  # beyond the client's 10 s timeout
                    return self._json(0)
                if fault == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return None
                if fault == "truncated_json":
                    return self._raw(
                        200, json.dumps({"Value": 0, "ErrorNumber": 0}).encode(),
                        truncate=True)
                if fault == "error_number":
                    return self._json(None, 1024 + obs.injector.rand().randrange(200),
                                      "fuzz-injected device error")
                if fault == "missing_value":
                    return self._raw(200, b'{"ErrorNumber": 0, "ErrorMessage": ""}')
                if fault == "wrong_type_value":
                    return self._json(obs.injector.rand().choice(
                        ["banana", [1, 2], {"x": 1}, None]))
                if fault == "non_json":
                    return self._raw(200, b"<html>device error page</html>",
                                     ctype="text/html")

                # ── Normal dispatch (with semantic value corruption) ────────
                if self.command == "GET":
                    if dtype == "telescope" and "slewing_stuck" in episodes \
                            and attr == "slewing":
                        return self._json(True)
                    if dtype == "telescope" and "atpark_flapping" in episodes \
                            and attr == "atpark":
                        key = ("flap", dtype, attr)
                        obs._flap[key] = not obs._flap.get(key, False)
                        return self._json(obs._flap[key])
                    if dtype == "telescope" and "park_raises" in episodes \
                            and attr == "atpark":
                        return self._json(False)
                    if dtype == "camera" and "camera_never_ready" in episodes \
                            and attr == "imageready":
                        return self._json(False)
                    if dtype == "filterwheel" and "filter_stuck" in episodes \
                            and attr == "position":
                        return self._json(-1)
                    value = dev.get(attr)
                    if fault == "nan_coords" and isinstance(value, float):
                        value = obs.injector.rand().choice(
                            [float("nan"), float("inf")])
                    elif fault == "out_of_range_coords" and isinstance(value, float):
                        value = obs.injector.rand().choice([25.5, 95.0, -400.0])
                    elif fault == "negative_number" and isinstance(value, (int, float)):
                        value = -abs(value) - 1
                    elif fault == "huge_number" and isinstance(value, (int, float)):
                        value = 1e308
                    if isinstance(value, float) and not math.isfinite(value):
                        # json.dumps would emit bare NaN/Infinity — which is
                        # exactly what some real drivers do, so keep it.
                        body = (b'{"Value": ' + str(value).replace("inf", "Infinity")
                                .replace("nan", "NaN").encode()
                                + b', "ErrorNumber": 0, "ErrorMessage": ""}')
                        return self._raw(200, body)
                    return self._json(value)

                # PUT
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(
                    self.rfile.read(length).decode("utf-8", "replace")).items()}
                if dtype == "telescope" and "park_raises" in episodes \
                        and attr == "park":
                    return self._json(None, 1031, "park failed (fuzz)")
                dev.put(attr, form)
                return self._json(None)

            def do_GET(self):
                try:
                    self._dispatch()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_PUT = do_GET
            do_POST = do_GET

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-alpaca")

    def start(self) -> "FakeObservatory":
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
