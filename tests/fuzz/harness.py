"""NodeHarness: boot the real node agent against fake hardware + fake cloud.

Runs dashboard.launch() (the production entry point — all daemon threads,
safety manager, supervisor, cloud communicator) in-process, pointed at a
FakeObservatory (ALPACA HTTP double) and the gauntlet's FakeCloud, inside a
fresh temp cwd. One harness per process: dashboard state is module-global,
so the fuzz runner starts a new subprocess per seed.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests.fuzz.fakealpaca import FakeObservatory
from tests.fuzz.faults import FaultPlan

_CONFIG_TEMPLATE = """
alpaca:
  api_version: 1
devices:
  telescope: {{enabled: true, device_number: 0}}
  camera: {{enabled: true, device_number: 0}}
  focuser: {{enabled: false}}
  covercalibrator: {{enabled: false}}
safety:
  enabled: true
  heartbeat_interval: 0.2
  heartbeat_timeout: 1.0
  disconnect_timeout: {disconnect_timeout}
  reconnect_attempts: 1
  reconnect_delay: 0.5
  park_at_dawn: false
  observer: {{latitude: 31.5, longitude: -99.2}}
observatory: {{}}
cloud:
  enabled: true
  url: {cloud_url}
  realtime_url: {cloud_url}
  node_id: node_test01
  api_key: key_test01
  heartbeat_interval: 1.0
  plan_poll_interval: 3.0
photometry:
  enabled: false
image_watcher:
  enabled: false
commissioning:
  enabled: false
pier_cam:
  enabled: false
logging:
  level: INFO
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def restrict_network_to_localhost():
    """Fail any outbound request that isn't to 127.0.0.1/localhost.

    The node calls weather APIs, geocoders, GitHub, etc. — in the fuzz
    harness every one of those must fail fast (which is itself fault
    injection for their error paths) instead of touching the internet.
    """
    import requests
    import requests.sessions
    real_request = requests.sessions.Session.request

    def guarded(self, method, url, *a, **kw):
        if "127.0.0.1" in url or "localhost" in url:
            return real_request(self, method, url, *a, **kw)
        raise requests.ConnectionError(f"fuzz: outbound network disabled ({url})")

    requests.sessions.Session.request = guarded


class RealCloud:
    """The production Flask cloud app on an ephemeral port + ephemeral PG.

    API-compatible with the subset of gauntlet FakeCloud the harness uses.
    """

    def __init__(self):
        from tests.fuzz.pgtmp import TempPostgres
        self._pg = TempPostgres().__enter__()
        import os
        os.environ["DATABASE_URL"] = self._pg.dsn
        from cloud import db, registry
        db.init(self._pg.dsn)
        import cloud.server as server
        app = server.create_app({"server": {"admin_key": "fuzz-admin"}})
        self.status_counts: dict = {}

        counts = self.status_counts
        wsgi = app.wsgi_app

        def counting(environ, start_response):
            def sr(status, headers, exc_info=None):
                counts[status[:3]] = counts.get(status[:3], 0) + 1
                return start_response(status, headers, exc_info)
            return wsgi(environ, sr)
        app.wsgi_app = counting

        self.creds = registry.register_node(
            {"latitude": 31.5, "longitude": -99.2, "owner_name": "Fuzz"})
        from werkzeug.serving import make_server
        self._server = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="real-cloud")

    def start(self) -> "RealCloud":
        self._thread.start()
        return self

    def count_paths(self, needle: str) -> int:
        return 0   # request log not tracked; outbox invariant unused here

    def stop(self):
        self._server.shutdown()
        self._pg.__exit__(None, None, None)

    def five_hundreds(self) -> int:
        return sum(v for k, v in self.status_counts.items()
                   if k.startswith("5"))


class NodeHarness:
    def __init__(self, plan: FaultPlan, scenario_s: float = 25.0,
                 workdir: str | None = None, disconnect_timeout: float = 6.0,
                 cloud_mode: str = "fake"):
        self.plan = plan
        self.scenario_s = scenario_s
        self.disconnect_timeout = disconnect_timeout
        self.cloud_mode = cloud_mode
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="nodefuzz_"))
        self.thread_exceptions: list[dict] = []
        self.result: dict = {}

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def run(self) -> dict:
        import os
        os.chdir(self.workdir)
        restrict_network_to_localhost()

        # Capture daemon-thread deaths (I2)
        def hook(args):
            self.thread_exceptions.append({
                "thread": getattr(args.thread, "name", "?"),
                "type": args.exc_type.__name__,
                "value": str(args.exc_value)[:500],
            })
        threading.excepthook = hook

        if self.cloud_mode == "real":
            self.cloud = RealCloud().start()
            node_id = self.cloud.creds["node_id"]
            api_key = self.cloud.creds["api_key"]
        else:
            from tests.gauntlet.fakecloud import FakeCloud
            self.cloud = FakeCloud().start()
            node_id, api_key = "node_test01", "key_test01"
        self.obs = FakeObservatory(self.plan).start()

        (self.workdir / "config.yaml").write_text(_CONFIG_TEMPLATE.format(
            cloud_url=self.cloud.url,
            disconnect_timeout=self.disconnect_timeout)
            .replace("node_test01", node_id).replace("key_test01", api_key))

        import src.dashboard as dashboard
        self.dashboard = dashboard
        port = _free_port()
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=dashboard.launch, args=(port,),
                         daemon=True, name="node-launch").start()

        try:
            self._wait_ready()
            self._api("/api/connect", {"host": "127.0.0.1", "port": self.obs.port})
            self._api("/api/schedule/run", {"items": self._schedule_items()})
            self._observe()
        finally:
            try:
                self.result = self._collect()
            finally:
                # Always release external resources — a RealCloud holds a
                # separate postgres process that outlives this process.
                try:
                    self.cloud.stop()
                except Exception:
                    pass
                try:
                    self.obs.stop()
                except Exception:
                    pass
        return self.result

    def _wait_ready(self, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.base + "/api/status", timeout=1)
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("node dashboard never became ready")

    def _api(self, path: str, payload: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _schedule_items(self) -> list[dict]:
        return [
            {"target": "Fuzz Target A", "ra": 6.0, "dec": 42.0,
             "expDur": 0.3, "expCount": 2, "binning": 1, "startTime": "",
             "filter": "CV"},
            {"target": "Fuzz Target B", "ra": 7.5, "dec": 35.0,
             "expDur": 0.3, "expCount": 2, "binning": 1, "startTime": "",
             "filter": "V"},
        ]

    def _observe(self):
        """Let the scenario play out; return early once the schedule finishes
        and at least one safety-heartbeat cycle has passed."""
        deadline = time.monotonic() + self.scenario_s
        while time.monotonic() < deadline:
            time.sleep(0.5)
            with self.dashboard._sched_lock:
                running = self.dashboard._sched_state["running"]
            if not running and time.monotonic() > deadline - self.scenario_s + 8:
                break

    # ── collection & invariants ────────────────────────────────────────────────

    def _collect(self) -> dict:
        from tests.fuzz import invariants as inv

        d = self.dashboard
        with d._sched_lock:
            sched = dict(d._sched_state)
        sched.pop("items", None)
        safety = {}
        try:
            if d._safety_mgr is not None:
                safety = d._safety_mgr.status()
        except Exception as exc:
            safety = {"error": str(exc)}

        # A schedule still running at scenario end isn't a wedge by itself —
        # product timeouts (slew 180 s, exposure +120 s) legitimately outlive
        # a short scenario. The invariant is cancellation responsiveness:
        # request an abort and require the machine to stop within its polling
        # granularity. A machine that ignores abort is truly wedged.
        if sched.get("running"):
            try:
                req = urllib.request.Request(
                    self.base + "/api/schedule/abort", method="DELETE")
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
            for _ in range(60):
                time.sleep(0.5)
                with d._sched_lock:
                    sched = dict(d._sched_state)
                sched.pop("items", None)
                if not sched.get("running"):
                    break

        violations = []
        violations += inv.check_threads(set(), self.thread_exceptions)
        violations += inv.check_phase(sched)
        with self.obs._log_lock:
            park_attempts = sum(1 for _, m, d, a in self.obs.request_log
                                if m == "PUT" and d == "telescope" and a == "park")
        violations += inv.check_safety_parked(safety, park_attempts)
        violations += inv.check_poller_singleton()
        violations += inv.check_log(self.workdir / "logs" / "node.log")
        if self.cloud_mode == "real":
            n500 = self.cloud.five_hundreds()
            if n500:
                violations.append(
                    f"real cloud served {n500} 5xx responses to the node")

        return {
            "violations": violations,
            "sched_state": {k: sched.get(k) for k in
                            ("running", "current_phase", "completed", "total",
                             "error", "current_target")},
            "safety": {k: safety.get(k) for k in ("safe", "parked", "reason")},
            "telescope": {"park_calls": self.obs.telescope.park_calls,
                          "slew_calls": self.obs.telescope.slew_calls},
            "camera": {"exposures": self.obs.camera.exposures},
            "requests_served": len(self.obs.request_log),
            "thread_exceptions": self.thread_exceptions,
            "plan": json.loads(self.plan.to_json()),
        }
