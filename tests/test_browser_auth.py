#!/usr/bin/env python3
"""Signing in without a password ever reaching a tool call.

A chat interface must not take a password as an argument: it lands in the
transcript, the model's context, and whatever logged either, and no later
deletion recalls it. With the app retired there is no other window to type one
into, so the member opens a browser and the agent waits.

That makes the link itself the credential, which is what these tests are about.
It has to be single-use and short-lived, because a link that stays valid is a
password that never expires — and it is the sort of string that ends up in
shell history and screen shares.

Run with:  python3 -m pytest tests/test_browser_auth.py
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cloud import browser_auth


class _FakeDb:
    def __init__(self):
        self.rows: dict = {}

    def execute(self, sql, params=(), returning_id=False):
        if "INSERT INTO auth_browser_sessions" in sql:
            code, status, created, expires = params
            self.rows[code] = {"code": code, "status": status, "user_id": "",
                               "token": "", "created_at": created,
                               "approved_at": "", "expires_at": expires}
        elif "SET status = %s, user_id" in sql:
            status, user_id, token, approved, code, expected = params
            row = self.rows.get(code)
            if row is not None and row["status"] == expected:
                row.update({"status": status, "user_id": user_id,
                            "token": token, "approved_at": approved})
        elif "SET status = %s, token = ''" in sql:
            status, code = params
            if code in self.rows:
                self.rows[code].update({"status": status, "token": ""})
        elif "DELETE FROM auth_browser_sessions" in sql:
            before = len(self.rows)
            self.rows = {k: v for k, v in self.rows.items()
                         if v["created_at"] >= params[0]}
            return before - len(self.rows)
        return 1

    def query_one(self, sql, params=()):
        # A copy, as a real driver returns: handing back the live dict let a
        # later UPDATE mutate a row the caller was still holding, which no
        # database does.
        row = self.rows.get(params[0])
        return dict(row) if row is not None else None


class _Base(unittest.TestCase):
    def setUp(self):
        self.db = _FakeDb()
        patcher = patch.object(browser_auth, "db", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self):
        return browser_auth.start("https://api.example.invalid/")


class LinkTest(_Base):

    def test_the_link_carries_a_code_and_points_at_the_page(self):
        result = self.start()
        self.assertTrue(result["code"])
        self.assertIn("/auth/link?code=", result["url"])
        self.assertIn(result["code"], result["url"])

    def test_the_code_is_long_enough_to_be_the_credential(self):
        """It is the whole secret; a guessable one is an open account."""
        self.assertGreaterEqual(len(self.start()["code"]), 40)

    def test_two_links_never_collide(self):
        self.assertNotEqual(self.start()["code"], self.start()["code"])

    def test_a_link_expires(self):
        result = self.start()
        expires = datetime.fromisoformat(result["expires_at"])
        self.assertLessEqual(
            expires - datetime.now(timezone.utc),
            timedelta(minutes=browser_auth.LINK_TTL_MINUTES + 1))


class FlowTest(_Base):

    def test_the_happy_path_hands_over_a_session(self):
        code = self.start()["code"]
        self.assertEqual(browser_auth.poll(code)["status"], browser_auth.PENDING)
        self.assertTrue(browser_auth.approve(code, "user_1", "SESSION-TOKEN"))
        result = browser_auth.poll(code)
        self.assertEqual(result["status"], browser_auth.APPROVED)
        self.assertEqual(result["token"], "SESSION-TOKEN")
        self.assertEqual(result["user_id"], "user_1")

    def test_a_link_can_only_be_used_once(self):
        """It ends up in shell history and screen shares. Replaying it must
        not produce a second session."""
        code = self.start()["code"]
        browser_auth.approve(code, "user_1", "SESSION-TOKEN")
        self.assertEqual(browser_auth.poll(code)["token"], "SESSION-TOKEN")
        again = browser_auth.poll(code)
        self.assertEqual(again["status"], browser_auth.CONSUMED)
        self.assertNotIn("token", again)

    def test_the_token_is_not_left_in_the_row_after_use(self):
        code = self.start()["code"]
        browser_auth.approve(code, "user_1", "SESSION-TOKEN")
        browser_auth.poll(code)
        self.assertEqual(self.db.rows[code]["token"], "")

    def test_an_unknown_code_is_refused(self):
        self.assertEqual(browser_auth.poll("not-a-real-code")["status"],
                         browser_auth.EXPIRED)

    def test_approving_an_unknown_code_fails(self):
        self.assertFalse(browser_auth.approve("nope", "user_1", "TOKEN"))

    def test_a_link_cannot_be_approved_twice(self):
        """Otherwise a stale tab could overwrite a live session."""
        code = self.start()["code"]
        self.assertTrue(browser_auth.approve(code, "user_1", "FIRST"))
        self.assertFalse(browser_auth.approve(code, "attacker", "SECOND"))
        self.assertEqual(browser_auth.poll(code)["token"], "FIRST")

    def test_an_expired_link_is_refused(self):
        code = self.start()["code"]
        self.db.rows[code]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        result = browser_auth.poll(code)
        self.assertEqual(result["status"], browser_auth.EXPIRED)
        self.assertIn("expired", result["detail"].lower())

    def test_an_expired_link_cannot_be_approved(self):
        code = self.start()["code"]
        self.db.rows[code]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.assertFalse(browser_auth.approve(code, "user_1", "TOKEN"))

    def test_an_already_approved_link_survives_its_own_expiry(self):
        """The member signed in before it lapsed; losing the session because
        the agent polled a second late would be gratuitous."""
        code = self.start()["code"]
        browser_auth.approve(code, "user_1", "TOKEN")
        self.db.rows[code]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.assertEqual(browser_auth.poll(code)["status"], browser_auth.APPROVED)

    def test_a_malformed_expiry_is_treated_as_expired(self):
        code = self.start()["code"]
        self.db.rows[code]["expires_at"] = "whenever"
        self.assertEqual(browser_auth.poll(code)["status"], browser_auth.EXPIRED)


class HousekeepingTest(_Base):

    def test_old_links_are_purged(self):
        code = self.start()["code"]
        self.db.rows[code]["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        browser_auth.purge()
        self.assertNotIn(code, self.db.rows)

    def test_fresh_links_survive_a_purge(self):
        code = self.start()["code"]
        browser_auth.purge()
        self.assertIn(code, self.db.rows)


class EndpointTest(unittest.TestCase):
    """Through the real Flask app."""

    def setUp(self):
        import cloud.server as server
        self.client = server.app.test_client()
        self.db = _FakeDb()
        patcher = patch.object(browser_auth, "db", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_page_is_served_and_offers_both_paths(self):
        resp = self.client.get("/auth/link?code=abc")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Create an account", body)
        self.assertIn("Sign in", body)

    def test_the_page_never_carries_a_token(self):
        body = self.client.get("/auth/link?code=abc").get_data(as_text=True)
        self.assertNotIn("Bearer", body)

    def test_start_then_poll_then_approve_then_poll(self):
        started = self.client.post("/api/v1/auth/browser/start").get_json()
        code = started["code"]
        self.assertEqual(
            self.client.post("/api/v1/auth/browser/poll",
                             json={"code": code}).get_json()["status"],
            browser_auth.PENDING)
        approved = self.client.post(
            "/api/v1/auth/browser/approve",
            json={"code": code, "user_id": "u1", "token": "T"})
        self.assertEqual(approved.status_code, 200)
        done = self.client.post("/api/v1/auth/browser/poll",
                                json={"code": code}).get_json()
        self.assertEqual(done["token"], "T")

    def test_approving_a_dead_link_is_a_400_with_a_readable_reason(self):
        resp = self.client.post("/api/v1/auth/browser/approve",
                                json={"code": "nope", "user_id": "u", "token": "T"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no longer valid", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
