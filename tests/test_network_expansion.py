"""Focused acceptance tests for calibration, GCN tiling and autonomy."""

import base64
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from cloud import autonomy as cloud_autonomy
from cloud.calibration import fit_samples
from cloud.event_tiling import generate_tiles
from cloud.gcn_consumer import decode_notice
from cloud.gcn_events import normalize, policy_decision
from src.autonomy import AutonomyStore
from src.durable_outbox import DurableOutbox


class CalibrationModelTests(unittest.TestCase):
    def test_recovers_injected_terms_on_held_out_stars(self):
        rng = np.random.default_rng(12)
        rows = []
        for night in range(8):
            for star in range(80):
                color = -0.3 + 2.0 * star / 79
                airmass = 1.0 + 1.2 * ((star + night) % 11) / 10
                bjd = 2460000.0 + night
                offset, cterm, extinction, drift = 0.08, 0.035, 0.12, 0.001
                residual = offset + cterm * (color - 0.7) + extinction * (airmass - 1) \
                    + drift * (bjd - 2460003.5)
                catalog = 11 + star / 40
                frame_zp = 20.0
                instr = catalog + residual - frame_zp + rng.normal(0, 0.006)
                rows.append({"source_key": f"star-{star}", "bjd": bjd,
                             "instrumental_mag": instr, "frame_zero_point": frame_zp,
                             "instrumental_err": 0.006, "catalog_mag": catalog,
                             "catalog_err": 0.004, "catalog_color": color,
                             "airmass": airmass})
        fit = fit_samples(rows, "V")
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["offset"], 0.08, delta=0.01)
        self.assertAlmostEqual(fit["color_term"], 0.035, delta=0.01)
        self.assertAlmostEqual(fit["extinction"], 0.12, delta=0.01)
        self.assertLess(fit["weighted_rms"], 0.02)


class EventTilingTests(unittest.TestCase):
    def test_probability_conserved_and_deterministic_across_ra_zero(self):
        loc = {"pixels": [
            {"ra_deg": 359.8, "dec_deg": 10.0, "probability": 0.25},
            {"ra_deg": 0.1, "dec_deg": 10.1, "probability": 0.35},
            {"ra_deg": 42.0, "dec_deg": 89.7, "probability": 0.40},
        ]}
        first = generate_tiles(loc, 0.6)
        second = generate_tiles(loc, 0.6)
        self.assertEqual(first, second)
        self.assertAlmostEqual(sum(t["probability_mass"] for t in first), 1.0, places=10)
        self.assertEqual(len(first), 2)

    def test_gcn_normalization_and_shadow_policy(self):
        notice = {"superevent_id": "S123", "role": "test", "significant": True,
                  "HasNS": 0.5, "ra": 15.0, "dec": -20.0, "error_radius": 2.0}
        event = normalize("gcn.notices.lvc.initial", notice)
        decision = policy_decision(event, {"gcn": {"live_dispatch": True}})
        self.assertEqual(event["source"], "lvk")
        self.assertEqual(event["localization_type"], "ellipse")
        self.assertTrue(decision["eligible"])
        self.assertFalse(decision["dispatch"])
        self.assertEqual(decode_notice(b'{"id":"x"}')["id"], "x")
        xml = b'<VOEvent ivorn="ivo://x" role="test"><What><Param name="TrigID" value="2"/></What></VOEvent>'
        self.assertEqual(decode_notice(xml)["TrigID"], "2")
        classic = decode_notice(b"NOTICE_TYPE: FERMI_GBM_FIN_POS\nTRIGGER_NUM: 42\n"
                                b"GRB_RA: 359.5d\nGRB_DEC: -20.0d\nERROR: 1.2d\n")
        self.assertEqual(classic["trigger_id"], "42")
        self.assertEqual(classic["ra"], 359.5)

    def test_current_schema_field_variants(self):
        notice = {"event_name": ["IceCube-230416A"], "record_number": 2,
                  "alert_tense": "current", "pipeline": "Gold Track Alert",
                  "trigger_time": "2026-07-15T00:00:00Z", "ra": 346.7,
                  "dec": 12.6, "ra_dec_error": [0.5, 0.6, 17],
                  "healpix_url": "https://example.invalid/map.fits", "p_astro": 0.7}
        event = normalize("gcn.notices.icecube.gold_bronze_track_alerts", notice)
        self.assertEqual(event["source_event_id"], "IceCube-230416A")
        self.assertEqual(event["revision"], 2)
        self.assertEqual(event["significance"]["class"], "Gold Track Alert")
        self.assertEqual(event["localization_type"], "healpix_moc")
        self.assertEqual(event["localization"]["storage_key"],
                         "https://example.invalid/map.fits")
        self.assertTrue(policy_decision(event, {"gcn": {}})["eligible"])

        chime = normalize("gcn.notices.chime.frb", {
            "id": "427", "ra": 346.7, "dec": 12.6,
            "ra_dec_error": [0.5, 0.6, 17]})
        self.assertEqual(chime["localization_type"], "ellipse")
        self.assertEqual(chime["localization"]["position_angle_deg"], 17)

        lvk = normalize("igwn.gwalert", {"superevent_id": "S1", "alert_type": "UPDATE",
            "event": {"time": "2026-07-15T00:00:00Z", "significant": True,
                      "far": 1e-9, "properties": {"HasNS": 0.4},
                      "classification": {"BNS": 0.8, "Terrestrial": 0.2}},
            "ra": 10, "dec": 20})
        self.assertEqual(lvk["source"], "lvk")
        self.assertTrue(lvk["significance"]["significant"])
        self.assertEqual(lvk["significance"]["has_ns"], 0.4)


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.private = Ed25519PrivateKey.generate()
        raw_private = self.private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())
        raw_public = self.private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.config = {"autonomy": {"private_key": base64.b64encode(raw_private).decode()}}
        self.keys = {"primary": base64.b64encode(raw_public).decode()}

    def _bundle(self, sequence=1):
        now = datetime.now(timezone.utc)
        return cloud_autonomy.sign_bundle({
            "schema_version": 1, "bundle_id": f"b-{sequence}", "sequence": sequence,
            "node_id": "node-a", "plan_id": "p1", "issued_at": now.isoformat(),
            "valid_from": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=2)).isoformat(),
            "minimum_agent_version": "1", "items": [{"item_id": "i1"}],
            "contingencies": {}, "budgets": {"max_items": 5, "max_slews": 5,
            "max_exposure_s": 1000, "max_storage_bytes": 1000},
            "requirements": {}, "safety_policy_version": "1",
            "config_fingerprint": "x", "signing_key_id": "primary",
        }, self.config)

    def test_signature_rollback_and_restart_journal(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "autonomy.db"
            store = AutonomyStore(path)
            accepted = self._bundle(2)
            store.verify_and_store(accepted, "node-a", self.keys)
            store.verify_and_store(accepted, "node-a", self.keys)
            reused = self._bundle(2)
            reused["bundle_id"] = "different"
            reused = cloud_autonomy.sign_bundle(reused, self.config)
            with self.assertRaises(ValueError):
                store.verify_and_store(reused, "node-a", self.keys)
            with self.assertRaises(ValueError):
                store.verify_and_store(self._bundle(1), "node-a", self.keys)
            bad = self._bundle(3)
            bad["items"][0]["item_id"] = "tampered"
            with self.assertRaises(ValueError):
                store.verify_and_store(bad, "node-a", self.keys)
            attempt = store.record("b-2", "i1", "started")
            store.record("b-2", "i1", "completed", attempt_id=attempt)
            restarted = AutonomyStore(path)
            self.assertEqual(restarted.remaining_items(self._bundle(2)), [])

    def test_wrong_node_expiry_budget_and_key_rotation_rejected_or_applied(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        with tempfile.TemporaryDirectory() as td:
            store = AutonomyStore(Path(td) / "a.db")
            with self.assertRaises(ValueError):
                store.verify_and_store(self._bundle(1), "node-b", self.keys)
            expired = self._bundle(1)
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            expired["valid_from"] = (past - timedelta(hours=1)).isoformat()
            expired["expires_at"] = past.isoformat()
            expired = cloud_autonomy.sign_bundle(expired, self.config)
            with self.assertRaises(ValueError):
                store.verify_and_store(expired, "node-a", self.keys)
            excessive = self._bundle(1)
            excessive["budgets"]["max_storage_bytes"] = 99_000_000
            excessive = cloud_autonomy.sign_bundle(excessive, self.config)
            with self.assertRaises(ValueError):
                store.verify_and_store(excessive, "node-a", self.keys,
                                       max_storage_bytes=1_000)

            next_private = Ed25519PrivateKey.generate()
            next_raw = next_private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            rotating = self._bundle(2)
            rotating["next_public_key"] = {
                "key_id": "next", "public_key": base64.b64encode(next_raw).decode()}
            rotating = cloud_autonomy.sign_bundle(rotating, self.config)
            store.verify_and_store(rotating, "node-a", self.keys)
            next_private_raw = next_private.private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                serialization.NoEncryption())
            next_config = {"autonomy": {"private_key": base64.b64encode(next_private_raw).decode()}}
            follow = self._bundle(3)
            follow["signing_key_id"] = "next"
            follow = cloud_autonomy.sign_bundle(follow, next_config)
            store.verify_and_store(follow, "node-a", self.keys)


class DurableOutboxTests(unittest.TestCase):
    def test_more_than_legacy_record_limit_and_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            box = DurableOutbox(Path(td) / "outbox.db", max_bytes=10_000_000)
            for i in range(650):
                self.assertTrue(box.enqueue("measurement", {"i": i},
                                            idempotency_key=f"m-{i}", priority=100))
            self.assertEqual(box.count("measurement"), 650)
            self.assertFalse(box.enqueue("measurement", {"i": 0},
                                         idempotency_key="m-0", priority=100))


if __name__ == "__main__":
    unittest.main()
