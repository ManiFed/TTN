"""
AAVSO reporting timestamps.

The pipeline records BJD_TDB. The Extended File Format's #DATE= accepts JD,
HJD or EXCEL and nothing else, and BJD_TDB sits ~68 s away from HJD_UTC, so
what goes into a submission has to be converted rather than relabelled. These
tests pin both emitters (node single-observation, cloud batch) to that.
"""

from pathlib import Path

import pytest

from cloud import data_pipeline as DP
from src import aavso_submission as A

# Z Cam — a real target with a real position, so the light-travel terms are
# representative rather than degenerate.
RA, DEC = 126.30492, 73.11086
BJD = 2461251.715127


def _row(**kw):
    row = {
        "id": 1, "node_id": "node_007", "target_name": "Z Cam",
        "bjd": BJD, "hjd": None, "magnitude": 11.6, "uncertainty": 0.09,
        "filter": "CV", "airmass": 1.21, "snr": 25.2, "comparison_stars": 14,
        "zp_scatter": 0.28, "validation_status": "consistent",
        "quality_flag": "good", "calibration_state": "", "network_magnitude": None,
        "target_ra_deg": RA, "target_dec_deg": DEC,
    }
    row.update(kw)
    return row


# ── cloud batch ────────────────────────────────────────────────────────────────

def test_uploaded_hjd_is_used_verbatim():
    rows, undatable = DP._partition_by_reportable_date([_row(hjd=2461251.714340)])
    assert not undatable
    assert rows[0]["_hjd"] == pytest.approx(2461251.714340)


def test_legacy_row_without_hjd_is_converted_from_its_bjd():
    rows, undatable = DP._partition_by_reportable_date([_row()])
    assert not undatable
    offset_s = (BJD - rows[0]["_hjd"]) * 86400.0
    assert 60.0 < offset_s < 80.0, f"BJD−HJD = {offset_s:.1f} s"


def test_row_with_no_hjd_and_no_coordinates_is_held_back():
    """Better an unsubmitted observation than one a minute out of place."""
    rows, undatable = DP._partition_by_reportable_date(
        [_row(target_ra_deg=None, target_dec_deg=None)])
    assert rows == []
    assert len(undatable) == 1


def test_batch_header_declares_hjd_and_notes_keep_the_bjd():
    rows, _ = DP._partition_by_reportable_date([_row()])
    text = DP._format_batch(rows, "EGBA", {"chart_id": "X42585HPK"})
    lines = text.strip().split("\n")
    header = [l for l in lines if l.startswith("#")]
    data = [l for l in lines if not l.startswith("#")]

    assert "#DATE=HJD" in header
    assert not any("BJD" in h for h in header)

    fields = data[0].split(",")
    assert float(fields[1]) == pytest.approx(rows[0]["_hjd"], abs=1e-6)
    assert float(fields[1]) != pytest.approx(BJD, abs=1e-6)
    assert f"bjd_tdb={BJD:.6f}" in fields[14]


# ── node single observation ────────────────────────────────────────────────────

def test_node_prefers_the_measured_hjd():
    assert A.aavso_date({"hjd": 2461251.7, "bjd": BJD}) == 2461251.7


def test_node_converts_when_only_a_bjd_is_present():
    hjd = A.aavso_date({"bjd": BJD, "ra_deg": RA, "dec_deg": DEC})
    assert 60.0 < (BJD - hjd) * 86400.0 < 80.0


def test_node_refuses_to_report_a_bjd_as_an_hjd():
    with pytest.raises(ValueError):
        A.aavso_date({"bjd": BJD})


# ── issue #87: WebObs status+body logging and response persistence ─────────────
#
# Operators could not tell why a node/cloud submission claimed success while
# AAVSO's "My Observations" was empty, because only a truncated warning was
# logged for *unrecognised* bodies and the cloud batch response was never
# written to disk at all. Every POST attempt must now leave an auditable
# trail: an HTTP status + bounded body snippet in the logs, and (cloud) a
# persisted response file whose path is recorded and surfaced.

class _FakeResp:
    def __init__(self, status_code, text, content_type="text/html; charset=utf-8"):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": content_type}


def test_cloud_post_batch_logs_status_and_body_on_success(monkeypatch, tmp_path, caplog):
    resp = _FakeResp(200, "Thanks! 2 observation(s) were uploaded successfully.")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)

    file_path = tmp_path / "batch_20260101T000000.txt"
    file_path.write_text("dummy", encoding="utf-8")

    with caplog.at_level("INFO", logger="cloud.data_pipeline"):
        status, accepted, rejected, message, response_path = DP._post_batch(
            "text", "user", "pass", "https://example.test/webobs", file_path=file_path)

    assert status == "accepted"
    assert accepted == 2
    assert any("HTTP 200" in r.message and "body=" in r.message for r in caplog.records)
    assert response_path == str(file_path) + "_response.txt"
    assert Path(response_path).read_text(encoding="utf-8") == resp.text


def test_cloud_post_batch_logs_status_and_body_on_unrecognised_response(
        monkeypatch, tmp_path, caplog):
    resp = _FakeResp(200, "<html>please sign in</html>")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)

    file_path = tmp_path / "batch_20260101T010000.txt"
    file_path.write_text("dummy", encoding="utf-8")

    with caplog.at_level("INFO", logger="cloud.data_pipeline"):
        status, accepted, rejected, message, response_path = DP._post_batch(
            "text", "user", "pass", "https://example.test/webobs", file_path=file_path)

    assert status == "error"
    # Status+body must be logged even though the body could not be parsed.
    assert any("HTTP 200" in r.message and "body=" in r.message for r in caplog.records)
    assert response_path is not None
    assert Path(response_path).exists()


def test_cloud_post_batch_logs_status_on_non_2xx(monkeypatch, tmp_path, caplog):
    resp = _FakeResp(500, "internal server error")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)

    file_path = tmp_path / "batch_20260101T020000.txt"
    file_path.write_text("dummy", encoding="utf-8")

    with caplog.at_level("INFO", logger="cloud.data_pipeline"):
        status, accepted, rejected, message, response_path = DP._post_batch(
            "text", "user", "pass", "https://example.test/webobs", file_path=file_path)

    assert status == "error"
    assert "HTTP 500" in message
    assert any("HTTP 500" in r.message and "body=" in r.message for r in caplog.records)
    assert response_path is not None


def test_submit_pending_batch_persists_and_returns_response_path(monkeypatch, tmp_path):
    """submit_pending_batch must record the response file both in its return
    value and in the aavso_batches row, so operators/MCP tooling can find it
    without touching the filesystem directly (issue #87)."""
    row = _row(id=1)
    monkeypatch.setattr(DP.db, "query", lambda *a, **k: [row])
    monkeypatch.setattr(DP.db, "executemany", lambda *a, **k: None)

    inserted = {}

    def _fake_execute(sql, params):
        if "INSERT INTO aavso_batches" in sql:
            inserted["sql"] = sql
            inserted["params"] = params

    monkeypatch.setattr(DP.db, "execute", _fake_execute)

    resp = _FakeResp(200, "Thanks! 1 observation(s) were uploaded successfully.")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)

    config = {"aavso": {
        "observer_code": "EGBA", "dry_run": False,
        "username": "user", "password": "pass",
        "audit_dir": str(tmp_path),
    }}

    result = DP.submit_pending_batch(config)

    assert result["status"] == "accepted"
    assert result["response_path"]
    assert Path(result["response_path"]).exists()
    # response_path must have been persisted into the aavso_batches insert too.
    assert "response_path" in inserted["sql"]
    assert result["response_path"] in inserted["params"]


def test_dry_run_never_marks_measurements_submitted(monkeypatch, tmp_path):
    """A dry_run batch never POSTs to WebObs, so it must not flip
    aavso_submitted=1 -- doing so would permanently exclude real
    measurements from ever actually reaching AAVSO once dry_run is turned
    back off (they'd never again match `WHERE aavso_submitted = 0`)."""
    row = _row(id=1)
    monkeypatch.setattr(DP.db, "query", lambda *a, **k: [row])
    monkeypatch.setattr(DP.db, "execute", lambda *a, **k: None)

    marked_submitted = []

    def _fake_executemany(sql, seq):
        if "aavso_submitted = 1" in sql:
            marked_submitted.extend(seq)

    monkeypatch.setattr(DP.db, "executemany", _fake_executemany)

    config = {"aavso": {
        "observer_code": "EGBA", "dry_run": True,
        "audit_dir": str(tmp_path),
    }}

    result = DP.submit_pending_batch(config)

    assert result["status"] == "dry_run"
    assert marked_submitted == []
