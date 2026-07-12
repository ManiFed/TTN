#!/usr/bin/env python3
"""
Cloud-side plate solving for universal frame ingestion.

Contributed frames arrive without a WCS. This module gives the ingest worker a
single `solve(path, ...)` call that:

  1. tries local Astrometry.net `solve-field` (src/plate_solve.solve) — fast
     when the index files for the frame's scale are on the worker's volume;
  2. optionally falls back to the nova.astrometry.net web API for frames outside
     the local index range (rate-limited, minutes of latency — archive only,
     never the live path).

Config (cloud config `solver:` block):

    solver:
      solve_field_path: solve-field
      search_radius_deg: 15
      timeout_s: 180
      web_fallback: false            # enable nova.astrometry.net
      web_api_key: ${ASTROMETRY_API_KEY}
      web_timeout_s: 300
"""

import logging
import time

logger = logging.getLogger("cloud.solver")


def _cfg(config: dict) -> dict:
    s = config.get("solver", {}) or {}
    return {
        "solve_field_path": s.get("solve_field_path", "solve-field"),
        "search_radius_deg": float(s.get("search_radius_deg", 15.0)),
        "timeout_s": float(s.get("timeout_s", 180.0)),
        "web_fallback": bool(s.get("web_fallback", False)),
        "web_api_key": str(s.get("web_api_key", "") or ""),
        "web_timeout_s": float(s.get("web_timeout_s", 300.0)),
        "scratch_dir": s.get("scratch_dir", "/tmp"),
    }


def solve(path: str, config: dict,
          hint_ra: float | None = None,
          hint_dec: float | None = None,
          hint_scale: float | None = None) -> dict | None:
    """Solve a frame, writing the WCS in place. Returns the solution dict or None."""
    cfg = _cfg(config)
    from src import plate_solve
    info = plate_solve.solve(
        path, hint_ra=hint_ra, hint_dec=hint_dec, hint_scale=hint_scale,
        solve_field_path=cfg["solve_field_path"],
        search_radius=cfg["search_radius_deg"],
        timeout_s=cfg["timeout_s"], scratch_dir=cfg["scratch_dir"])
    if info is not None:
        info["solver"] = "local"
        return info
    if cfg["web_fallback"] and cfg["web_api_key"]:
        info = _solve_web(path, cfg, hint_scale)
        if info is not None:
            info["solver"] = "nova"
            return info
    return None


# ── nova.astrometry.net web API fallback ────────────────────────────────────────

_NOVA = "https://nova.astrometry.net/api"


def _solve_web(path: str, cfg: dict, hint_scale: float | None) -> dict | None:
    """Blind-solve via nova.astrometry.net and inject the returned WCS.

    Best-effort and slow; used only for archive frames the local index misses.
    """
    import json
    import requests

    try:
        sess = _nova_login(cfg["web_api_key"])
        if sess is None:
            return None
        with open(path, "rb") as fh:
            files = {"file": (path.rsplit("/", 1)[-1], fh, "application/octet-stream")}
            req = {"session": sess, "publicly_visible": "n",
                   "allow_modifications": "n", "allow_commercial_use": "n"}
            r = requests.post(f"{_NOVA}/upload",
                              data={"request-json": json.dumps(req)},
                              files=files, timeout=60)
        sub = r.json().get("subid")
        if not sub:
            logger.warning("nova upload rejected: %s", r.text[:200])
            return None
        job_id = _nova_await_job(sub, cfg["web_timeout_s"])
        if job_id is None:
            return None
        # Retrieve the WCS file the job produced and inject it.
        wcs = requests.get(f"https://nova.astrometry.net/wcs_file/{job_id}",
                           timeout=60)
        import tempfile
        import os
        wcs_path = os.path.join(cfg["scratch_dir"], f"nova_{job_id}.wcs")
        with open(wcs_path, "wb") as fh:
            fh.write(wcs.content)
        try:
            from src.plate_solve import _read_solution, _write_wcs
            info = _read_solution(wcs_path)
            if _write_wcs(path, wcs_path):
                info["wcs_written"] = True
                return info
        finally:
            try:
                os.unlink(wcs_path)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("nova web solve failed: %s", exc)
    return None


def _nova_login(api_key: str):
    import json
    import requests
    try:
        r = requests.post(f"{_NOVA}/login",
                          data={"request-json": json.dumps({"apikey": api_key})},
                          timeout=30)
        data = r.json()
        return data.get("session") if data.get("status") == "success" else None
    except Exception as exc:
        logger.warning("nova login failed: %s", exc)
        return None


def _nova_await_job(subid, timeout_s: float):
    import requests
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            sub = requests.get(f"{_NOVA}/submissions/{subid}", timeout=30).json()
            jobs = sub.get("jobs") or []
            if jobs and jobs[0]:
                status = requests.get(
                    f"{_NOVA}/jobs/{jobs[0]}", timeout=30).json().get("status")
                if status == "success":
                    return jobs[0]
                if status == "failure":
                    return None
        except Exception:
            pass
        time.sleep(5)
    logger.warning("nova job for submission %s timed out", subid)
    return None
