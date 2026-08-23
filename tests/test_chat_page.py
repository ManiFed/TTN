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


class NextStepTest(_Base):
    """One place decides what to do now, and every screen displays it.

    Each screen used to end by assuming the member would work out the next move
    themselves. They do not: the person who built this said he barely knew what
    came next. So the node answers that question from its own state, and the
    page leads with the answer rather than a blank prompt.
    """

    def _next(self, telescope=False, camera=False, linked=False):
        from unittest.mock import MagicMock
        cloud = MagicMock() if linked else None
        if cloud is not None:
            cloud.credentials.return_value = ("node_1", "key")
        state = {"telescope": {"connected": telescope},
                 "camera": {"connected": camera}}
        with patch.object(dash, "_cloud", cloud), \
             patch.dict(dash._state, state):
            return self.client.get("/api/chat/next").get_json()

    def test_no_telescope_asks_them_to_connect_one(self):
        step = self._next()
        self.assertEqual(step["state"], "no_telescope")
        self.assertEqual(step["say"], "connect my telescope")

    def test_no_telescope_warns_about_station_mode(self):
        """The most common reason it will not be found, and the telescope's own
        app will not mention it."""
        self.assertIn("Station Mode", self._next()["detail"])

    def test_a_connected_but_unlinked_telescope_asks_for_an_account(self):
        step = self._next(telescope=True)
        self.assertEqual(step["state"], "not_linked")
        self.assertIn("account", step["headline"].lower() + step["detail"].lower())

    def test_a_linked_telescope_with_no_camera_offers_to_diagnose(self):
        step = self._next(telescope=True, linked=True)
        self.assertEqual(step["state"], "no_camera")
        self.assertEqual(step["say"], "is anything wrong?")

    def test_a_working_telescope_says_it_observes_on_its_own(self):
        step = self._next(telescope=True, camera=True, linked=True)
        self.assertEqual(step["state"], "ready")
        self.assertIn("on its own", step["headline"])

    def test_there_is_always_something_to_say(self):
        for kwargs in ({}, {"telescope": True}, {"telescope": True, "linked": True},
                       {"telescope": True, "camera": True, "linked": True}):
            step = self._next(**kwargs)
            self.assertTrue(step["say"], f"no next action for {kwargs}")
            self.assertTrue(step["headline"], f"no headline for {kwargs}")

    def test_a_failure_still_gives_them_something_to_do(self):
        """Never leave the page with nothing to say."""
        with patch.object(dash, "_next_step", side_effect=RuntimeError("boom")):
            step = self.client.get("/api/chat/next").get_json()
        self.assertTrue(step["say"])
        self.assertEqual(step["state"], "unknown")

    def test_installed_assistants_are_reported(self):
        with patch("telescope_mcp.register_client.installed_clients",
                   return_value=["Claude Desktop"]):
            self.assertIn("Claude Desktop", self._next()["assistants"])


class NextStepPageTest(_Base):

    def setUp(self):
        super().setUp()
        self.body = self.client.get("/chat").get_data(as_text=True)

    def test_the_page_leads_with_the_next_step(self):
        self.assertIn('id="next"', self.body)
        self.assertIn("/api/chat/next", self.body)

    def test_the_next_step_is_a_labelled_region(self):
        self.assertIn('aria-labelledby="next-head"', self.body)

    def test_it_offers_a_button_that_says_the_thing(self):
        self.assertIn("next-do", self.body)

    def test_it_refreshes_after_every_turn(self):
        """The answer changes the moment anything actually happens."""
        self.assertGreaterEqual(self.body.count("showNext()"), 2)

    def test_it_tells_them_to_restart_their_assistant(self):
        """Nobody restarts an app because a terminal said so."""
        self.assertIn("only notices new", self.body)
