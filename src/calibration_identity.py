"""Stable response fingerprints for the network calibration mesh."""

from __future__ import annotations

import hashlib
import json


def response_descriptor(config: dict, node_id: str = "") -> dict:
    """Return the auditable fields that define one instrumental response."""
    phot = config.get("photometry") or {}
    obs = config.get("observatory") or {}
    tel = config.get("telescope") or {}
    cam = config.get("camera") or {}
    return {
        "node_id": node_id or phot.get("node_id", ""),
        "telescope_model": obs.get("telescope") or tel.get("model") or "",
        "telescope_identity": (obs.get("telescope_id") or tel.get("serial_number")
                               or tel.get("unique_id") or ""),
        "camera_model": obs.get("instrument") or cam.get("model") or "",
        "sensor_identity": (cam.get("sensor_id") or cam.get("serial_number")
                            or cam.get("sensor_name") or obs.get("sensor_id") or ""),
        "filter": phot.get("filter_name", "CV"),
        "physical_filter": phot.get("filter_id") or phot.get("filter_name", "CV"),
        "gain": phot.get("gain"),
        "binning": cam.get("binning", 1),
        "pixel_scale": phot.get("pixel_scale"),
        "hardware_characterization_version": phot.get(
            "hardware_characterization_version", "1"),
        "processing": {
            "mode": phot.get("processing_mode", "aperture_photometry"),
            "aperture_factor": phot.get("aperture_factor"),
            "bjd_midpoint_correction": phot.get("bjd_midpoint_correction", True),
        },
    }


def response_fingerprint(config: dict, node_id: str = "") -> str:
    """Hash only fields that can materially change a photometric response."""
    payload = response_descriptor(config, node_id)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "resp_" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def response_family(config: dict) -> str:
    """Equipment-family prior identity; deliberately excludes node identity."""
    phot = config.get("photometry") or {}
    obs = config.get("observatory") or {}
    cam = config.get("camera") or {}
    payload = {
        "telescope_model": obs.get("telescope") or "",
        "camera_model": obs.get("instrument") or cam.get("model") or "",
        "sensor_name": cam.get("sensor_name") or "",
        "filter": phot.get("filter_name", "CV"),
        "gain": phot.get("gain"), "binning": cam.get("binning", 1),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "family_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
