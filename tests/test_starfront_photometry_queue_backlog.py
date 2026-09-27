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
        now = time.time()
        for p, at in ((fresh, now), (old_file, now), (expired, now - 13 * 3600),
                      (missing, now)):
            self.q.put_nowait(p, priority=dash._PHOT_PRIO_WATCHER, key=p, enqueued_at=at)
        gone = dash._purge_stale_photometry_jobs("test")
        self.assertEqual({e["path"] for e in gone}, {old_file, expired, missing})
        self.assertEqual([e["path"] for e in self.q.items()], [fresh])
        reasons = self.q.summary()["last_purge"]["reasons"]
        self.assertEqual(reasons, {"old_file": 1, "expired": 1, "missing": 1})

    def test_frame_queued_before_utc_rollover_is_kept(self):
        # Codex P1 on #145: queued at 23:59 UTC in fits_export/<yesterday>,
        # still waiting (e.g. worker yielding to a live stack) after 00:00 UTC.
        import datetime as _dt
        y, m, d = map(int, dash._fits_export_night_utc().split("-"))
        yesterday = (_dt.date(y, m, d) - _dt.timedelta(days=1)).isoformat()
        path = _touch(os.path.join(self.export, yesterday, "SS_Cyg_coadd05_6eb74013.fits"))
        self.q.put_nowait({"path": path, "target_name": "SS Cyg"},
                          priority=dash._PHOT_PRIO_SCIENCE, key=path,
                          enqueued_at=time.time() - 60)
        self.assertEqual(dash._purge_stale_photometry_jobs("test"), [])
        self.assertEqual(self.q.qsize(), 1)
        # A fresh enqueue of that dated path is still refused (issue #123).
        self.assertTrue(dash._is_previous_night_fits(path))
        self.assertEqual(dash._photometry_job_stale_reason(path), "previous_night")

    def test_enqueue_purges_stale_backlog_so_tonight_fits_gets_in(self):
        self._backlog(49, enqueued_ago_s=3 * 86400)  # earlier nights, still on disk
        coadd = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        self.assertEqual(dash._enqueue_photometry(coadd, target_name="SS Cyg"), "queued")
        self.assertEqual(self.q.qsize(), 1)
        with dash._state_lock:
            self.assertEqual(dash._state["photometry"]["queued"], 1)

    def test_watcher_refuses_fits_written_long_ago(self):
        old = _touch(os.path.join(self.myworks, "Light_old.fit"), age_s=2 * 86400)
        self.assertEqual(dash._enqueue_watcher_photometry(old), "refused_stale")
        self.assertEqual(self.q.qsize(), 0)
        fresh = _touch(os.path.join(self.myworks, "Light_new.fit"))
        self.assertEqual(dash._enqueue_watcher_photometry(fresh), "queued")

    def test_on_new_fits_routes_through_watcher_gate(self):
        import inspect
        src = inspect.getsource(dash._on_new_fits)
        self.assertIn("_enqueue_watcher_photometry(path)", src)

    def test_untrusted_scope_clock_mtime_is_not_refused(self):
        # Seestar clock reset to years ago: mtime alone must not refuse.
        weird = _touch(os.path.join(self.myworks, "Light_clock.fit"),
                       age_s=400 * 86400)
        self.assertEqual(dash._enqueue_watcher_photometry(weird), "queued")
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
            self.assertEqual(dash._enqueue_watcher_photometry(old), "queued")
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


class DeadLetterTest(_Base):
    def setUp(self):
        super().setUp()
        dash._phot_failures.clear()
        self.addCleanup(dash._phot_failures.clear)

    def test_repeatedly_failing_frame_is_dead_lettered_and_purged(self):
        hd = _touch(os.path.join(self.tonight, "HD 209458 b_01_abc.fits"))
        dash._enqueue_photometry(hd, target_name="HD 209458 b")
        with patch.object(dash, "_run_photometry_bg",
                          return_value=(False, "vsp_http_400")), \
             patch.object(dash, "_live_capture_busy", return_value=False):
            self.assertTrue(dash._phot_worker_step(timeout=0.01))
            self.q.task_done()
            # One failure: a retry is still allowed.
            self.assertEqual(dash._enqueue_photometry(hd, target_name="HD 209458 b"),
                             "queued")
            self.assertTrue(dash._phot_worker_step(timeout=0.01))
        self.assertEqual(dash._enqueue_photometry(hd, target_name="HD 209458 b"),
                         "refused_dead_letter")
        self.assertEqual(self.q.qsize(), 0)
        dead = dash._photometry_dead_letter_list()
        self.assertEqual([d["file"] for d in dead], ["HD 209458 b_01_abc.fits"])
        self.assertEqual(dead[0]["reason"], "vsp_http_400")

    def test_queued_entry_purged_once_dead_lettered(self):
        hd = _touch(os.path.join(self.tonight, "HD 209458 b_02.fits"))
        self.q.put_nowait(hd, priority=dash._PHOT_PRIO_CAPTURE, key=os.path.realpath(hd))
        for _ in range(dash._PHOT_MAX_ATTEMPTS):
            dash._record_photometry_outcome(hd, False, "fits copy no-op")
        gone = dash._purge_stale_photometry_jobs("test")
        self.assertEqual([e["path"] for e in gone], [hd])
        self.assertEqual(self.q.summary()["last_purge"]["reasons"], {"dead_letter": 1})

    def test_success_clears_failure_history_and_dead_letter_ages_out(self):
        p = _touch(os.path.join(self.tonight, "SS Cyg_03.fits"))
        dash._record_photometry_outcome(p, False, "x")
        dash._record_photometry_outcome(p, True, None)
        self.assertEqual(dash._phot_failures, {})
        for _ in range(dash._PHOT_MAX_ATTEMPTS):
            dash._record_photometry_outcome(p, False, "x")
        dash._phot_failures[os.path.realpath(p)]["at"] = time.time() - 13 * 3600
        self.assertEqual(dash._enqueue_photometry(p), "queued")

    def test_run_photometry_bg_reports_outcome(self):
        with patch("src.photometry.run_pipeline_ex",
                   return_value=(None, {"stage": "catalog", "reason_code": "vsp_http_400",
                                        "message": "AAVSO VSP returned HTTP 400"})), \
             patch.object(dash, "_handle_empty_field_rejection"):
            ok, reason = dash._run_photometry_bg("/tmp/hd.fits")
        self.assertFalse(ok)
        self.assertEqual(reason, "vsp_http_400")

    def test_api_clear_dead_letter_by_match_allows_requeue(self):
        client = dash.app.test_client()
        hd = _touch(os.path.join(self.tonight, "HD 209458 b_04.fits"))
        for _ in range(dash._PHOT_MAX_ATTEMPTS):
            dash._record_photometry_outcome(hd, False, "vsp_http_400")
        body = client.get("/api/photometry/queue").get_json()
        self.assertEqual(body["queue"]["dead_lettered"], 1)
        self.assertEqual(body["dead_letter"][0]["file"], "HD 209458 b_04.fits")
        body = client.post("/api/photometry/queue/clear",
                           json={"match": "HD 209458", "dead_letter": True}).get_json()
        self.assertEqual(body["dead_letter_cleared"], 1)
        self.assertEqual(dash._enqueue_photometry(hd), "queued")


class QueueOperatorControlsTest(_Base):
    def test_cancel_by_match_and_prioritize_session_science(self):
        client = dash.app.test_client()
        hd = [_touch(os.path.join(self.tonight, f"HD 209458 b_{i}.fits")) for i in range(3)]
        for p in hd:
            dash._enqueue_photometry(p)
        subs = self._backlog(2)
        ss = _touch(os.path.join(self.tonight, "SS_Cyg_coadd01_93bac84.fits"))
        dash._enqueue_photometry(ss)

        body = client.post("/api/photometry/queue/clear",
                           json={"match": "HD 209458"}).get_json()
        self.assertEqual(body["removed"], 3)
        self.assertEqual(self.q.qsize(), 3)

        body = client.post("/api/photometry/queue/prioritize",
                           json={"match": "light_001"}).get_json()
        self.assertEqual(body["prioritized"], 1)
        self.assertEqual(body["items"][0]["file"], "Light_001.fit")
        self.assertEqual(body["items"][0]["priority"], "pinned")

        body = client.post("/api/photometry/queue/prioritize", json={}).get_json()
        self.assertEqual(body["files"], ["SS_Cyg_coadd01_93bac84.fits"])
        self.assertEqual({i["file"] for i in body["items"][:2]},
                         {"Light_001.fit", "SS_Cyg_coadd01_93bac84.fits"})
        self.assertEqual(os.path.basename(subs[0]), body["items"][-1]["file"])

    def test_mcp_prioritize_and_targeted_clear_tools(self):
        import inspect
        from telescope_mcp.tools import hardware
        src = inspect.getsource(hardware).replace("\r\n", "\n")
        self.assertIn("@server.tool()\n    def node_photometry_queue_prioritize(", src)
        self.assertIn('"/api/photometry/queue/prioritize"', src)
        self.assertIn('body["match"] = m', src)
        self.assertIn('body["dead_letter"] = True', src)


class SameFileExportTest(unittest.TestCase):
    def test_export_of_frame_already_in_fits_export_enriches_in_place(self):
        import numpy as np
        from astropy.io import fits
        from src import fits_export
        with tempfile.TemporaryDirectory() as td:
            export = os.path.join(td, "fits_export")
            result = {"target_name": "HD 209458 b", "magnitude": 7.6,
                      "uncertainty": 0.01, "snr": 50, "quality_flag": "good"}
            src_dir = os.path.join(export, "2026-09-26")
            os.makedirs(src_dir)
            src = os.path.join(src_dir, "HD 209458 b_01.fits")
            hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.float32))
            hdu.header["DATE-OBS"] = "2026-09-26T03:00:00"
            hdu.writeto(src)
            with patch.object(fits_export, "_date_str_from_result",
                              return_value="2026-09-26"):
                out = fits_export.export_enhanced_fits(
                    src, result, {"photometry": {}}, export_dir=export)
            self.assertEqual(os.path.realpath(out), os.path.realpath(src))
            self.assertTrue(dash._fits_already_photometered(src))


class SeestarPositionReadTest(unittest.TestCase):
    """Starfront 2026-09-26 (related #133): 1279 "The given key 'RA' was not
    present in the dictionary" is a transient read, not a connect failure."""

    def _err(self):
        from alpaca.client import AlpacaError
        return AlpacaError(
            "rightascension → ErrorNumber 1279: The given key 'RA' was not "
            "present in the dictionary.", code=1279)

    def test_ra_read_retries_transient_missing_key(self):
        from alpaca.telescope import Telescope
        tel = Telescope("127.0.0.1", 1)
        tel._c = MagicMock()
        tel._c._get.side_effect = [self._err(), 20.36]
        with patch("alpaca.telescope.time.sleep") as sleep:
            self.assertAlmostEqual(tel.ra(), 20.36)
        sleep.assert_called_once()

    def test_other_errors_are_not_retried(self):
        from alpaca.client import AlpacaError
        from alpaca.telescope import Telescope, is_transient_position_error
        tel = Telescope("127.0.0.1", 1)
        tel._c = MagicMock()
        tel._c._get.side_effect = AlpacaError("declination → ErrorNumber 1279: "
                                              "capture is active", code=1279)
        with patch("alpaca.telescope.time.sleep") as sleep:
            with self.assertRaises(AlpacaError):
                tel.dec()
        sleep.assert_not_called()
        self.assertTrue(is_transient_position_error(self._err()))

    def test_poll_loop_does_not_mark_disconnected_on_transient(self):
        import inspect
        src = inspect.getsource(dash)
        i = src.index("if is_transient_position_error(exc):")
        block = src[i:i + 900]
        self.assertLess(block.index("not a "), block.index('_state["telescope"]["connected"] = False'))
        self.assertIn("else:", block)


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

    def test_api_enqueue_reports_full_queue_instead_of_ok(self):
        client = dash.app.test_client()
        for i in range(dash._PHOT_QUEUE_MAX):
            p = _touch(os.path.join(self.tonight, f"SS_Cyg_coadd{i:02d}_x.fits"))
            self.q.put_nowait(p, priority=dash._PHOT_PRIO_SCIENCE, key=p)
        frame = _touch(os.path.join(self.tonight, "Manual_01.fits"))
        resp = client.post("/api/photometry/enqueue", json={"path": frame})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["status"], "dropped_full")
        ok = client.post("/api/photometry/enqueue",
                         json={"path": os.path.join(self.tonight, "SS_Cyg_coadd00_x.fits")})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["status"], "merged")

    def test_mcp_tools_registered(self):
        import inspect
        from telescope_mcp.tools import hardware
        src = inspect.getsource(hardware).replace("\r\n", "\n")
        self.assertIn("@server.tool()\n    def node_photometry_queue()", src)
        self.assertIn("@server.tool()\n    def node_photometry_queue_clear(", src)
        self.assertIn('require_confirmation(confirm, "clear the whole photometry queue")', src)


if __name__ == "__main__":
    unittest.main()
