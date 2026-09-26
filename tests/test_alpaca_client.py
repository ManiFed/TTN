"""ALPACA client response parsing.

A driver can return valid JSON with ErrorNumber 0 and simply omit "Value" --
tests/fuzz/fakealpaca.py models exactly this as the "missing_value" protocol
fault. Every caller (dashboard.py's camera-drop recovery in particular)
classifies failures by AlpacaError/message text, so this must surface as a
structured AlpacaError rather than a bare KeyError.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from alpaca.client import AlpacaClient, AlpacaError


def _fake_response(body: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = __import__("json").dumps(body).encode()
    resp.headers = {"Content-Length": str(len(resp.content))}
    resp.raise_for_status = MagicMock()
    return resp


class MissingValueTest(unittest.TestCase):
    def setUp(self):
        self.client = AlpacaClient("127.0.0.1", 32323, "telescope", 0)
        self.client.session = MagicMock()

    def test_get_raises_alpaca_error_not_keyerror(self):
        self.client.session.get.return_value = _fake_response(
            {"ErrorNumber": 0, "ErrorMessage": ""})
        with self.assertRaises(AlpacaError):
            self.client._get("connected")

    def test_get_returns_value_when_present(self):
        self.client.session.get.return_value = _fake_response(
            {"ErrorNumber": 0, "ErrorMessage": "", "Value": True})
        self.assertTrue(self.client._get("connected"))

    def test_ping_raises_alpaca_error_not_keyerror(self):
        self.client._ping_session = MagicMock()
        self.client._ping_session.get.return_value = _fake_response(
            {"ErrorNumber": 0, "ErrorMessage": ""})
        with self.assertRaises(AlpacaError):
            self.client.ping()


if __name__ == "__main__":
    unittest.main()
