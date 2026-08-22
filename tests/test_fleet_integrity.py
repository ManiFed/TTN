#!/usr/bin/env python3
"""Each fleet-integrity check must actually detect the bug it is named for.

A health check that cannot fail is worse than no health check: it reports
"healthy" forever and nobody looks again. So every check here is shown a
database that contains exactly the fault it hunts, and asserted to find it --
and then shown a clean database and asserted to stay quiet.

The faults are drawn from bugs that reached production:
    orphaned node        319fded, 0c4bb87
    stale vacation       9421bbd, 2cbc6a1
    missing credentials  5d926a1, 9280ba7
    dead heartbeat       0c4bb87

Run with:  python3 -m pytest tests/test_fleet_integrity.py
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud import integrity


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class _FakeDb:
    """Dispatches db.query on a distinctive fragment of each check's SQL."""

    def __init__(self, **rows):
        self.rows = rows

    def query(self, sql: str, params: tuple = ()):
        if "nm.node_id IS NULL" in sql:
            return self.rows.get("orphans", [])
        if "n.node_id IS NULL" in sql:
            return self.rows.get("dangling", [])
        if "api_key IS NULL" in sql:
            return self.rows.get("no_key", [])
        if "status = 'vacation'" in sql:
            return self.rows.get("vacation", [])
        if "HAVING COUNT(*) > 1" in sql:
            return self.rows.get("duplicates", [])
        if "last_heartbeat IS NULL" in sql:
            return self.rows.get("ghosts", [])
        if "SELECT * FROM nodes" in sql:
            return self.rows.get("all_nodes", [])
        raise AssertionError(f"unexpected query: {sql[:80]}")


def _run(**rows) -> dict:
    with patch.object(integrity, "db", _FakeDb(**rows)):
        return integrity.run_all()


def _checks_hit(result: dict) -> set:
    return {f["check"] for f in result["findings"]}


class FleetIntegrityTest(unittest.TestCase):

    def test_clean_fleet_reports_healthy(self):
        result = _run()
        self.assertTrue(result["healthy"], result)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["errors"], [])

    def test_orphaned_node_with_history_is_critical(self):
        """A node with observations but no owner is invisible to its member."""
        result = _run(orphans=[{
            "node_id": "node_orphan", "telescope_model": "Seestar S50",
            "registered_at": _iso(days=30), "last_heartbeat": _iso(minutes=2),
            "total_observations": 412,
        }])
        self.assertIn("orphaned_node", _checks_hit(result))
        finding = next(f for f in result["findings"] if f["check"] == "orphaned_node")
        self.assertEqual(finding["severity"], "critical")
        self.assertIn("412", finding["detail"])

    def test_orphaned_node_without_history_is_only_a_warning(self):
        result = _run(orphans=[{
            "node_id": "node_new", "registered_at": _iso(days=1),
            "last_heartbeat": None, "total_observations": 0,
        }])
        finding = next(f for f in result["findings"] if f["check"] == "orphaned_node")
        self.assertEqual(finding["severity"], "warning")

    def test_stale_vacation_is_detected(self):
        """The bug fixed four times: status lags the calendar."""
        result = _run(vacation=[{
            "node_id": "node_vac", "status": "vacation",
            "vacation_from": "2026-01-01", "vacation_until": "2026-01-14",
        }])
        self.assertIn("stale_vacation", _checks_hit(result))

    def test_current_vacation_is_not_flagged(self):
        """A member actually on holiday must never be reported as a fault."""
        today = datetime.now(timezone.utc).date()
        result = _run(vacation=[{
            "node_id": "node_vac", "status": "vacation",
            "vacation_from": str(today - timedelta(days=1)),
            "vacation_until": str(today + timedelta(days=5)),
        }])
        self.assertNotIn("stale_vacation", _checks_hit(result))

    def test_missing_credentials_is_detected(self):
        result = _run(no_key=[{"node_id": "node_nokey", "registered_at": _iso(days=3)}])
        finding = next(f for f in result["findings"]
                       if f["check"] == "missing_credentials")
        self.assertEqual(finding["severity"], "critical")

    def test_dead_heartbeat_thread_is_detected(self):
        """An active node that stopped checking in — the silently-dead thread."""
        result = _run(all_nodes=[{
            "node_id": "node_quiet", "status": "active",
            "last_heartbeat": _iso(hours=30),
        }])
        self.assertIn("heartbeat_gap", _checks_hit(result))

    def test_recent_heartbeat_is_not_flagged(self):
        result = _run(all_nodes=[{
            "node_id": "node_ok", "status": "active",
            "last_heartbeat": _iso(minutes=3),
        }])
        self.assertEqual(result["findings"], [])

    def test_sleeping_and_vacation_nodes_are_never_heartbeat_gaps(self):
        """A vacationing node is quiet by design.

        Flagging one would recreate the false 'your telescope missed last
        night' alert that vacationing members must never receive.
        """
        today = datetime.now(timezone.utc).date()
        result = _run(all_nodes=[
            {"node_id": "n_sleep", "status": "sleeping",
             "last_heartbeat": _iso(days=9)},
            {"node_id": "n_vac", "status": "vacation",
             "vacation_from": str(today - timedelta(days=1)),
             "vacation_until": str(today + timedelta(days=9)),
             "last_heartbeat": _iso(days=9)},
            {"node_id": "n_dis", "status": "disabled",
             "last_heartbeat": _iso(days=9)},
        ])
        self.assertNotIn("heartbeat_gap", _checks_hit(result))

    def test_ghost_registration_is_detected(self):
        result = _run(ghosts=[{
            "node_id": "node_ghost", "telescope_model": "Seestar S50",
            "registered_at": _iso(days=4),
        }])
        self.assertIn("ghost_registration", _checks_hit(result))

    def test_fresh_registration_is_not_yet_a_ghost(self):
        """Someone mid-way through linking a telescope is not a fault."""
        result = _run(ghosts=[{
            "node_id": "node_new", "registered_at": _iso(minutes=10),
        }])
        self.assertEqual(result["findings"], [])

    def test_dangling_membership_is_detected(self):
        result = _run(dangling=[{"node_id": "node_gone", "user_id": "user_1"}])
        self.assertIn("dangling_membership", _checks_hit(result))

    # ── reporting behaviour ───────────────────────────────────────────────

    def test_findings_are_ordered_critical_first(self):
        result = _run(
            duplicates=[{"node_id": "n_dup", "n": 2}],
            no_key=[{"node_id": "n_key", "registered_at": _iso(days=1)}],
            all_nodes=[{"node_id": "n_quiet", "status": "active",
                        "last_heartbeat": _iso(hours=30)}],
        )
        severities = [f["severity"] for f in result["findings"]]
        self.assertEqual(severities, sorted(
            severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]))
        self.assertEqual(severities[0], "critical")

    def test_one_broken_check_does_not_hide_the_others(self):
        """A check that raises is reported as an error, not allowed to abort the run."""
        def explode():
            raise RuntimeError("column does not exist")

        with patch.object(integrity, "CHECKS",
                          (("orphaned_nodes", explode),
                           ("missing_credentials", integrity.missing_credentials))):
            with patch.object(integrity, "db",
                              _FakeDb(no_key=[{"node_id": "n1",
                                               "registered_at": _iso(days=1)}])):
                result = integrity.run_all()

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("column does not exist", result["errors"][0]["error"])
        self.assertIn("missing_credentials", _checks_hit(result))
        self.assertFalse(result["healthy"])

    def test_unparseable_timestamps_do_not_crash(self):
        """Bad data must produce a quiet skip, not a 500 on the admin endpoint."""
        result = _run(
            ghosts=[{"node_id": "n1", "registered_at": "not-a-date"}],
            all_nodes=[{"node_id": "n2", "status": "active",
                        "last_heartbeat": "also-not-a-date"}],
            vacation=[{"node_id": "n3", "status": "vacation",
                       "vacation_from": "??", "vacation_until": "??"}],
        )
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()


class FleetIntegrityEndpointTest(unittest.TestCase):
    """GET /api/v1/admin/fleet-integrity: auth, shape, and a real finding."""

    ADMIN_KEY = "test-admin-key"

    def setUp(self):
        import cloud.server as server
        self.server = server
        self.client = server.app.test_client()
        self._saved = server._config.get("server", {}).get("admin_key", "")
        server._config.setdefault("server", {})["admin_key"] = self.ADMIN_KEY

    def tearDown(self):
        self.server._config["server"]["admin_key"] = self._saved

    def _get(self, key=None):
        headers = {"X-Admin-Key": key if key is not None else self.ADMIN_KEY}
        return self.client.get("/api/v1/admin/fleet-integrity", headers=headers)

    def test_rejects_a_wrong_admin_key(self):
        self.assertEqual(self._get("nope").status_code, 401)

    def test_reports_healthy_on_a_clean_fleet(self):
        with patch.object(integrity, "db", _FakeDb()):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["healthy"])
        self.assertEqual(body["total_findings"], 0)
        self.assertEqual(sorted(body["checks_run"]),
                         sorted(name for name, _ in integrity.CHECKS))

    def test_flags_a_node_left_in_a_stale_vacation_state(self):
        """The end-to-end check the plan calls for: plant one, see it reported."""
        with patch.object(integrity, "db", _FakeDb(vacation=[{
            "node_id": "node_stale", "status": "vacation",
            "vacation_from": "2026-01-01", "vacation_until": "2026-01-14",
        }])):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["healthy"])
        self.assertEqual(body["total_findings"], 1)
        finding = body["findings"][0]
        self.assertEqual(finding["check"], "stale_vacation")
        self.assertEqual(finding["node_id"], "node_stale")
        self.assertEqual(finding["effective_status"], "active")

    def test_a_broken_check_returns_200_with_an_error_not_a_500(self):
        """The admin endpoint must degrade rather than crash — 0c4bb87's lesson."""
        class Exploding:
            def query(self, sql, params=()):
                raise RuntimeError("relation \"nodes\" does not exist")

        with patch.object(integrity, "db", Exploding()):
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["healthy"])
        self.assertEqual(len(body["errors"]), len(integrity.CHECKS))


class ReadOnlyAdminKeyTest(unittest.TestCase):
    """The nightly patrol runs unattended in CI, so its credential must be
    unable to do anything but look.

    Handing the full admin key to GitHub Actions would give every workflow in
    the repository the ability to replan the network, roll back tuning weights
    or mark AAVSO batches submitted. These tests pin the smaller credential --
    and, more importantly, pin that an *unset* key never authenticates, which
    is exactly the state a mis-piped CI secret leaves behind.
    """

    ADMIN = "full-admin-key"
    READONLY = "readonly-admin-key"

    def setUp(self):
        import cloud.server as server
        self.server = server
        self.client = server.app.test_client()
        self._saved = dict(server._config.get("server", {}))
        server._config.setdefault("server", {}).update(
            {"admin_key": self.ADMIN, "admin_readonly_key": self.READONLY})

    def tearDown(self):
        self.server._config["server"] = self._saved

    def _integrity(self, key):
        headers = {"X-Admin-Key": key} if key is not None else {}
        with patch.object(integrity, "db", _FakeDb()):
            return self.client.get("/api/v1/admin/fleet-integrity", headers=headers)

    def test_the_readonly_key_can_read_integrity(self):
        self.assertEqual(self._integrity(self.READONLY).status_code, 200)

    def test_the_full_admin_key_still_works(self):
        self.assertEqual(self._integrity(self.ADMIN).status_code, 200)

    def test_a_wrong_key_is_rejected(self):
        self.assertEqual(self._integrity("nope").status_code, 401)

    def test_a_missing_header_is_rejected(self):
        self.assertEqual(self._integrity(None).status_code, 401)

    def test_an_empty_header_is_rejected(self):
        self.assertEqual(self._integrity("").status_code, 401)

    def test_an_unset_readonly_key_does_not_authenticate_an_empty_header(self):
        """The mis-piped-CI-secret case: blank config must not accept blank."""
        self.server._config["server"]["admin_readonly_key"] = ""
        self.assertEqual(self._integrity("").status_code, 401)
        self.assertEqual(self._integrity(None).status_code, 401)

    def test_the_readonly_key_cannot_reach_a_mutating_admin_endpoint(self):
        """The whole point: it can look at the fleet and nothing else."""
        for method, path in (
            ("post", "/api/v1/admin/replan"),
            ("post", "/api/v1/admin/ingest"),
            ("post", "/api/v1/admin/tuning/rollback"),
            ("get", "/api/v1/admin/aavso-batches"),
            ("get", "/api/v1/admin/incidents"),
        ):
            resp = getattr(self.client, method)(
                path, headers={"X-Admin-Key": self.READONLY})
            self.assertEqual(
                resp.status_code, 401,
                f"{method.upper()} {path} accepted the read-only key; it must "
                f"stay behind the full admin key")
