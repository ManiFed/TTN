#!/usr/bin/env python3
"""Starfront 2026-09-26 (NodeAgent 1.0.92): photometry backlog starved science.

The photometry queue (cap 50) sat at ~49 stale jobs from earlier nights, so
tonight's SS Cyg science coadds stayed queued / were dropped and never produced
a magnitude, and the backlog's ASTAP solves contended with live-stack
centering (Error 1279, related #133). Covers:

* science coadds / named captures jump watcher backlog;
* a full queue evicts backlog instead of dropping fresh science;
* stale jobs (earlier night, expired, FITS gone, old file) are purged;
* the worker yields to live stacking / centering and solves in the
  background ASTAP lane; interactive ASTAP solves jump background ones;
* queue status / clear are exposed on the API + MCP surface.
"""

import os
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import src.dashboard as dash
import src.photometry as phot


def _touch(path: str, age_s: float = 0.0) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"SIMPLE  =                    T")
    if age_s:
        t = time.time() - age_s
        os.utime(path, (t, t))
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        self.export = os.path.join(self.root, "fits_export")
        self.tonight = os.path.join(self.export, dash._fits_export_night_utc())
        self.myworks = os.path.join(self.root, "MyWorks", "SS Cyg_sub")
        self.q = dash._PhotometryQueue(maxsize=dash._PHOT_QUEUE_MAX)
        self._patches = [
            patch.object(dash, "_phot_queue", self.q),
            patch.object(dash, "_load_config", return_value={"photometry": {}}),
            patch.object(dash, "_fits_export_dir", return_value=self.export),
            patch.object(dash, "_notify_commissioning_fits"),
            patch.object(dash, "_telemetry", MagicMock()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.td.cleanup()

    def _backlog(self, n: int, *, age_s: float = 0.0, enqueued_ago_s: float = 0.0):
        paths = []
        for i in range(n):
            path = _touch(os.path.join(self.myworks, f"Light_{i:03d}.fit"), age_s)
            self.q.put_nowait(path, priority=dash._PHOT_PRIO_WATCHER,
                              key=os.path.realpath(path),
                              enqueued_at=time.time() - enqueued_ago_s)
            paths.append(path)
        return paths


class PriorityTest(_Base):
    def test_science_coadd_jumps_49_job_backlog(self):
        self._backlog(49)
        coadd = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        self.assertEqual(dash._enqueue_photometry(coadd, target_name="SS Cyg"), "queued")
        first = self.q.get(block=False)
        self.assertEqual(first, {"path": coadd, "target_name": "SS Cyg"})

    def test_plain_coadd_path_is_science_priority(self):
        coadd = os.path.join(self.tonight, "SS_Cyg_coadd05_6eb74013.fits")
        self.assertEqual(dash._photometry_job_priority(coadd), dash._PHOT_PRIO_SCIENCE)
        manual = os.path.join(self.tonight, "SS Cyg_01_abc.fits")
        self.assertEqual(dash._photometry_job_priority(manual), dash._PHOT_PRIO_CAPTURE)
        sub = os.path.join(self.myworks, "Light_001.fit")
        self.assertEqual(dash._photometry_job_priority(sub), dash._PHOT_PRIO_WATCHER)

    def test_full_queue_evicts_oldest_backlog_for_science(self):
        paths = self._backlog(dash._PHOT_QUEUE_MAX)
        coadd = _touch(os.path.join(self.tonight, "SS_Cyg_coadd05_6eb74013.fits"))
        self.assertEqual(dash._enqueue_photometry(coadd, target_name="SS Cyg"), "queued")
        self.assertEqual(self.q.qsize(), dash._PHOT_QUEUE_MAX)
        queued_paths = {e["path"] for e in self.q.items()}
        self.assertNotIn(paths[0], queued_paths)   # oldest backlog evicted
        self.assertIn(coadd, queued_paths)
        self.assertEqual(self.q.summary()["evicted_total"], 1)

    def test_full_queue_of_science_drops_new_backlog(self):
        for i in range(dash._PHOT_QUEUE_MAX):
            p = _touch(os.path.join(self.tonight, f"SS_Cyg_coadd{i:02d}_x.fits"))
            self.q.put_nowait(p, priority=dash._PHOT_PRIO_SCIENCE, key=p)
        sub = _touch(os.path.join(self.myworks, "Light_new.fit"))
        self.assertEqual(dash._enqueue_photometry(sub), "dropped_full")
        self.assertEqual(self.q.qsize(), dash._PHOT_QUEUE_MAX)

    def test_duplicate_enqueue_merges_and_keeps_target_identity(self):
        path = _touch(os.path.join(self.tonight, "SS Cyg_01_abc.fits"))
        self.assertEqual(dash._enqueue_photometry(path), "queued")   # fits_export watcher
        self.assertEqual(
            dash._enqueue_photometry(path, target_name="SS Cyg"), "merged")  # expose path
        self.assertEqual(self.q.qsize(), 1)
        entry = self.q.items()[0]
        self.assertEqual(entry["job"], {"path": path, "target_name": "SS Cyg"})
        self.assertEqual(entry["priority"], dash._PHOT_PRIO_SCIENCE)


class StalePurgeTest(_Base):
    def test_purge_drops_missing_expired_old_and_previous_night(self):
        fresh = _touch(os.path.join(self.tonight, "fresh.fits"))
        old_file = _touch(os.path.join(self.myworks, "old.fit"), age_s=2 * 86400)
        expired = _touch(os.path.join(self.myworks, "expired.fit"))
        missing = os.path.join(self.myworks, "gone.fit")
        prev = os.path.join(self.export, "2020-01-01", "SS Cyg_01.fits")
        now = time.time()
        for p, at in ((fresh, now), (old_file, now), (expired, now - 13 * 3600),
                      (missing, now), (prev, now)):
            self.q.put_nowait(p, priority=dash._PHOT_PRIO_WATCHER, key=p, enqueued_at=at)
        gone = dash._purge_stale_photometry_jobs("test")
        self.assertEqual({e["path"] for e in gone}, {old_file, expired, missing, prev})
        self.assertEqual([e["path"] for e in self.q.items()], [fresh])
        reasons = self.q.summary()["last_purge"]["reasons"]
        self.assertEqual(reasons, {"old_file": 1, "expired": 1, "missing": 1,
                                   "previous_night": 1})

    def test_enqueue_purges_stale_backlog_so_tonight_fits_gets_in(self):
        self._backlog(49, enqueued_ago_s=3 * 86400)  # earlier nights, still on disk
        coadd = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        self.assertEqual(dash._enqueue_photometry(coadd, target_name="SS Cyg"), "queued")
        self.assertEqual(self.q.qsize(), 1)
        with dash._state_lock:
            self.assertEqual(dash._state["photometry"]["queued"], 1)

    def test_enqueue_refuses_fits_written_long_ago(self):
        old = _touch(os.path.join(self.myworks, "Light_old.fit"), age_s=2 * 86400)
        self.assertEqual(dash._enqueue_photometry(old), "refused_stale")
        self.assertEqual(self.q.qsize(), 0)

    def test_untrusted_scope_clock_mtime_is_not_refused(self):
        # Seestar clock reset to years ago: mtime alone must not refuse.
        weird = _touch(os.path.join(self.myworks, "Light_clock.fit"),
                       age_s=400 * 86400)
        self.assertEqual(dash._enqueue_photometry(weird), "queued")
        self.assertEqual(dash._purge_stale_photometry_jobs("test"), [])

    def test_watcher_duplicate_of_inflight_frame_is_merged(self):
        path = _touch(os.path.join(self.tonight, "SS Cyg_01_abc.fits"))
        dash._enqueue_photometry(path, target_name="SS Cyg")
        self.q.get(block=False)                      # worker picked it up
        self.assertEqual(dash._enqueue_photometry(path), "merged")
        self.assertEqual(self.q.qsize(), 0)
        self.q.task_done()
        self.assertEqual(dash._enqueue_photometry(path), "queued")

    def test_max_age_zero_disables_age_checks(self):
        old = _touch(os.path.join(self.myworks, "Light_old.fit"), age_s=2 * 86400)
        with patch.object(dash, "_load_config",
                          return_value={"photometry": {"queue_max_age_hours": 0}}):
            self.assertEqual(dash._enqueue_photometry(old), "queued")
            self.assertEqual(dash._purge_stale_photometry_jobs("test"), [])


class WorkerYieldTest(_Base):
    def test_worker_yields_to_live_stack_then_runs_in_background_lane(self):
        path = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        dash._enqueue_photometry(path, target_name="SS Cyg")
        seen = []

        def fake_run(fits_path, **kw):
            seen.append((fits_path, getattr(phot._astap_tls, "priority", None)))

        dash._phot_yield_started = None
        with patch.object(dash, "_run_photometry_bg", side_effect=fake_run), \
             patch.object(dash, "_live_capture_busy", return_value=True), \
             patch.object(dash.time, "sleep"):
            self.assertFalse(dash._phot_worker_step(timeout=0.01))
        self.assertEqual(seen, [])
        self.assertEqual(self.q.qsize(), 1)
        with patch.object(dash, "_run_photometry_bg", side_effect=fake_run), \
             patch.object(dash, "_live_capture_busy", return_value=False):
            self.assertTrue(dash._phot_worker_step(timeout=0.01))
        self.assertEqual(seen, [(path, phot.ASTAP_BACKGROUND)])
        self.assertIsNone(getattr(phot._astap_tls, "priority", None))

    def test_worker_processes_after_yield_budget(self):
        path = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        dash._enqueue_photometry(path, target_name="SS Cyg")
        dash._phot_yield_started = time.monotonic() - 10_000
        with patch.object(dash, "_run_photometry_bg") as run, \
             patch.object(dash, "_live_capture_busy", return_value=True):
            self.assertTrue(dash._phot_worker_step(timeout=0.01))
        run.assert_called_once()

    def test_worker_skips_job_whose_fits_vanished(self):
        path = _touch(os.path.join(self.tonight, "SS Cyg_01.fits"))
        dash._enqueue_photometry(path, target_name="SS Cyg")
        os.remove(path)
        with patch.object(dash, "_run_photometry_bg") as run, \
             patch.object(dash, "_live_capture_busy", return_value=False):
            self.assertFalse(dash._phot_worker_step(timeout=0.01))
        run.assert_not_called()


class AstapArbiterTest(unittest.TestCase):
    def test_interactive_solve_jumps_waiting_background_solve(self):
        arb = phot._AstapArbiter()
        order = []
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with arb.slot(phot.ASTAP_BACKGROUND):
                holding.set()
                release.wait(5)

        def waiter(priority):
            with arb.slot(priority, wait_s=5):
                order.append(priority)

        t0 = threading.Thread(target=holder)
        t0.start()
        holding.wait(5)
        tb = threading.Thread(target=waiter, args=(phot.ASTAP_BACKGROUND,))
        tb.start()
        time.sleep(0.05)
        ti = threading.Thread(target=waiter, args=(phot.ASTAP_INTERACTIVE,))
        ti.start()
        deadline = time.time() + 5
        while arb.status()["interactive_waiting"] < 1 and time.time() < deadline:
            time.sleep(0.01)
        release.set()
        for t in (t0, tb, ti):
            t.join(5)
        self.assertEqual(order, [phot.ASTAP_INTERACTIVE, phot.ASTAP_BACKGROUND])
        self.assertFalse(arb.status()["busy"])

    def test_slot_timeout_runs_anyway_instead_of_failing(self):
        arb = phot._AstapArbiter()
        with arb.slot(phot.ASTAP_BACKGROUND) as first:
            self.assertTrue(first)
            with arb.slot(phot.ASTAP_INTERACTIVE, wait_s=0.01) as second:
                self.assertFalse(second)
        self.assertFalse(arb.status()["busy"])

    def test_run_astap_uses_thread_priority(self):
        seen = []

        class _Arb:
            def slot(self, priority):
                seen.append(priority)
                return phot._AstapArbiter().slot(priority)

        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(phot, "_astap_arbiter", _Arb()), \
             patch("src.photometry.subprocess.run", return_value=fake):
            phot._run_astap("/tmp/x.fits", 10.0, 20.0, "astap", 10)
            with phot.astap_priority(phot.ASTAP_BACKGROUND):
                phot._run_astap("/tmp/x.fits", 10.0, 20.0, "astap", 10)
        self.assertEqual(seen, [phot.ASTAP_INTERACTIVE, phot.ASTAP_BACKGROUND])

    def test_centering_solve_is_interactive_inside_background_thread(self):
        from alpaca import platesolve
        seen = []

        def fake_astap(*a, **k):
            seen.append(getattr(phot._astap_tls, "priority", None))
            return phot.AstapResult(False, "No solution found!")

        with patch("src.photometry._run_astap", side_effect=fake_astap), \
             phot.astap_priority(phot.ASTAP_BACKGROUND):
            platesolve.solve_image_array([[1.0, 2.0], [3.0, 4.0]], 10.0, 20.0,
                                         solver="astap", solver_path="astap")
            self.assertEqual(phot._astap_tls.priority, phot.ASTAP_BACKGROUND)
        self.assertEqual(seen, [phot.ASTAP_INTERACTIVE])


class QueueApiTest(_Base):
    def test_queue_status_and_clear_endpoints(self):
        client = dash.app.test_client()
        fresh = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        dash._enqueue_photometry(fresh, target_name="SS Cyg")
        self._backlog(3, enqueued_ago_s=3 * 86400)

        body = client.get("/api/photometry/queue").get_json()
        self.assertEqual(body["queue"]["size"], 4)
        self.assertEqual(body["items"][0]["file"], "SS_Cyg_coadd01_93bac84.fits")
        self.assertEqual(body["items"][0]["priority"], "science")
        self.assertIn("astap", body)

        body = client.get("/api/photometry").get_json()
        self.assertEqual(body["queue"]["max"], dash._PHOT_QUEUE_MAX)

        body = client.post("/api/photometry/queue/clear", json={}).get_json()
        self.assertEqual(body["removed"], 3)
        self.assertEqual(body["queue"]["size"], 1)

        body = client.post("/api/photometry/queue/clear",
                           json={"stale_only": False}).get_json()
        self.assertEqual(body["removed"], 1)
        self.assertEqual(body["queue"]["size"], 0)

    def test_api_enqueue_reports_stale_refusal(self):
        client = dash.app.test_client()
        old = _touch(os.path.join(self.tonight, "old.fits"), age_s=2 * 86400)
        resp = client.post("/api/photometry/enqueue", json={"path": old})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["status"], "refused_stale")

    def test_mcp_tools_registered(self):
        import inspect
        from telescope_mcp.tools import hardware
        src = inspect.getsource(hardware).replace("\r\n", "\n")
        self.assertIn("@server.tool()\n    def node_photometry_queue()", src)
        self.assertIn("@server.tool()\n    def node_photometry_queue_clear(", src)
        self.assertIn('require_confirmation(confirm, "clear the whole photometry queue")', src)


if __name__ == "__main__":
    unittest.main()
