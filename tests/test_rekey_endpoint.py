#!/usr/bin/env python3
"""
POST /api/v1/nodes/rekey must return a clean 401 on an invalid/rotated
recovery_token, not crash.

Regression test: the endpoint's miss-handling call had drifted out of sync
with _pair_claim_record_miss's signature (which gained a required `token`
argument when pairing-claim rate limiting was changed to count distinct
tokens), so every rejected rekey attempt raised an unhandled TypeError and
returned a 500 instead of 401 -- observed live in production, where it left
a node's cloud_communicator treating a real rejection as an unexpected error.

Run with:  python3 -m unittest tests.test_rekey_endpoint
"""

import unittest
from unittest.mock import patch

import cloud.server as server


class RekeyEndpointTest(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    @patch("cloud.server.registry.rekey_node", return_value=None)
    def test_invalid_token_returns_401_not_500(self, rekey_node):
        resp = self.client.post(
            "/api/v1/nodes/rekey",
            json={"node_id": "node_abc123", "recovery_token": "wrong-or-reused"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("error", resp.get_json())

    @patch("cloud.server.registry.rekey_node",
           return_value={"api_key": "new_key", "recovery_token": "new_recovery"})
    def test_valid_token_returns_fresh_credentials(self, rekey_node):
        resp = self.client.post(
            "/api/v1/nodes/rekey",
            json={"node_id": "node_abc123", "recovery_token": "correct-token"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["api_key"], "new_key")


if __name__ == "__main__":
    unittest.main()
