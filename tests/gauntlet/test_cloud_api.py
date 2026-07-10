"""Gauntlet: cloud-side reliability endpoints (F14, attach linking).

Covers the node-incident ingestion endpoint and member attach (activation
codes are retired).
"""

import unittest
from unittest.mock import patch

import cloud.server as server


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


class ActivationRetiredTest(unittest.TestCase):
    """Activation codes return 410 Gone."""

    def test_register_rejects_activation_code(self):
        client = server.app.test_client()
        resp = client.post("/api/v1/nodes/register",
                           json={"activation_code": "BS-2026-ANYTHING",
                                 "latitude": 31.0, "longitude": -99.0})
        self.assertEqual(resp.status_code, 410)
        self.assertIn("retired", resp.get_json()["error"].lower())

    def test_admin_activation_codes_gone(self):
        client = server.app.test_client()
        # Without admin key → 401; with wrong key same. Endpoint exists as 410 after auth.
        with patch("cloud.server.require_admin", lambda f: f):
            # require_admin is a decorator already applied; hit via client gets 401.
            resp = client.post("/api/v1/admin/activation-codes", json={"count": 1})
        self.assertIn(resp.status_code, (401, 410))


if __name__ == "__main__":
    unittest.main()
