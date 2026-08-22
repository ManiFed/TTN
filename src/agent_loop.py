"""The tool loop that runs on the telescope's own computer.

The model runs in the cloud and the telescope is here, so something local has
to close the loop: send the conversation up, get back a tool call, execute it
against the hardware, send the result up again. That is all this is.

It reuses the MCP tool surface directly rather than spawning a server and
speaking the protocol to ourselves -- the tools are already registered in this
process, and one definition of what a tool does is worth more than protocol
purity between two halves of the same program.

Nothing here holds an API key. Inference and metering both happen in the cloud
(cloud/agent_chat.py), so a member never has to obtain a credential, and
spending is refused before a call rather than discovered after it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

logger = logging.getLogger("src.agent_loop")

#: A single question should not be able to run the telescope all night. Each
#: round trip is a tool call and its answer; a genuine diagnosis takes a
#: handful, and anything past this is a loop rather than a conversation.
MAX_ROUNDS = 12

SYSTEM = """\
You are operating one telescope on behalf of the person talking to you. It is
their instrument, in their garden, and they may not be technical.

How to be useful here:
  - Say what you are about to do before doing it, in one short sentence.
  - Prefer reading before acting. node_status, node_safety and diagnose answer
    most questions without touching anything.
  - When something is wrong, say what is wrong and what you propose, then do it.
    Do not narrate a plan and stop.
  - Real hardware is on the other end. If a tool refuses, that refusal is
    usually correct -- explain it rather than working around it.
  - Research is what the network exists for, but it is their telescope. Nudge,
    never argue.

Answer in plain language. No markdown headings, no bullet lists unless they
genuinely help. Short.
"""


class Conversation:
    """One member's chat with their telescope. Holds history; owns no I/O."""

    def __init__(self, send_to_cloud: Callable[[list, list, str], dict],
                 server, max_rounds: int = MAX_ROUNDS):
        self._send = send_to_cloud
        self._server = server
        self._max_rounds = max_rounds
        self.messages: list = []
        self._tools: Optional[list] = None

    # ── tools ─────────────────────────────────────────────────────────────
    def tools(self) -> list:
        """The tool schemas, built once.

        Cached deliberately: the cloud caches this block with the model, and a
        prefix that changes between turns silently costs ten times as much.
        """
        if self._tools is None:
            listed = asyncio.run(self._server.list_tools())
            self._tools = [{
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.input_schema,
            } for t in sorted(listed, key=lambda t: t.name)]
        return self._tools

    def _run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute one tool. Returns (text, is_error)."""
        try:
            result = asyncio.run(self._server.call_tool(name, args or {}))
        except Exception as exc:
            # A failing tool is information, not a crash: the model can often
            # recover, and the member needs to be told what refused.
            return f"{type(exc).__name__}: {exc}", True
        text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
        failed = getattr(result, "is_error", None)
        if failed is None:
            failed = getattr(result, "isError", False)
        return text or "(no output)", bool(failed)

    # ── the loop ──────────────────────────────────────────────────────────
    def ask(self, question: str) -> dict:
        """Answer one question, running tools as needed."""
        self.messages.append({"role": "user", "content": question})
        used: list = []

        for _ in range(self._max_rounds):
            reply = self._send(self.messages, self.tools(), SYSTEM)
            if reply.get("error"):
                return {"reply": reply["error"], "tools_used": used,
                        "credits": reply.get("credits"),
                        "out_of_credit": bool(reply.get("out_of_credit"))}

            content = reply.get("content") or []
            self.messages.append({"role": "assistant", "content": content})

            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                said = " ".join(b.get("text", "") for b in content
                                if b.get("type") == "text").strip()
                return {"reply": said, "tools_used": used,
                        "credits": reply.get("credits")}

            # All results go back in one user message -- splitting them teaches
            # the model to stop making parallel calls.
            results = []
            for call in calls:
                text, failed = self._run_tool(call.get("name", ""),
                                              call.get("input") or {})
                used.append({"tool": call.get("name"), "ok": not failed})
                results.append({"type": "tool_result",
                                "tool_use_id": call.get("id"),
                                "content": text[:20_000],
                                "is_error": failed})
            self.messages.append({"role": "user", "content": results})

        return {"reply": ("I went round in circles on that one and stopped "
                          "rather than keep going. Ask me again, or ask for "
                          "diagnose to see the current state."),
                "tools_used": used, "hit_round_limit": True}
