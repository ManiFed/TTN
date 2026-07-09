import json

import numpy as np
from astropy.io import fits

from src.commissioning import CommissioningManager


def _manager(tmp_path, *, registered=True, connected=True):
    watch = tmp_path / "watch"
    watch.mkdir()
    solver = tmp_path / "astap"
    solver.write_text("")
    cfg = {
        "safety": {"observer": {"latitude": 40.0, "longitude": -105.0}},
        "image_watcher": {"watch_path": str(watch)},
        "photometry": {"astap_path": str(solver)},
    }
    return CommissioningManager(
        load_config=lambda: cfg,
        is_registered=lambda: registered,
        runtime_status=lambda: {
            "telescope_connected": connected,
            "camera_connected": connected,
        },
        telescope_specs=lambda: {"aperture_mm": 50.0, "fov_deg": 1.2},
        state_path=str(tmp_path / "commissioning.json"),
    )


def test_waits_for_signup(tmp_path):
    mgr = _manager(tmp_path, registered=False)
    state = mgr.evaluate()
    assert state["status"] == "waiting_for_signup"
    assert state["certification"] == "uncommissioned"


def test_registration_automatically_commissions_ready_node(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    mgr = _manager(tmp_path)
    state = mgr.evaluate()
    assert state["status"] == "complete"
    assert state["certification"] == "operational"
    assert state["capabilities"]["aperture_mm"] == 50.0
    persisted = json.loads((tmp_path / "commissioning.json").read_text())
    assert persisted["certification"] == "operational"


def test_disconnected_hardware_stays_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    state = _manager(tmp_path, connected=False).evaluate()
    assert state["status"] == "evaluating"
    assert not state["checks"]["camera"]["ok"]


def test_first_fits_adds_scientific_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    path = tmp_path / "science.fits"
    header = fits.Header()
    header["DATE-OBS"] = "2026-07-08T03:00:00"
    header["EXPTIME"] = 30.0
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    fits.writeto(path, np.arange(100, dtype=np.float32).reshape(10, 10), header)
    mgr = _manager(tmp_path)
    mgr.evaluate()
    mgr.observe_fits(str(path))
    state = mgr.status()
    assert len(state["evidence"]) == 1
    assert state["capabilities"]["fits_timing_keywords"] is True
    assert state["capabilities"]["fits_wcs_present"] is True
