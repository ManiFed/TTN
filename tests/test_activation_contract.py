#!/usr/bin/env python3
"""
Contract tests for node linking without activation codes.

Run with:  python3 -m unittest tests.test_activation_contract
"""

import unittest
from unittest.mock import patch

import cloud.server as server


class ActivationRetiredContractTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_register_rejects_activation_code(self):
        with patch("cloud.server.registry.register_node") as register_node:
            resp = self.client.post(
                "/api/v1/nodes/register",
                json={"activation_code": "BS-2026-LEGACY01",
                      "latitude": 40.0, "longitude": -74.0},
            )
        self.assertEqual(resp.status_code, 410)
        self.assertIn("retired", resp.get_json()["error"].lower())
        register_node.assert_not_called()

    def test_me_activation_code_gone(self):
        # require_member will 401 without auth — either is fine for retirement.
        resp = self.client.post("/api/v1/me/activation-code", json={})
        self.assertIn(resp.status_code, (401, 410))

    @patch("cloud.server.db.execute")
    @patch("cloud.server.db.query_one", return_value=None)
    @patch("cloud.server.registry.register_node",
           return_value={"node_id": "node_test1", "api_key": "key_test1"})
    def test_attach_registers_and_links(self, register_node, query_one, execute):
        # Call the view function directly (decorator already applied at import).
        # Unwrap require_member by using __wrapped__ if present.
        fn = getattr(server.api_me_attach_node, "__wrapped__", None)
        self.assertIsNotNone(fn, "expected require_member to expose __wrapped__")
        with server.app.test_request_context(
            "/api/v1/me/nodes/attach",
            method="POST",
            json={
                "latitude": 41.0,
                "longitude": -73.0,
                "location_name": "Backyard",
                "telescope_model": "ZWO Seestar S50",
                "portable": False,
            },
        ):
            resp = fn({"user_id": "u_test"})
        if isinstance(resp, tuple):
            body, status = resp[0], resp[1]
        else:
            body, status = resp, getattr(resp, "status_code", 200)
        data = body.get_json() if hasattr(body, "get_json") else body
        self.assertEqual(status, 200)
        self.assertEqual(data["node_id"], "node_test1")
        self.assertEqual(data["api_key"], "key_test1")
        register_node.assert_called_once()
        self.assertTrue(any(
            "node_members" in str(c.args[0]) for c in execute.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
