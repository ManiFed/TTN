#!/usr/bin/env python3
"""
ImageWatcher — monitors a directory for new Seestar FITS files.

Uses watchdog (inotify/FSEvents/kqueue) so there is no polling.
A configurable debounce delay lets partial writes finish before the
callback fires.  The callback receives a dict:

    {
        "path":   "/mnt/seestar/foo.fits",
        "header": { "OBJECT": "M31", "EXPTIME": 30.0, ... },   # or {}
        "size_kb": 12345.6,
    }
"""

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger("image_watcher")

_FITS_EXTENSIONS = {".fits", ".fit"}


class ImageWatcher:
    def __init__(
        self,
        watch_path: str,
        callback: Callable[[dict], None],
        debounce_delay: float = 2.0,
    ) -> None:
        self._path          = watch_path
        self._callback      = callback
        self._debounce      = debounce_delay
        self._timers: dict[str, threading.Timer] = {}
        self._timers_lock   = threading.Lock()
        self._observer      = None
        self._running       = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        if not os.path.isdir(self._path):
            logger.error(
                "Image watcher not started — watch path does not exist: %s",
                self._path,
            )
            return

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    watcher._schedule(event.src_path)

            def on_moved(self, event):
                # Atomic-rename completion (e.g. tmp → final.fits)
                if not event.is_directory:
                    watcher._schedule(event.dest_path)

        self._observer = Observer()
        self._observer.schedule(_Handler(), self._path, recursive=False)
        self._observer.start()
        self._running = True
        logger.info("Image watcher started: %s  (debounce %.1f s)", self._path, self._debounce)

    def stop(self) -> None:
        self._running = False
        with self._timers_lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        logger.info("Image watcher stopped")

    # ── Internal ───────────────────────────────────────────────────────────────

    def _schedule(self, path: str) -> None:
        ext = os.path.splitext(path)[1].lower()
        if ext not in _FITS_EXTENSIONS:
            return

        with self._timers_lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(self._debounce, self._fire, args=(path,))
            self._timers[path] = t
        t.start()

    def _fire(self, path: str) -> None:
        with self._timers_lock:
            self._timers.pop(path, None)

        if not self._running:
            return
        if not os.path.isfile(path):
            return

        logger.info("New FITS file detected: %s", os.path.basename(path))

        header = _read_fits_header(path)
        size_kb = os.path.getsize(path) / 1024.0

        try:
            self._callback({"path": path, "header": header, "size_kb": size_kb})
        except Exception as exc:
            logger.error("Image watcher callback raised: %s", exc)


def _read_fits_header(path: str) -> dict:
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False, ignore_missing_simple=True) as hdul:
            hdr = hdul[0].header
            return {k: (v.strip() if isinstance(v, str) else v)
                    for k, v in hdr.items()
                    if k not in ("", "COMMENT", "HISTORY")}
    except Exception as exc:
        logger.warning("Could not read FITS header (%s): %s", os.path.basename(path), exc)
        return {}
