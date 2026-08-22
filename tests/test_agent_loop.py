#!/usr/bin/env python3
"""The tool loop that runs on the telescope's computer.

It sits between a model in the cloud and real hardware in someone's garden, so
the behaviours worth pinning are the ones that bite when nobody is watching: it
must stop rather than spin, a failing tool must reach the model as information
instead of ending the conversation, and the tool block must be byte-stable
between turns -- the cloud caches it, and a prefix that changes costs ten times
as much without anything visibly breaking.

Run with:  python3 -m pytest tests/test_agent_loop.py
"""

import unittest
from unittest.mock import MagicMock

from src.agent_loop import MAX_ROUNDS, Conversation


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"does {name}"
        self.input_schema = {"type": "object", "properties": {}}


class _Result:
    def __init__(self, text, is_error=False):
        self.content = [MagicMock(text=text)]
        self.is_error = is_error


def _server(tools=("node_status", "node_slew"), result=None, raises=None):
    server = MagicMock()

    async def list_tools():
        return [_Tool(n) for n in tools]

    async def call_tool(name, args):
        if raises:
            raise raises
        return result or _Result("ok")

    server.list_tools = list_tools
    server.call_tool = call_tool
    return server


def _says(text):
    return {"content": [{"type": "text", "text": text}], "credits": {"balance": 1.0}}


def _calls(name, args=None, call_id="t1"):
    return {"content": [{"type": "tool_use", "id": call_id,
                         "name": name, "input": args or {}}],
            "credits": {"balance": 1.0}}


class AnswerTest(unittest.TestCase):

    def test_a_plain_answer_needs_no_tools(self):
        convo = Conversation(lambda m, t, s: _says("Two hours tonight."), _server())
        result = convo.ask("what's the plan?")
        self.assertEqual(result["reply"], "Two hours tonight.")
        self.assertEqual(result["tools_used"], [])

    def test_a_tool_call_is_executed_and_answered(self):
        replies = [_calls("node_status"), _says("It is connected and safe.")]
        convo = Conversation(lambda m, t, s: replies.pop(0), _server())
        result = convo.ask("is it working?")
        self.assertEqual(result["reply"], "It is connected and safe.")
        self.assertEqual(result["tools_used"], [{"tool": "node_status", "ok": True}])

    def test_the_result_goes_back_as_a_tool_result(self):
        replies = [_calls("node_status"), _says("done")]
        convo = Conversation(lambda m, t, s: replies.pop(0), _server())
        convo.ask("check")
        results = [m for m in convo.messages
                   if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertTrue(results)
        block = results[0]["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "t1")

    def test_parallel_calls_come_back_in_one_message(self):
        """Splitting them teaches the model to stop calling tools in parallel."""
        both = {"content": [
            {"type": "tool_use", "id": "a", "name": "node_status", "input": {}},
            {"type": "tool_use", "id": "b", "name": "node_safety", "input": {}},
        ], "credits": {}}
        replies = [both, _says("both fine")]
        convo = Conversation(lambda m, t, s: replies.pop(0), _server())
        convo.ask("check both")
        results = [m for m in convo.messages
                   if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["content"]), 2)


class FailureTest(unittest.TestCase):

    def test_a_failing_tool_is_reported_to_the_model_not_raised(self):
        """The model can usually recover, and the member needs to be told."""
        replies = [_calls("node_slew"), _says("The mount refused; it is parked.")]
        convo = Conversation(lambda m, t, s: replies.pop(0),
                             _server(result=_Result("below horizon", is_error=True)))
        result = convo.ask("point at M42")
        self.assertEqual(result["reply"], "The mount refused; it is parked.")
        self.assertEqual(result["tools_used"], [{"tool": "node_slew", "ok": False}])

    def test_a_tool_that_raises_does_not_end_the_conversation(self):
        replies = [_calls("node_slew"), _says("I could not reach the mount.")]
        convo = Conversation(lambda m, t, s: replies.pop(0),
                             _server(raises=RuntimeError("connection reset")))
        result = convo.ask("point at M42")
        self.assertIn("could not reach", result["reply"])
        blocks = [m for m in convo.messages
                  if m["role"] == "user" and isinstance(m["content"], list)]
        self.assertIn("connection reset", blocks[0]["content"][0]["content"])
        self.assertTrue(blocks[0]["content"][0]["is_error"])

    def test_running_out_of_credit_is_surfaced_plainly(self):
        convo = Conversation(
            lambda m, t, s: {"error": "You are out of credit.",
                             "out_of_credit": True, "credits": {"balance": 0.0}},
            _server())
        result = convo.ask("anything")
        self.assertTrue(result["out_of_credit"])
        self.assertIn("out of credit", result["reply"])

    def test_it_stops_rather_than_looping_for_ever(self):
        """A model that keeps calling tools must not run the telescope all night."""
        convo = Conversation(lambda m, t, s: _calls("node_status"), _server())
        result = convo.ask("go")
        self.assertTrue(result["hit_round_limit"])
        self.assertEqual(len(result["tools_used"]), MAX_ROUNDS)

    def test_a_huge_tool_output_is_truncated(self):
        replies = [_calls("node_logs"), _says("ok")]
        convo = Conversation(lambda m, t, s: replies.pop(0),
                             _server(result=_Result("x" * 100_000)))
        convo.ask("logs")
        block = [m for m in convo.messages
                 if m["role"] == "user" and isinstance(m["content"], list)][0]
        self.assertLessEqual(len(block["content"][0]["content"]), 20_000)


class ToolBlockTest(unittest.TestCase):
    """The cloud caches this block. Instability costs money silently."""

    def test_the_tool_block_is_identical_between_turns(self):
        convo = Conversation(lambda m, t, s: _says("hi"), _server())
        first = convo.tools()
        convo.ask("one")
        convo.ask("two")
        self.assertIs(convo.tools(), first, "rebuilt between turns")

    def test_tools_are_ordered_deterministically(self):
        convo = Conversation(lambda m, t, s: _says("hi"),
                             _server(tools=("zulu", "alpha", "mike")))
        names = [t["name"] for t in convo.tools()]
        self.assertEqual(names, sorted(names))

    def test_the_schema_is_what_the_api_expects(self):
        convo = Conversation(lambda m, t, s: _says("hi"), _server())
        for tool in convo.tools():
            self.assertEqual(set(tool), {"name", "description", "input_schema"})


if __name__ == "__main__":
    unittest.main()
