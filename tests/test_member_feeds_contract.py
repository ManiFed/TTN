#!/usr/bin/env python3
"""
Contract tests for the two feeds the desktop app renders as whole screens:

    GET /api/v1/network/live-fleet   → LiveFleetScreen  (LiveFleetFeed model)
    GET /api/v1/me/discoveries       → DiscoveriesScreen (Discovery model)

Both screens parse JSON key-by-key with silent fallbacks — a renamed or dropped
column doesn't raise, it renders an empty list or a zeroed field, which looks
exactly like "nothing happened tonight". These tests assert the keys the Dart
models actually read (app/lib/models/models.dart) against real rows written to
a throwaway PostgreSQL, so a schema change that would blank the screens fails
here instead of in front of a member.

Skipped when no local postgres is reachable.

Run with:  python3 -m unittest tests.test_member_feeds_contract
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

TEST_DB_NAME = "boundless_feeds_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"

# Exactly what app/lib/models/models.dart reads out of each record.
FLEET_NODE_KEYS = {"node_id", "phase", "target_name", "sky_clear", "is_dark",
                   "online", "updated_at"}
REFLOW_KEYS = {"from_node", "to_node", "target_name", "expected_info",
               "outcome", "created_at"}
REFLEX_KEYS = {"name", "node_ids", "created_at"}
DISCOVERY_KEYS = {"id", "source_key", "ra_deg", "dec_deg", "kind", "filter",
                  "state", "vsx_name", "tns_name", "updated_at",
                  "retrospective"}
LIVE_DISCOVERY_KEYS = {"n_detections", "n_nodes", "peak_delta_mag", "last_mag",
                       "your_nodes"}
RETRO_DISCOVERY_KEYS = {"delta_mag", "mag"}


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class MemberFeedContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        from cloud import db
        admin = psycopg2.connect(ADMIN_URL)
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        admin.close()
        db.init(TEST_URL)
        cls.db = db

        import cloud.server as server
        cls.server = server
        cls.client = server.app.test_client()

    @classmethod
    def tearDownClass(cls):
        import psycopg2
        if cls.db._pool is not None:
            cls.db._pool.closeall()
            cls.db._pool = None
        admin = psycopg2.connect(ADMIN_URL)
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        admin.close()

    def setUp(self):
        for table in ("node_live_state", "reflow_log", "interrupts",
                      "discovery_candidates", "retro_discoveries",
                      "node_members", "nodes", "users"):
            self.db.execute(f"DELETE FROM {table}")

    # ── live fleet ─────────────────────────────────────────────────────────────

    def _seed_fleet(self):
        from cloud import live
        live.record_state("node_a", {"phase": "observing", "target_name": "Z Cam",
                                     "sky_clear": True, "is_dark": True})
        night = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.db.execute(
            "INSERT INTO reflow_log (night, from_node, to_node, target_id,"
            " target_name, expected_info, outcome, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (night, "node_a", "node_b", "t_zcam", "Z Cam", 0.42, "delivered",
             _now()),
        )
        self.db.execute(
            "INSERT INTO interrupts (name, ra_deg, dec_deg, reason, node_ids,"
            " created_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("reflex:CAT-001", 126.3, 73.1, "reflex_confirm",
             json.dumps(["node_b"]), _now(),
             (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()),
        )

    def test_live_fleet_feed_carries_every_key_the_screen_reads(self):
        self._seed_fleet()
        resp = self.client.get("/api/v1/network/live-fleet")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()

        self.assertTrue(body["fleet"], "fleet must not be empty after a heartbeat")
        self.assertLessEqual(FLEET_NODE_KEYS, set(body["fleet"][0]))
        self.assertLessEqual(REFLOW_KEYS, set(body["reflows"][0]))
        self.assertLessEqual(REFLEX_KEYS, set(body["reflex_confirmations"][0]))

    def test_reflex_node_ids_arrive_as_a_list_not_json_text(self):
        """The screen maps over node_ids; a JSON string would render as
        characters."""
        self._seed_fleet()
        body = self.client.get("/api/v1/network/live-fleet").get_json()
        self.assertEqual(body["reflex_confirmations"][0]["node_ids"], ["node_b"])

    def test_empty_night_is_a_well_formed_empty_feed(self):
        body = self.client.get("/api/v1/network/live-fleet").get_json()
        for key in ("fleet", "reflows", "reflex_confirmations"):
            self.assertEqual(body[key], [], key)

    # ── discoveries ────────────────────────────────────────────────────────────

    def _member(self, user_id="u_test1", node_id="node_a"):
        self.db.execute(
            "INSERT INTO users (user_id, email, password_hash, salt, created_at)"
            " VALUES (%s,%s,%s,%s,%s)",
            (user_id, f"{user_id}@example.test", "x", "y", _now()),
        )
        self.db.execute(
            "INSERT INTO nodes (node_id, api_key, latitude, longitude,"
            " registered_at, last_heartbeat) VALUES (%s,%s,%s,%s,%s,%s)",
            (node_id, "key", 40.0, -74.0, _now(), _now()),
        )
        self.db.execute(
            "INSERT INTO node_members (node_id, user_id, claimed_at)"
            " VALUES (%s,%s,%s)", (node_id, user_id, _now()))
        return {"user_id": user_id}

    def _discoveries_for(self, user):
        """Call the view behind require_member, which needs a bearer token we
        don't have here."""
        view = self.server.api_me_discoveries
        with self.server.app.test_request_context("/api/v1/me/discoveries"):
            return getattr(view, "__wrapped__", view)(user).get_json()

    def test_live_candidate_carries_every_key_the_screen_reads(self):
        user = self._member()
        self.db.execute(
            "INSERT INTO discovery_candidates (source_key, ra_deg, dec_deg,"
            " kind, filter, first_bjd, last_bjd, n_detections, n_nodes,"
            " node_ids, peak_delta_mag, last_mag, state, updated_at, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("CAT-001", 126.3, 73.1, "transient", "CV", 2461251.5, 2461251.7,
             3, 2, json.dumps(["node_a", "node_z"]), 1.4, 13.2, "pending",
             _now(), _now()),
        )
        body = self._discoveries_for(user)
        self.assertEqual(body["count"], 1)
        rec = body["discoveries"][0]
        self.assertLessEqual(DISCOVERY_KEYS | LIVE_DISCOVERY_KEYS, set(rec))
        self.assertIs(rec["retrospective"], False)
        # "your telescope found it" — only this member's nodes, not the fleet's.
        self.assertEqual(rec["your_nodes"], ["node_a"])

    def test_retrospective_candidate_carries_its_own_shape(self):
        user = self._member()
        self.db.execute(
            "INSERT INTO retro_discoveries (source_key, ra_deg, dec_deg, kind,"
            " filter, bjd, mag, delta_mag, state, user_id, updated_at,"
            " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("CAT-002", 10.0, 20.0, "nova", "CV", 2459000.5, 15.1, 2.2,
             "pending", user["user_id"], _now(), _now()),
        )
        rec = self._discoveries_for(user)["discoveries"][0]
        self.assertLessEqual(DISCOVERY_KEYS | RETRO_DISCOVERY_KEYS, set(rec))
        self.assertIs(rec["retrospective"], True)

    def test_a_member_never_sees_another_members_candidates(self):
        mine = self._member("u_mine", "node_mine")
        self._member("u_theirs", "node_theirs")
        self.db.execute(
            "INSERT INTO discovery_candidates (source_key, ra_deg, dec_deg,"
            " kind, filter, n_detections, n_nodes, node_ids, state, updated_at,"
            " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("CAT-003", 1.0, 2.0, "transient", "CV", 2, 1,
             json.dumps(["node_theirs"]), "pending", _now(), _now()),
        )
        self.assertEqual(self._discoveries_for(mine)["count"], 0)

    def test_member_with_no_nodes_gets_an_empty_feed(self):
        body = self._discoveries_for({"user_id": "u_nobody"})
        self.assertEqual((body["count"], body["discoveries"]), (0, []))


if __name__ == "__main__":
    unittest.main()
