#!/usr/bin/env python3
"""MCP member sessions must survive a process restart, without leaking
the bearer token into a tool result.

The hour-long silent drop in issue #36 is the MCP process recycling and
losing a RAM-only token. The cloud session itself lasts 90 days.

Run with:  python3 -m pytest tests/test_mcp_session.py
"""

from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from telescope_mcp import session_store
from telescope_mcp.client import ApiError, CloudClient
from telescope_mcp.cloud_server import build_server as build_cloud


def call(server, name: str, args: dict | None = None) -> tuple[bool, str]:
    try:
        result = asyncio.run(server.call_tool(name, args or {}))
    except ToolError as exc:
        return False, str(exc)
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    failed = getattr(result, "is_error", None)
    if failed is None:
        failed = getattr(result, "isError", False)
    return not failed, text


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = json.dumps(payload)
    return resp


def _err_response(status: int, message: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps({"error": message})
    return resp


class SessionStoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mcp_session"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        session_store.save("tok_abc", self.path, cloud_base="https://example.invalid")
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "tok_abc",
        )

    def test_file_is_user_only(self):
        session_store.save("tok_abc", self.path, cloud_base="https://example.invalid")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_clear_removes_the_file(self):
        session_store.save("tok_abc", self.path, cloud_base="https://example.invalid")
        session_store.clear(self.path)
        self.assertIsNone(session_store.load(self.path))
        self.assertFalse(self.path.exists())

    def test_missing_file_is_none(self):
        self.assertIsNone(session_store.load(self.path))

    def test_mismatched_cloud_base_is_ignored(self):
        session_store.save(
            "tok_prod", self.path,
            cloud_base="https://api.thetelescope.net",
        )
        self.assertIsNone(session_store.load(
            self.path, expected_base="https://staging.example",
        ))
        self.assertEqual(
            session_store.load(
                self.path, expected_base="https://api.thetelescope.net",
            ),
            "tok_prod",
        )

    def test_plaintext_legacy_file_is_ignored(self):
        self.path.write_text("tok_legacy", encoding="utf-8")
        self.assertIsNone(session_store.load(
            self.path, expected_base="https://api.thetelescope.net",
        ))


class CloudClientPersistTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mcp_session"
        self.env_patch = patch.dict("os.environ", {}, clear=False)
        self.env = self.env_patch.start()
        self.env.pop("TELESCOPE_MCP_TOKEN", None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def _client(self) -> CloudClient:
        return CloudClient(
            base="https://example.invalid",
            session_path=self.path,
        )

    def test_set_token_survives_a_new_client(self):
        first = self._client()
        first.set_token("tok_abc")
        restarted = self._client()
        self.assertTrue(restarted.authenticated)
        with patch.object(restarted._session, "request",
                          return_value=_ok_response({"ok": True})) as req:
            restarted.get("/me")
        self.assertEqual(
            req.call_args.kwargs["headers"]["Authorization"],
            "Bearer tok_abc",
        )

    def test_env_token_wins_and_is_not_copied_to_disk(self):
        session_store.save(
            "tok_disk", self.path, cloud_base="https://example.invalid",
        )
        with patch.dict("os.environ", {"TELESCOPE_MCP_TOKEN": "tok_env"}):
            client = self._client()
            self.assertTrue(client.authenticated)
            with patch.object(client._session, "request",
                              return_value=_ok_response({"ok": True})) as req:
                client.get("/me")
        self.assertEqual(
            req.call_args.kwargs["headers"]["Authorization"],
            "Bearer tok_env",
        )
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "tok_disk",
        )

    def test_logout_deletes_the_file(self):
        client = self._client()
        client.set_token("tok_abc")
        self.assertTrue(self.path.exists())
        client.set_token(None)
        self.assertFalse(self.path.exists())
        self.assertFalse(client.authenticated)

    def test_401_clears_the_saved_session(self):
        client = self._client()
        client.set_token("tok_abc")
        with patch.object(client._session, "request",
                          return_value=_err_response(401, "authentication required")):
            with self.assertRaises(ApiError) as ctx:
                client.get("/me")
        self.assertTrue(ctx.exception.unauthorized)
        self.assertIn("Sign in again", ctx.exception.message)
        self.assertFalse(client.authenticated)
        self.assertFalse(self.path.exists())

    def test_404_does_not_clear_the_session(self):
        client = self._client()
        client.set_token("tok_abc")
        with patch.object(client._session, "request",
                          return_value=_err_response(404, "No node with that id.")):
            with self.assertRaises(ApiError) as ctx:
                client.get("/me/nodes/nope")
        self.assertEqual(ctx.exception.status, 404)
        self.assertTrue(client.authenticated)
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "tok_abc",
        )



    def test_other_origin_is_not_restored(self):
        session_store.save(
            "tok_prod", self.path,
            cloud_base="https://api.thetelescope.net",
        )
        client = CloudClient(
            base="https://staging.example",
            session_path=self.path,
        )
        self.assertFalse(client.authenticated)

    def test_http_mode_does_not_restore_or_save(self):
        session_store.save(
            "tok_abc", self.path, cloud_base="https://example.invalid",
        )
        client = CloudClient(
            base="https://example.invalid",
            session_path=self.path,
            persist=False,
        )
        self.assertFalse(client.authenticated)
        client.set_token("tok_new")
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "tok_abc",
        )

    def test_admin_401_does_not_clear_the_session(self):
        client = self._client()
        client.set_token("tok_abc")
        with patch.object(client._session, "request",
                          return_value=_err_response(401, "admin key required")):
            with self.assertRaises(ApiError) as ctx:
                client.get("/admin/integrity", admin=True)
        self.assertTrue(ctx.exception.unauthorized)
        self.assertTrue(client.authenticated)
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "tok_abc",
        )


class AuthStatusTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "mcp_session"
        self.env_patch = patch.dict("os.environ", {}, clear=False)
        self.env = self.env_patch.start()
        self.env.pop("TELESCOPE_MCP_TOKEN", None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def _server(self, client: CloudClient):
        return build_cloud(client)

    def test_unsigned_in_says_to_sign_in(self):
        client = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        ok, text = call(self._server(client), "auth_status")
        self.assertTrue(ok, text)
        self.assertIn("Not signed in", text)
        self.assertNotIn("true", text.split("authenticated")[-1][:40].lower())

    def test_restored_session_reports_signed_in_without_echoing_the_token(self):
        first = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        first.set_token("SECRET-SESSION-TOKEN")
        restarted = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        with patch.object(restarted._session, "request",
                          return_value=_ok_response({"user_id": "user_1"})):
            ok, text = call(self._server(restarted), "auth_status")
        self.assertTrue(ok, text)
        self.assertIn("user_1", text)
        self.assertIn("Signed in", text)
        self.assertNotIn("SECRET-SESSION-TOKEN", text)

    def test_dead_session_asks_them_to_sign_in_again(self):
        client = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        client.set_token("SECRET-SESSION-TOKEN")
        with patch.object(client._session, "request",
                          return_value=_err_response(401, "authentication required")):
            ok, text = call(self._server(client), "auth_status")
        self.assertTrue(ok, text)
        self.assertIn("lost its session", text)
        self.assertNotIn("SECRET-SESSION-TOKEN", text)
        self.assertFalse(client.authenticated)

    def test_auth_login_persists_without_echoing_the_token(self):
        client = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        with patch.object(client._session, "request",
                          return_value=_ok_response({
                              "token": "SECRET-SESSION-TOKEN",
                              "user_id": "user_1",
                          })):
            ok, text = call(self._server(client), "auth_login",
                            {"email": "a@b.c", "password": "hunter2"})
        self.assertTrue(ok, text)
        self.assertNotIn("SECRET-SESSION-TOKEN", text)
        self.assertEqual(
            session_store.load(self.path, expected_base="https://example.invalid"),
            "SECRET-SESSION-TOKEN",
        )
        restarted = CloudClient(
            base="https://example.invalid", session_path=self.path,
        )
        self.assertTrue(restarted.authenticated)


if __name__ == "__main__":
    unittest.main()
