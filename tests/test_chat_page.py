#!/usr/bin/env python3
"""The chat page: the whole product for a member with no assistant installed.

With the app retired this is the only interface some people will ever see, so
the tests are about the two things that decide whether it is usable rather than
merely present: that a screen reader can follow a conversation arriving in
pieces, and that every failure says the telescope is still observing.

That last one matters more than it looks. Every error here is about the chat --
no credit, no network, node stopped -- and none of them stop the telescope. A
member who reads "something went wrong" and believes their night is lost will
go and power-cycle a working instrument.

Run with:  python3 -m pytest tests/test_chat_page.py
"""

import unittest
from unittest.mock import patch

import src.dashboard as dash


class _Convo:
    def __init__(self, result):
        self.result = result
        self.asked = []

    def ask(self, question):
        self.asked.append(question)
        return self.result


class _Base(unittest.TestCase):
    def setUp(self):
        self.client = dash.app.test_client()

    def with_convo(self, result):
        convo = _Convo(result)
        p = patch.object(dash, "_chat_conversation", lambda: convo)
        p.start()
        self.addCleanup(p.stop)
        return convo


class PageTest(_Base):

    def setUp(self):
        super().setUp()
        self.body = self.client.get("/chat").get_data(as_text=True)

    def test_the_page_is_served(self):
        self.assertEqual(self.client.get("/chat").status_code, 200)

    def test_the_conversation_is_a_live_region(self):
        """Replies arrive after the page has loaded. Without this a screen
        reader announces nothing at all."""
        self.assertIn('role="log"', self.body)
        self.assertIn('aria-live="polite"', self.body)

    def test_the_input_has_a_real_label(self):
        """A placeholder is not a label -- it disappears on focus."""
        self.assertIn('<label class="sr" for="q"', self.body)
        self.assertIn('id="q"', self.body)

    def test_the_page_declares_a_language(self):
        self.assertIn('<html lang="en"', self.body)

    def test_focus_is_visible_for_keyboard_users(self):
        self.assertIn("focus-visible", self.body)

    def test_animation_is_dropped_when_asked(self):
        self.assertIn("prefers-reduced-motion", self.body)

    def test_it_suggests_what_to_say(self):
        """Nobody knows what to type at a blank telescope prompt."""
        self.assertIn("connect my telescope", self.body)

    def test_nothing_is_loaded_from_the_internet(self):
        """The node may be on a garden Wi-Fi that cannot reach anything."""
        for marker in ("https://fonts.", "cdn.", "<script src="):
            self.assertNotIn(marker, self.body)


class TurnTest(_Base):

    def test_a_question_is_answered(self):
        convo = self.with_convo({"reply": "All fine.", "tools_used": []})
        body = self.client.post("/api/chat",
                                json={"message": "how is it?"}).get_json()
        self.assertEqual(body["reply"], "All fine.")
        self.assertEqual(convo.asked, ["how is it?"])

    def test_tools_used_come_back_for_display(self):
        self.with_convo({"reply": "ok", "tools_used": [
            {"tool": "node_status", "ok": True},
            {"tool": "node_slew", "ok": False}]})
        body = self.client.post("/api/chat", json={"message": "x"}).get_json()
        self.assertEqual(len(body["tools_used"]), 2)
        self.assertFalse(body["tools_used"][1]["ok"])

    def test_an_empty_message_is_refused(self):
        self.assertEqual(
            self.client.post("/api/chat", json={"message": "   "}).status_code, 400)

    def test_an_enormous_message_is_refused(self):
        resp = self.client.post("/api/chat", json={"message": "x" * 5000})
        self.assertEqual(resp.status_code, 400)

    def test_running_out_of_credit_is_answered_not_errored(self):
        """A 500 here would look like the telescope broke."""
        self.with_convo({"reply": "You are out of credit.",
                         "out_of_credit": True, "tools_used": []})
        resp = self.client.post("/api/chat", json={"message": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["out_of_credit"])


class FailureTest(_Base):
    """Every failure here is about the chat, and none of them stop observing."""

    def test_a_crash_answers_rather_than_500s(self):
        p = patch.object(dash, "_chat_conversation",
                         side_effect=RuntimeError("boom"))
        p.start()
        self.addCleanup(p.stop)
        resp = self.client.post("/api/chat", json={"message": "x"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("carries on observing", resp.get_json()["reply"])

    def test_the_page_says_so_when_it_cannot_reach_the_node(self):
        body = self.client.get("/chat").get_data(as_text=True)
        self.assertIn("telescope itself is unaffected", body)

    def test_the_out_of_credit_note_says_observing_continues(self):
        body = self.client.get("/chat").get_data(as_text=True)
        self.assertIn("keeps observing either way", body)

    def test_credits_are_absent_rather_than_broken_when_unlinked(self):
        """An unlinked node answers {}; rendering that as $NaN is what a
        member would actually have seen."""
        with patch.object(dash, "_cloud", None):
            self.assertEqual(self.client.get("/api/chat/credits").get_json(), {})
        body = self.client.get("/chat").get_data(as_text=True)
        self.assertIn("typeof c.balance !== 'number'", body,
                      "the page must guard against a credits object with no "
                      "balance, not merely against a missing one")


class ResetTest(_Base):

    def test_the_conversation_can_be_started_again(self):
        """Long histories cost more per turn and drift off topic."""
        dash._chat_convo = object()
        self.assertEqual(self.client.post("/api/chat/reset").status_code, 200)
        self.assertIsNone(dash._chat_convo)


if __name__ == "__main__":
    unittest.main()
