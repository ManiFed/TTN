#!/usr/bin/env python3
"""
Validation of the quality gates, AAVSO Extended Format output, WebObs response
parsing, measurement ingest bounds, and cloud cross-validation logic.

The cloud database is stubbed (no PostgreSQL, no network).
Run:  pytest tests/validation -q
"""

import pytest

from src.photometry import evaluate_quality
from src.shared_models import Measurement
import src.aavso_submission as A


_CFG = {}   # gate defaults: snr 20, unc 0.3, min_comp 3, airmass 3.0,
            # zp warn/max 0.15/0.30


def _m(**over):
    base = {"snr": 45.0, "uncertainty": 0.08, "n_comparison_stars": 6,
            "airmass": 1.3, "zp_scatter": 0.05, "target_saturated": False,
            "target_blended": False}
    base.update(over)
    return base


# ── evaluate_quality ───────────────────────────────────────────────────────────

def test_clean_measurement_is_good_with_no_reasons():
    flag, reasons = evaluate_quality(_m(), _CFG)
    assert flag == "good" and reasons == []


@pytest.mark.parametrize("metrics,expected,check", [
    (_m(snr=15), "acceptable", "snr"),                    # warn zone
    (_m(snr=8), "poor", "snr"),                           # below half threshold
    (_m(uncertainty=0.35), "acceptable", "uncertainty"),
    (_m(uncertainty=0.50), "poor", "uncertainty"),
    (_m(n_comparison_stars=2), "acceptable", "comparison_stars"),
    (_m(n_comparison_stars=1), "poor", "comparison_stars"),
    (_m(airmass=3.4), "acceptable", "airmass"),
    (_m(zp_scatter=0.20), "acceptable", "zp_scatter"),
    (_m(zp_scatter=0.35), "poor", "zp_scatter"),
    (_m(target_saturated=True), "poor", "target_saturated"),
    (_m(target_blended=True), "poor", "target_blended"),
    (_m(airmass=None), "acceptable", "airmass"),
])
def test_gate_matrix(metrics, expected, check):
    flag, reasons = evaluate_quality(metrics, _CFG)
    assert flag == expected
    assert any(r["check"] == check for r in reasons)
    for r in reasons:
        assert set(r) == {"check", "value", "threshold", "outcome"}
        assert r["outcome"] in ("warn", "fail")


def test_saturation_overrides_otherwise_perfect_metrics():
    flag, reasons = evaluate_quality(_m(snr=500, uncertainty=0.01,
                                        target_saturated=True), _CFG)
    assert flag == "poor"


def test_gates_match_legacy_flag_logic():
    """For non-saturated, low-zp-scatter measurements the new gate function
    must reproduce the historical inline flag logic exactly (no silent
    loosening or tightening of the submission gate)."""
    def legacy(snr, unc, n, am):
        if snr >= 20 and unc < 0.3 and n >= 3 and am < 3.0:
            return "good"
        if snr >= 10 and unc < 0.45 and n >= 2:
            return "acceptable"
        return "poor"

    for snr in (5.0, 10.0, 15.0, 20.0, 45.0):
        for unc in (0.05, 0.29, 0.31, 0.44, 0.46):
            for n in (1, 2, 3, 7):
                for am in (1.2, 2.9, 3.1):
                    flag, _ = evaluate_quality(_m(
                        snr=snr, uncertainty=unc,
                        n_comparison_stars=n, airmass=am), _CFG)
                    assert flag == legacy(snr, unc, n, am), (
                        f"divergence at snr={snr} unc={unc} n={n} am={am}: "
                        f"new={flag} legacy={legacy(snr, unc, n, am)}"
                    )


# ── Measurement ingest bounds ──────────────────────────────────────────────────

def _meas(**over):
    base = dict(target_name="V TEST", bjd=2461000.5, magnitude=12.3,
                uncertainty=0.08, quality_flag="good")
    base.update(over)
    return Measurement.from_dict(base)


@pytest.mark.parametrize("kwargs,valid", [
    ({}, True),
    ({"target_name": ""}, False),
    ({"bjd": 61000.5}, False),           # MJD passed as BJD must be caught
    ({"bjd": 2600000.0}, False),
    ({"magnitude": 45.0}, False),
    ({"magnitude": -9.0}, False),
    ({"uncertainty": -0.1}, False),
    ({"uncertainty": 6.0}, False),
    ({"quality_flag": "excellent"}, False),
])
def test_measurement_bounds(kwargs, valid):
    assert _meas(**kwargs).is_valid() is valid


def test_measurement_ignores_unknown_keys():
    """provenance/quality_reasons in uploads must not break cloud ingestion."""
    m = Measurement.from_dict({
        "target_name": "V TEST", "bjd": 2461000.5, "magnitude": 12.3,
        "uncertainty": 0.08, "quality_flag": "good",
        "provenance": {"pipeline_version": "1.1.0"},
        "quality_reasons": [], "patrol_alerts": [],
    })
    assert m.is_valid()


# ── AAVSO Extended File Format ─────────────────────────────────────────────────

_MEASUREMENT = {
    "target_name": "T CrB, weird", "bjd": 2461000.123456,
    "magnitude": 10.1234, "uncertainty": 0.0567, "filter": "CV",
    "airmass": 1.345, "fwhm": 3.9, "snr": 41.2, "comparison_stars": 6,
    "quality_flag": "good", "node_id": "node_007",
    "zero_point": 21.02, "zp_scatter": 0.05, "fits_file": "x.fits",
}


def test_extended_format_structure():
    text = A._format_extended(_MEASUREMENT, "XYZA", {})
    lines = text.strip().split("\n")
    header = [l for l in lines if l.startswith("#")]
    rows = [l for l in lines if not l.startswith("#")]
    assert "#TYPE=Extended" in header
    assert "#OBSCODE=XYZA" in header
    assert "#DATE=BJD" in header
    assert "#DELIM=," in header
    assert len(rows) == 1
    fields = rows[0].split(",")
    assert len(fields) == 15, f"expected 15 fields, got {len(fields)}: {fields}"
    name, date, mag, merr, filt, trans, mtype, cname = fields[:8]
    assert "," not in name and name == "T CrB  weird"
    assert date == "2461000.123456"
    assert mag == "10.123" and merr == "0.057"
    assert filt == "CV" and trans == "NO" and mtype == "DIFF"
    assert cname == "ENSEMBLE"
    assert fields[11] == f"{1.345:.2f}"   # AMASS, 2 dp
    assert "quality=good" in fields[14]


def test_extended_format_missing_airmass():
    m = dict(_MEASUREMENT, airmass=None)
    text = A._format_extended(m, "XYZA", {})
    row = [l for l in text.strip().split("\n") if not l.startswith("#")][0]
    assert row.split(",")[11] == "na"


# ── WebObs response parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize("body,status,expected", [
    ("Thanks! 3 observations were uploaded successfully.", 200, (3, 0)),
    ("<html>Error: invalid magnitude on line 2</html>", 200, (0, 1)),
    # Auth0/WAF challenge page: HTTP 200, no token → must NOT count as accepted
    ("<html><body>Please verify you are human</body></html>", 200, (0, 0)),
    ("Internal Server Error", 500, (0, 1)),
])
def test_webobs_response_parsing(body, status, expected):
    assert A._parse_webobs_response(body, status) == expected


# ── Cloud cross-validation (stubbed db) ────────────────────────────────────────

class _FakeDB:
    """Captures UPDATEs; serves canned SELECT rows."""
    def __init__(self, rows):
        self.rows = rows
        self.updates = []   # (status, id)

    def query(self, sql, params=()):
        return [dict(r) for r in self.rows]

    def execute(self, sql, params=(), returning_id=False):
        self.updates.append(params)
        return 0


@pytest.fixture
def dp(monkeypatch):
    import cloud.data_pipeline as dp
    monkeypatch.setattr(dp.incidents, "log",
                        lambda *a, **k: None, raising=True)
    return dp


def test_cross_validate_marks_outlier(dp, monkeypatch):
    rows = [
        {"id": 1, "node_id": "n1", "magnitude": 12.00, "uncertainty": 0.05},
        {"id": 2, "node_id": "n2", "magnitude": 12.02, "uncertainty": 0.05},
        {"id": 3, "node_id": "n3", "magnitude": 12.90, "uncertainty": 0.05},
    ]
    fake = _FakeDB(rows)
    monkeypatch.setattr(dp, "db", fake)
    dp.cross_validate("V TEST", 2461000.5)
    statuses = {pid: status for status, pid in fake.updates}
    assert statuses[1] == "consistent"
    assert statuses[2] == "consistent"
    assert statuses[3] == "outlier"


def test_cross_validate_small_deviation_within_uncertainty_ok(dp, monkeypatch):
    """0.35 mag apart but σ=0.2 → 3σ rule keeps it consistent."""
    rows = [
        {"id": 1, "node_id": "n1", "magnitude": 12.00, "uncertainty": 0.20},
        {"id": 2, "node_id": "n2", "magnitude": 12.35, "uncertainty": 0.20},
    ]
    fake = _FakeDB(rows)
    monkeypatch.setattr(dp, "db", fake)
    dp.cross_validate("V TEST", 2461000.5)
    assert all(s == "consistent" for s, _ in fake.updates)


def test_cross_validate_single_measurement(dp, monkeypatch):
    rows = [{"id": 9, "node_id": "n1", "magnitude": 12.0, "uncertainty": 0.05}]
    fake = _FakeDB(rows)
    monkeypatch.setattr(dp, "db", fake)
    dp.cross_validate("V TEST", 2461000.5)
    assert fake.updates == [(9,)]   # marked 'single'


def test_consensus_weighted_mean(dp, monkeypatch):
    rows = [
        {"node_id": "n1", "bjd": 2461000.50, "magnitude": 12.00, "uncertainty": 0.05},
        {"node_id": "n2", "bjd": 2461000.51, "magnitude": 12.10, "uncertainty": 0.10},
    ]
    fake = _FakeDB(rows)
    monkeypatch.setattr(dp, "db", fake)
    c = dp.compute_consensus("V TEST", 2461000.5)
    assert c is not None
    # inverse-variance weights 400:100 → mean = 12.02
    assert c["magnitude"] == pytest.approx(12.02, abs=0.005)
    assert c["n_nodes"] == 2
    # uncertainty is never below the formal error, never hides scatter
    assert c["uncertainty"] >= 1.0 / (400 + 100) ** 0.5 - 1e-9


def test_consensus_requires_two_nodes(dp, monkeypatch):
    fake = _FakeDB([{"node_id": "n1", "bjd": 2461000.5,
                     "magnitude": 12.0, "uncertainty": 0.05}])
    monkeypatch.setattr(dp, "db", fake)
    assert dp.compute_consensus("V TEST", 2461000.5) is None
