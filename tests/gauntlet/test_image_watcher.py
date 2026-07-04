"""Gauntlet: ImageWatcher vs. real filesystem behavior (F9).

Partial writes, atomic renames, junk files, and vanished watch paths — the
watcher must debounce, fire exactly once per file, and fail visibly (not
crash) when the path is gone.
"""

import os
import threading
import time
import unittest

from src.image_watcher import ImageWatcher
from tests.gauntlet.util import TempCwdTestCase


class ImageWatcherGauntletTest(TempCwdTestCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.fired = threading.Event()
        os.makedirs("watch", exist_ok=True)
        self.watcher = ImageWatcher("watch", self._cb, debounce_delay=0.3)

    def tearDown(self):
        try:
            self.watcher.stop()
        except Exception:
            pass
        super().tearDown()

    def _cb(self, info):
        self.events.append(info)
        self.fired.set()

    def _write(self, name, data=b"SIMPLE  =                    T", flush_delay=0.0):
        path = os.path.join("watch", name)
        with open(path, "wb") as fh:
            fh.write(data)
            if flush_delay:
                fh.flush()
                time.sleep(flush_delay)
        return path

    def test_new_fits_fires_exactly_once(self):
        self.watcher.start()
        self._write("frame1.fits")
        self.assertTrue(self.fired.wait(timeout=5))
        time.sleep(0.5)  # would catch duplicate fires
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0]["path"].endswith("frame1.fits"))

    def test_atomic_rename_completion_is_detected(self):
        self.watcher.start()
        tmp = self._write("frame2.tmp")
        os.rename(tmp, os.path.join("watch", "frame2.fits"))
        self.assertTrue(self.fired.wait(timeout=5))
        self.assertTrue(self.events[0]["path"].endswith("frame2.fits"))

    def test_slow_partial_write_debounces_to_single_event(self):
        self.watcher.start()
        path = os.path.join("watch", "slow.fits")
        with open(path, "wb") as fh:
            for _ in range(3):
                fh.write(b"chunk")
                fh.flush()
                time.sleep(0.1)  # keep re-triggering within the debounce window
        self.assertTrue(self.fired.wait(timeout=5))
        time.sleep(0.5)
        self.assertEqual(len(self.events), 1)

    def test_non_fits_files_are_ignored(self):
        self.watcher.start()
        self._write("thumbnail.jpg")
        self._write("notes.txt")
        self.assertFalse(self.fired.wait(timeout=1.0))

    def test_unreadable_header_still_reports_the_file(self):
        self.watcher.start()
        self._write("garbage.fits", data=b"\x00\x01this is not FITS")
        self.assertTrue(self.fired.wait(timeout=5))
        self.assertEqual(self.events[0]["header"], {})  # header {} but event fired

    def test_missing_watch_path_fails_visibly_not_fatally(self):
        watcher = ImageWatcher("does_not_exist", self._cb)
        watcher.start()  # must not raise
        self.assertFalse(watcher._running)  # supervisor sees it as unhealthy

    def test_callback_exception_does_not_kill_watcher(self):
        def bad_cb(info):
            self.fired.set()
            raise RuntimeError("photometry exploded")

        watcher = ImageWatcher("watch", bad_cb, debounce_delay=0.2)
        watcher.start()
        try:
            self._write("boom.fits")
            self.assertTrue(self.fired.wait(timeout=5))
            self.fired.clear()
            self._write("after_boom.fits")
            self.assertTrue(self.fired.wait(timeout=5))  # still alive
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
