#!/usr/bin/env python3
"""
Companion — contributor mode for EVERY LENS ON EARTH.

Runs next to any imaging setup (no telescope control, no scheduler) and watches
one or more folders where an astrophotographer's FITS land. Each new frame is
uploaded to the cloud with the member's token; the cloud plate-solves and
extracts it. Years of existing images can be swept in one pass too. This is how
a node becomes *any camera*, not just a Seestar running the full agent.

Config (`contributor:` block):

    contributor:
      enabled: true
      member_token: ${BS_MEMBER_TOKEN}   # from the app/website login
      cloud_url: https://api.thetelescope.net
      watch_dirs:
        - /Users/me/Astrophotography/lights
      scan_existing: true                # upload what's already there on start
      debounce_delay: 3.0

Standalone:  python -m src.companion config.yaml
"""

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from src.image_watcher import ImageWatcher
from src.shared_models import expand_env

logger = logging.getLogger("companion")

_FITS_EXT = (".fits", ".fit")
_STATE_FILE = Path("data") / "companion_uploaded.txt"


class Companion:
    def __init__(self, config: dict) -> None:
        cc = config.get("contributor", {}) or {}
        self._url = str(cc.get("cloud_url")
                        or config.get("cloud", {}).get("url", "")).rstrip("/")
        self._token = str(expand_env(cc.get("member_token", "")) or "")
        self._dirs = [d for d in (cc.get("watch_dirs") or []) if d]
        self._scan_existing = bool(cc.get("scan_existing", True))
        self._debounce = float(cc.get("debounce_delay", 3.0))
        self._watchers: list[ImageWatcher] = []
        self._uploaded = _load_uploaded()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.stats = {"uploaded": 0, "skipped": 0, "failed": 0}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._url or not self._token:
            logger.error("Companion needs cloud_url and member_token — not started")
            return
        if not self._dirs:
            logger.error("Companion has no watch_dirs — not started")
            return
        for d in self._dirs:
            if not os.path.isdir(d):
                logger.warning("Companion watch dir missing: %s", d)
                continue
            w = ImageWatcher(d, self._on_file, debounce_delay=self._debounce)
            w.start()
            self._watchers.append(w)
        logger.info("Companion watching %d dir(s) → %s",
                    len(self._watchers), self._url)
        if self._scan_existing:
            threading.Thread(target=self._scan, daemon=True,
                             name="companion-scan").start()

    def stop(self) -> None:
        self._stop.set()
        for w in self._watchers:
            w.stop()

    # ── Upload ─────────────────────────────────────────────────────────────────

    def _on_file(self, info: dict) -> None:
        self.upload_file(info["path"])

    def upload_file(self, path: str) -> dict:
        """Upload one FITS to /api/v1/me/contributions. Deduped locally by
        sha256 so a restart/rescan never re-sends the same frame."""
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            logger.warning("Companion cannot read %s: %s", path, exc)
            self.stats["failed"] += 1
            return {"ok": False, "error": str(exc)}
        sha = hashlib.sha256(raw).hexdigest()
        with self._lock:
            if sha in self._uploaded:
                self.stats["skipped"] += 1
                return {"ok": True, "skipped": True}

        import requests
        try:
            resp = requests.post(
                self._url + "/api/v1/me/contributions",
                headers={"Authorization": f"Bearer {self._token}"},
                files={"file": (os.path.basename(path), raw,
                                "application/octet-stream")},
                timeout=60)
        except Exception as exc:
            logger.warning("Companion upload failed for %s: %s",
                           os.path.basename(path), exc)
            self.stats["failed"] += 1
            return {"ok": False, "error": str(exc)}

        if resp.status_code == 200:
            self._mark_uploaded(sha)
            self.stats["uploaded"] += 1
            logger.info("Contributed %s", os.path.basename(path))
            return {"ok": True, "status": 200}
        if resp.status_code == 409:
            # Server already has this frame (someone else, or a prior run).
            self._mark_uploaded(sha)
            self.stats["skipped"] += 1
            return {"ok": True, "status": 409, "duplicate": True}
        if resp.status_code == 429:
            logger.info("Companion hit the daily contribution cap — pausing")
            self._stop.wait(60)
            self.stats["failed"] += 1
            return {"ok": False, "status": 429}
        # 4xx (bad frame, not a LIGHT, etc.): don't retry endlessly — mark it.
        self._mark_uploaded(sha)
        self.stats["failed"] += 1
        return {"ok": False, "status": resp.status_code,
                "error": resp.text[:200]}

    def _mark_uploaded(self, sha: str) -> None:
        with self._lock:
            self._uploaded.add(sha)
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_STATE_FILE, "a") as fh:
                fh.write(sha + "\n")
        except OSError:
            pass

    def _scan(self) -> None:
        """One-shot sweep of existing FITS in the watch dirs (archive upload)."""
        for d in self._dirs:
            for root, _, files in os.walk(d):
                for name in files:
                    if self._stop.is_set():
                        return
                    if name.lower().endswith(_FITS_EXT):
                        self.upload_file(os.path.join(root, name))
        logger.info("Companion archive scan complete: %s", self.stats)


def _load_uploaded() -> set:
    try:
        return set(_STATE_FILE.read_text().split())
    except OSError:
        return set()


def main() -> None:
    import sys
    import yaml
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    with open(path) as fh:
        config = yaml.safe_load(fh) or {}
    comp = Companion(config)
    comp.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        comp.stop()


if __name__ == "__main__":
    main()
