"""Gauntlet: NodeSupervisor keeps a headless node alive (F1, F8, F9, F17).

The supervisor is the answer to "the service restarted at 2 a.m. and nobody
was watching": it must reconnect saved devices with backoff, revive a dead
image watcher, detect host sleep, and keep the disk from filling — and every
action must leave structured evidence.
"""

import os
import time
import unittest

from src import telemetry
from src.node_supervisor import (
    NodeSupervisor, prune_old_files, DISK_WARN_GB, _RECONNECT_BASE_S)
from tests.gauntlet.util import TempCwdTestCase


class _Harness:
    """Programmable fakes for every supervisor hook."""

    def __init__(self):
        self.config = {
            "alpaca": {"default_server": {"address": "10.0.0.5", "port": 5555}},
            "image_watcher": {"enabled": True, "watch_path": "/mnt/seestar"},
        }
        self.connected = False
        self.connect_result = True
        self.connect_calls = []
        self.connect_results_by_host = None  # optional {(host, port): bool}
        self.watcher_healthy = True
        self.restart_result = True
        self.restart_calls = 0
        self.discovered = []
        self.persisted = []

    def make(self, **kwargs) -> NodeSupervisor:
        return NodeSupervisor(
            load_config=lambda: self.config,
            devices_connected=lambda: self.connected,
            connect_default=self._connect,
            watcher_ok=lambda: self.watcher_healthy,
            restart_watcher=self._restart,
            discover_servers=kwargs.pop("discover_servers", self._discover),
            persist_default_server=kwargs.pop(
                "persist_default_server", self._persist),
            **kwargs,
        )

    def _discover(self):
        return list(self.discovered)

    def _persist(self, host, port):
        self.persisted.append((host, port))
        self.config.setdefault("alpaca", {}).setdefault("default_server", {})
        self.config["alpaca"]["default_server"]["address"] = host
        self.config["alpaca"]["default_server"]["port"] = port

    def _connect(self, host, port):
        self.connect_calls.append((host, port))
        if self.connect_results_by_host is not None:
            ok = bool(self.connect_results_by_host.get((host, port), False))
        else:
            ok = bool(self.connect_result)
        if ok:
            self.connected = True
        return ok

    def _restart(self):
        self.restart_calls += 1
        return self.restart_result


class SupervisorGauntletTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        telemetry.reset_for_tests()
        self.h = _Harness()

    def tearDown(self):
        telemetry.reset_for_tests()
        super().tearDown()

    # ── Device reconnect (F1) ─────────────────────────────────────────────────

    def test_disconnected_node_reconnects_to_saved_server(self):
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.connect_calls, [("10.0.0.5", 5555)])
        self.assertEqual(telemetry.counters().get("device_reconnected"), 1)

    def test_no_saved_server_means_no_reconnect_attempts(self):
        self.h.config["alpaca"] = {}
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.connect_calls, [])

    def test_connected_node_is_left_alone(self):
        self.h.connected = True
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.connect_calls, [])

    def test_failed_reconnect_backs_off_exponentially(self):
        self.h.connect_result = False
        sup = self.h.make()
        now = time.monotonic()
        sup.tick(now)
        self.assertEqual(len(self.h.connect_calls), 1)
        # Within the backoff window: no new attempt.
        sup.tick(now + 1)
        self.assertEqual(len(self.h.connect_calls), 1)
        # After the first backoff window: retry, and window doubles.
        sup.tick(now + _RECONNECT_BASE_S + 1)
        self.assertEqual(len(self.h.connect_calls), 2)
        sup.tick(now + _RECONNECT_BASE_S + 2)
        self.assertEqual(len(self.h.connect_calls), 2)
        self.assertGreaterEqual(
            telemetry.counters().get("device_connect_failed", 0), 2)

    def test_reconnect_exception_is_contained(self):
        def explode(host, port):
            raise RuntimeError("ALPACA driver panic")
        sup = NodeSupervisor(
            load_config=lambda: self.h.config,
            devices_connected=lambda: False,
            connect_default=explode,
            watcher_ok=lambda: True,
            restart_watcher=lambda: True,
        )
        sup.tick()  # must not raise
        self.assertGreaterEqual(
            telemetry.counters().get("device_connect_failed", 0), 1)

    def test_unreachable_saved_ip_falls_back_to_discovery(self):
        """Starfront 2026-09-01: Seestar DHCP moved 172.22.6.32 → .140."""
        self.h.connect_results_by_host = {
            ("10.0.0.5", 5555): False,
            ("10.0.0.9", 5555): True,
        }
        self.h.discovered = [{"address": "10.0.0.9", "port": 5555,
                              "device_name": "Seestar S30PROSF Telescope"}]
        sup = self.h.make()
        sup.tick()
        self.assertEqual(
            self.h.connect_calls, [("10.0.0.5", 5555), ("10.0.0.9", 5555)])
        self.assertEqual(self.h.persisted, [("10.0.0.9", 5555)])
        self.assertEqual(
            self.h.config["alpaca"]["default_server"]["address"], "10.0.0.9")
        self.assertEqual(telemetry.counters().get("device_reconnected"), 1)

    def test_discovery_skips_a_server_with_the_wrong_serial(self):
        """Two ALPACA servers on the LAN; only the saved UniqueID is ours."""
        self.h.config["observatory"] = {"telescope_serial": "SEESTAR-OURS"}
        self.h.connect_results_by_host = {
            ("10.0.0.5", 5555): False,
            ("10.0.0.8", 5555): True,   # simulator / neighbor
            ("10.0.0.9", 5555): True,   # our Seestar, new DHCP
        }
        self.h.discovered = [
            {"address": "10.0.0.8", "port": 5555, "serial": "OTHER-SCOPE"},
            {"address": "10.0.0.9", "port": 5555, "serial": "SEESTAR-OURS"},
        ]
        sup = self.h.make()
        sup.tick()
        self.assertNotIn(("10.0.0.8", 5555), self.h.connect_calls)
        self.assertEqual(self.h.persisted, [("10.0.0.9", 5555)])

    def test_discovery_skips_same_dead_saved_host(self):
        self.h.connect_result = False
        self.h.discovered = [{"address": "10.0.0.5", "port": 5555}]
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.connect_calls, [("10.0.0.5", 5555)])
        self.assertEqual(self.h.persisted, [])
        self.assertGreaterEqual(
            telemetry.counters().get("device_connect_failed", 0), 1)

    # ── Image watcher (F9) ────────────────────────────────────────────────────

    def test_dead_watcher_is_restarted_with_evidence(self):
        self.h.connected = True
        self.h.watcher_healthy = False
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.restart_calls, 1)
        self.assertEqual(telemetry.counters().get("image_watcher_restarted"), 1)

    def test_unrevivable_watcher_reports_down(self):
        self.h.connected = True
        self.h.watcher_healthy = False
        self.h.restart_result = False
        sup = self.h.make()
        sup.tick()
        self.assertEqual(telemetry.counters().get("image_watcher_down"), 1)

    def test_disabled_watcher_is_ignored(self):
        self.h.connected = True
        self.h.config["image_watcher"]["enabled"] = False
        self.h.watcher_healthy = False
        sup = self.h.make()
        sup.tick()
        self.assertEqual(self.h.restart_calls, 0)

    # ── Host sleep detection (F17) ────────────────────────────────────────────

    def test_wall_clock_jump_is_reported_as_host_sleep(self):
        self.h.connected = True
        sup = self.h.make()
        sup.tick()  # baseline
        sup._last_wall -= 600  # simulate 10 min of wall time passing while asleep
        sup.tick()
        self.assertEqual(telemetry.counters().get("host_slept"), 1)
        events = [e for e in telemetry.recent(10) if e["event"] == "host_slept"]
        self.assertGreater(events[0]["detail"]["gap_s"], 500)

    # ── Disk health & retention (F8) ──────────────────────────────────────────

    def test_low_disk_emits_event_once_per_state_change(self):
        self.h.connected = True
        sup = self.h.make()
        orig = telemetry.disk_free_gb
        telemetry.disk_free_gb = lambda path=".": DISK_WARN_GB - 1
        try:
            sup.tick()
            sup._next_disk_check_at = 0  # force another disk pass
            sup.tick()
        finally:
            telemetry.disk_free_gb = orig
        self.assertEqual(telemetry.counters().get("disk_low"), 1)

    def test_retention_prunes_only_old_files(self):
        os.makedirs("data/fits", exist_ok=True)
        old = "data/fits/old.fits"
        new = "data/fits/new.fits"
        for p in (old, new):
            with open(p, "w") as fh:
                fh.write("x")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        removed = prune_old_files(retention_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_retention_survives_missing_directories(self):
        self.assertEqual(prune_old_files(retention_days=14), 0)

    # ── The loop itself ───────────────────────────────────────────────────────

    def test_tick_exception_never_kills_the_loop(self):
        sup = NodeSupervisor(
            load_config=lambda: (_ for _ in ()).throw(RuntimeError("config boom")),
            devices_connected=lambda: False,
            connect_default=lambda h, p: True,
            watcher_ok=lambda: True,
            restart_watcher=lambda: True,
            interval_s=0.05,
        )
        sup.start()
        try:
            time.sleep(0.2)
            self.assertTrue(sup._thread.is_alive())
        finally:
            sup.stop()


if __name__ == "__main__":
    unittest.main()
