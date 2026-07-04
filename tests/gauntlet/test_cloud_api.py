"""Gauntlet: cloud-side reliability endpoints (F7, F14).

Covers the node-incident ingestion endpoint and the idempotent
activation-code retry window that prevents a lost registration response from
permanently bricking a member's activation.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cloud.server as server


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


_NODE = {"node_id": "node_a1", "api_key": "key_a1"}


class NodeIncidentEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def _post(self, body):
        with patch("cloud.server.registry.authenticate", return_value=dict(_NODE)):
            with patch("cloud.server.incidents.log") as log:
                resp = self.client.post(
                    "/api/v1/incidents", json=body,
                    headers={"X-Node-Id": "node_a1", "X-Api-Key": "key_a1"})
                return resp, log

    def test_incident_recorded_with_node_attribution(self):
        resp, log = self._post({
            "incident_type": "slew_failed", "severity": "error",
            "target_name": "T CrB", "detail": {"timeout_s": 180}})
        self.assertEqual(resp.status_code, 200)
        log.assert_called_once()
        args, kwargs = log.call_args
        self.assertEqual(args, ("node_a1", "slew_failed"))
        self.assertEqual(kwargs["severity"], "error")
        self.assertEqual(kwargs["target_name"], "T CrB")
        self.assertEqual(kwargs["detail"], {"timeout_s": 180})

    def test_missing_type_rejected(self):
        resp, log = self._post({"severity": "error"})
        self.assertEqual(resp.status_code, 400)
        log.assert_not_called()

    def test_bogus_severity_coerced_to_info(self):
        resp, log = self._post({"incident_type": "x", "severity": "apocalyptic"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(log.call_args.kwargs["severity"], "info")

    def test_oversized_detail_truncated(self):
        resp, log = self._post({
            "incident_type": "x", "detail": {"blob": "y" * 10000}})
        self.assertEqual(resp.status_code, 200)
        stored = log.call_args.kwargs["detail"]
        self.assertIn("truncated", stored)
        self.assertLessEqual(len(stored["truncated"]), 4000)

    def test_unauthenticated_node_rejected(self):
        with patch("cloud.server.registry.authenticate", return_value=None):
            resp = self.client.post("/api/v1/incidents",
                                    json={"incident_type": "x"})
        self.assertEqual(resp.status_code, 401)


class ActivationRetryWindowTest(unittest.TestCase):
    """F7 — a lost registration response must not brick the activation code."""

    def test_recently_used_code_returns_existing_credentials(self):
        code_row = {"used_at": _iso(60), "node_id": "node_a1"}
        with patch("cloud.server.db.query_one", return_value=dict(_NODE)):
            creds = server._activation_retry_credentials(code_row)
        self.assertEqual(creds, _NODE)

    def test_stale_used_code_is_not_recoverable(self):
        code_row = {"used_at": _iso(3600), "node_id": "node_a1"}
        with patch("cloud.server.db.query_one", return_value=dict(_NODE)):
            self.assertIsNone(server._activation_retry_credentials(code_row))

    def test_code_without_linked_node_is_not_recoverable(self):
        self.assertIsNone(server._activation_retry_credentials(
            {"used_at": _iso(60), "node_id": ""}))

    def test_vanished_node_is_not_recoverable(self):
        code_row = {"used_at": _iso(60), "node_id": "node_gone"}
        with patch("cloud.server.db.query_one", return_value=None):
            self.assertIsNone(server._activation_retry_credentials(code_row))

    def test_garbage_timestamp_is_not_recoverable(self):
        self.assertIsNone(server._activation_retry_credentials(
            {"used_at": "not-a-date", "node_id": "node_a1"}))

    def test_register_endpoint_returns_creds_inside_retry_window(self):
        client = server.app.test_client()
        code_row = {"used_at": _iso(60), "node_id": "node_a1",
                    "latitude": 31.0, "longitude": -99.0}

        def fake_query_one(sql, params=None):
            if "activation_codes" in sql:
                return dict(code_row)
            if "FROM nodes" in sql:
                return dict(_NODE)
            return None

        with patch("cloud.server.db.query_one", side_effect=fake_query_one):
            resp = client.post("/api/v1/nodes/register",
                               json={"activation_code": "BS-2026-LOSTRESP"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), _NODE)

    def test_register_endpoint_still_rejects_old_used_codes(self):
        client = server.app.test_client()
        code_row = {"used_at": _iso(7200), "node_id": "node_a1"}

        def fake_query_one(sql, params=None):
            if "activation_codes" in sql:
                return dict(code_row)
            return dict(_NODE)

        with patch("cloud.server.db.query_one", side_effect=fake_query_one):
            resp = client.post("/api/v1/nodes/register",
                               json={"activation_code": "BS-2026-OLDCODE1"})
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
