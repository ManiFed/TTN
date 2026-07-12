#!/usr/bin/env python3
"""
Live fleet cloud-side tests (Phase 0) against a throwaway local PostgreSQL:

  * live.record_state upserts one row/node and fleet_state derives 'offline'
    from heartbeat-age staleness,
  * live.publish appends a dispatch_events row AND fires a Postgres NOTIFY that
    a separately-connected LISTEN client receives (the mechanism the realtime
    SSE gateway relies on),
  * events_since replays exactly what a reconnecting client missed.

Skipped when no local postgres is reachable.

Run with:  python3 -m unittest tests.test_live_fleet_cloud
"""

import json
import select
import time
import unittest
from datetime import datetime, timedelta, timezone

TEST_DB_NAME = "boundless_org_test"
ADMIN_URL = "postgresql://boundless@/boundless?host=/tmp"
TEST_URL = f"postgresql://boundless@/{TEST_DB_NAME}?host=/tmp"


def _postgres_available() -> bool:
    try:
        import psycopg2
        psycopg2.connect(ADMIN_URL).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_postgres_available(), "local postgres not reachable")
class LiveFleetCloudTest(unittest.TestCase):
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
        for table in ("node_live_state", "dispatch_events"):
            self.db.execute(f"DELETE FROM {table}")

    # ── Live fleet state ────────────────────────────────────────────────────────

    def test_record_state_upserts_and_derives_online(self):
        from cloud import live
        live.record_state("node_a", {"phase": "exposing", "is_dark": True,
                                     "target_name": "V1"}, heartbeat_s=5)
        live.record_state("node_a", {"phase": "slewing", "is_dark": True},
                          heartbeat_s=5)  # second report updates same row
        rows = self.db.query("SELECT * FROM node_live_state")
        self.assertEqual(len(rows), 1)
        fleet = {n["node_id"]: n for n in live.fleet_state()}
        self.assertEqual(fleet["node_a"]["phase"], "slewing")
        self.assertTrue(fleet["node_a"]["online"])
        self.assertTrue(fleet["node_a"]["is_dark"])

    def test_stale_node_reads_as_offline(self):
        from cloud import live
        live.record_state("node_old", {"phase": "exposing"}, heartbeat_s=5)
        # Backdate the heartbeat well past 3× the 5 s cadence.
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.db.execute(
            "UPDATE node_live_state SET updated_at = %s WHERE node_id = %s",
            (old, "node_old"))
        n = live.fleet_state()[0]
        self.assertFalse(n["online"])
        self.assertEqual(n["phase"], "offline")

    def test_garbage_phase_collapses_to_idle(self):
        from cloud import live
        live.record_state("node_x", {"phase": "'; DROP TABLE nodes; --"})
        self.assertEqual(live.fleet_state()[0]["phase"], "idle")

    def test_dark_online_nodes_pool(self):
        from cloud import live
        live.record_state("dark1", {"phase": "idle", "is_dark": True})
        live.record_state("cloudy", {"phase": "clouded", "is_dark": True})
        live.record_state("day1", {"phase": "daylight", "is_dark": False})
        pool = live.dark_online_nodes()
        self.assertIn("dark1", pool)
        self.assertNotIn("cloudy", pool)   # clouded excluded
        self.assertNotIn("day1", pool)     # not dark

    # ── Dispatch bus: NOTIFY round-trip ─────────────────────────────────────────

    def test_publish_notifies_a_listener(self):
        """A LISTEN client receives the NOTIFY publish() fires — the exact path
        the realtime SSE gateway uses to wake a node."""
        import psycopg2
        import psycopg2.extensions
        from cloud import live

        listener = psycopg2.connect(TEST_URL)
        listener.set_isolation_level(
            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        listener.cursor().execute(f"LISTEN {live.NOTIFY_CHANNEL}")
        try:
            eid = live.publish("node_a", "interrupt", {"src": "reflex"})
            # Wait up to 2 s for the async notification.
            deadline = time.time() + 2.0
            got = None
            while time.time() < deadline and got is None:
                if select.select([listener], [], [], 0.2)[0]:
                    listener.poll()
                    while listener.notifies:
                        got = json.loads(listener.notifies.pop(0).payload)
            self.assertIsNotNone(got, "no NOTIFY received")
            self.assertEqual(got["node_id"], "node_a")
            self.assertEqual(got["kind"], "interrupt")
            self.assertEqual(got["id"], eid)
        finally:
            listener.close()

        # The event is also durably logged for Last-Event-ID replay.
        row = self.db.query_one(
            "SELECT node_id, kind FROM dispatch_events WHERE id = %s", (eid,))
        self.assertEqual(row["kind"], "interrupt")

    def test_events_since_replays_missed_only(self):
        from cloud import live
        e1 = live.publish("node_a", "plan")
        e2 = live.publish("node_a", "interrupt")
        live.publish("node_b", "plan")            # other node — must not leak
        missed = live.events_since("node_a", e1)
        self.assertEqual([e["id"] for e in missed], [e2])
        self.assertEqual(missed[0]["kind"], "interrupt")

    def test_prune_dispatch_events(self):
        from cloud import live
        eid = live.publish("node_a", "plan")
        old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        self.db.execute(
            "UPDATE dispatch_events SET created_at = %s WHERE id = %s", (old, eid))
        deleted = live.prune_dispatch_events()
        self.assertGreaterEqual(deleted, 1)
        self.assertIsNone(self.db.query_one(
            "SELECT 1 FROM dispatch_events WHERE id = %s", (eid,)))


if __name__ == "__main__":
    unittest.main()
