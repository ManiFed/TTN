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


def _write_science_fits(path, *, exptime=30.0):
    header = fits.Header()
    header["DATE-OBS"] = "2026-09-13T03:00:00"
    header["EXPTIME"] = exptime
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    fits.writeto(path, np.arange(100, dtype=np.float32).reshape(10, 10), header)


# Issue #88: science_frame must flip to ok regardless of ingest path, and
# report which path produced the evidence.

def test_evidence_from_fits_export_flips_science_frame_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    path = tmp_path / "export.fits"
    _write_science_fits(path)
    mgr = _manager(tmp_path)
    mgr.evaluate()
    mgr.observe_fits(str(path), source="fits_export")
    state = mgr.evaluate()
    assert state["checks"]["science_frame"]["ok"] is True
    assert state["checks"]["science_frame"]["source"] == "fits_export"
    assert state["science_frame_source"] == "fits_export"


def test_evidence_from_myworks_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    path = tmp_path / "myworks.fits"
    _write_science_fits(path)
    mgr = _manager(tmp_path)
    mgr.evaluate()
    mgr.observe_fits(str(path), source="myworks")
    state = mgr.evaluate()
    assert state["checks"]["science_frame"]["ok"] is True
    assert state["checks"]["science_frame"]["source"] == "myworks"


def test_duplicate_observation_keeps_first_source(tmp_path, monkeypatch):
    """A frame legitimately observed by more than one path (e.g. the
    watcher that ingested it and the enqueue it flows through) must not
    overwrite the recorded source or double-count evidence."""
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    path = tmp_path / "dup.fits"
    _write_science_fits(path)
    mgr = _manager(tmp_path)
    mgr.evaluate()
    mgr.observe_fits(str(path), source="myworks")
    mgr.observe_fits(str(path), source="fits_export")
    state = mgr.status()
    assert len(state["evidence"]) == 1
    assert state["science_frame_source"] == "myworks"


def test_default_source_reported_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.commissioning.shutil.disk_usage",
        lambda _: type("Usage", (), {"free": 20 * 1024 ** 3})(),
    )
    path = tmp_path / "unknown.fits"
    _write_science_fits(path)
    mgr = _manager(tmp_path)
    mgr.evaluate()
    mgr.observe_fits(str(path))
    state = mgr.evaluate()
    assert state["checks"]["science_frame"]["source"] == "unknown"
