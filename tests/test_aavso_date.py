"""
AAVSO reporting timestamps.

The pipeline records BJD_TDB. The Extended File Format's #DATE= accepts JD,
HJD or EXCEL and nothing else, and BJD_TDB sits ~68 s away from HJD_UTC, so
what goes into a submission has to be converted rather than relabelled. These
tests pin both emitters (node single-observation, cloud batch) to that.
"""

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
