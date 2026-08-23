"""The inference proxy: members talk to their telescope, TTN pays the model.

The node agent runs the tool loop -- it is the thing that can actually reach a
telescope -- but it does not hold an API key. It sends the conversation here,
the cloud calls the model, meters the cost against the member's credit, and
sends the reply back.

Routing it this way rather than putting a key on member machines buys three
things worth more than the extra hop: the key is in one place and rotates in
one place; spending is refused *before* the call rather than noticed after it;
and a member never has to obtain or paste a credential to use their own
telescope.

Provider is OpenRouter, matching cloud/help_chat.py -- one credential to
manage rather than two. It speaks the OpenAI-shaped chat API, so the
translation to and from Anthropic-shaped content blocks lives here, in one
place, and the node's loop stays ignorant of who serves the model.

Prompt caching is the economics, not an optimisation: the tool block is roughly
12,000 tokens re-sent on every turn, and cached it costs a tenth of that. It is
marked for caching and must stay byte-stable. Whether caching actually engages
depends on the provider honouring the hint, so cache_hit_rate is reported back
on every call -- a silent fall to zero is a tenfold bill, and it should be
visible rather than discovered monthly.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import requests

from . import credits

logger = logging.getLogger("cloud.agent_chat")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: Anthropic list pricing per million tokens. We bill on our own arithmetic
#: rather than the provider's reported cost, so this is the source of truth.
PRICING = {
    "anthropic/claude-opus-5":   {"input": 5.00, "output": 25.00},
    "anthropic/claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "anthropic/claude-haiku-4.5": {"input": 1.00, "output": 5.00},
}

CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

DEFAULT_MODEL = "anthropic/claude-opus-5"

#: Enough for a long diagnosis, not enough to run away.
MAX_TOKENS = 8_000

REQUEST_TIMEOUT = 120


def _api_key(config: dict) -> str:
    return (str((config.get("agent_chat") or {}).get("api_key") or "")
            or os.environ.get("OPENROUTER_API_KEY", ""))


def _model(config: dict) -> str:
    return str((config.get("agent_chat") or {}).get("model") or DEFAULT_MODEL)


# ── translation ──────────────────────────────────────────────────────────────

def _tools_to_openai(tools: list) -> list:
    """Anthropic tool schemas -> OpenAI function schemas."""
    return [{
        "type": "function",
        "function": {
            "name": t.get("name"),
            "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object",
                                                    "properties": {}},
        },
    } for t in tools]


def _messages_to_openai(messages: list, system: str) -> list:
    """Anthropic-shaped history -> OpenAI-shaped history.

    The shapes differ in one structural way that matters: Anthropic carries
    tool calls and their results as content blocks inside user/assistant turns,
    while OpenAI splits them into assistant.tool_calls and separate `tool`
    messages. Getting this wrong does not error -- the model simply loses track
    of what it already did and repeats itself.
    """
    out: list = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message.get("role"), message.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        blocks = content or []
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            calls = [{
                "id": b.get("id"),
                "type": "function",
                "function": {"name": b.get("name"),
                             "arguments": json.dumps(b.get("input") or {})},
            } for b in blocks if b.get("type") == "tool_use"]
            entry: dict = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue

        # A user turn carrying tool results becomes one `tool` message each.
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            for block in results:
                out.append({"role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": str(block.get("content") or "")})
            continue

        out.append({"role": "user",
                    "content": "".join(b.get("text", "") for b in blocks)})
    return out


def _reply_to_blocks(message: dict) -> list:
    """OpenAI reply -> Anthropic-shaped blocks, which is what the node expects."""
    blocks: list = []
    text = message.get("content")
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            # A malformed argument blob is the model's mistake, not a crash:
            # pass it through empty and let the tool report what it needed.
            logger.warning("Un-parseable tool arguments for %s",
                           function.get("name"))
            args = {}
        blocks.append({"type": "tool_use", "id": call.get("id"),
                       "name": function.get("name"), "input": args})
    return blocks


# ── cost ─────────────────────────────────────────────────────────────────────

def _usage_counts(usage: dict) -> dict:
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    return {
        "cache_read": cached,
        "plain_input": max(0, prompt - cached),
        "cache_write": int(details.get("cache_creation_tokens") or 0),
        "output": int(usage.get("completion_tokens") or 0),
    }


def cost_micros(model: str, usage: dict) -> int:
    """What one call cost us, before markup.

    Cache reads are a tenth of the input rate. Billing them as fresh input
    would overcharge by roughly ten times, because the cached tool block is
    most of every request.
    """
    price = PRICING.get(model) or PRICING[DEFAULT_MODEL]
    counts = _usage_counts(usage)
    dollars = (
        counts["plain_input"] / 1e6 * price["input"]
        + counts["cache_read"] / 1e6 * price["input"] * CACHE_READ_MULTIPLIER
        + counts["cache_write"] / 1e6 * price["input"] * CACHE_WRITE_MULTIPLIER
        + counts["output"] / 1e6 * price["output"]
    )
    return credits.to_micros(dollars)


# ── the call ─────────────────────────────────────────────────────────────────

def complete(user_id: str, messages: list, tools: list, system: str,
             config: dict) -> dict:
    """One model call on a member's behalf, paid for out of their credit."""
    credits.check_can_spend(user_id)

    key = _api_key(config)
    if not key:
        raise RuntimeError(
            "Telescope chat is not configured on the server "
            "(OPENROUTER_API_KEY missing).")

    model = _model(config)
    openai_tools = _tools_to_openai(tools)
    if openai_tools:
        # Cache the stable prefix. Anthropic models honour this through
        # OpenRouter; if a provider ignores it the call still succeeds, which
        # is why the hit rate is reported rather than assumed.
        openai_tools[-1]["cache_control"] = {"type": "ephemeral"}

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://thetelescope.net",
                "X-Title": "The Telescope Net",
            },
            json={
                "model": model,
                "messages": _messages_to_openai(messages, system),
                "tools": openai_tools,
                "max_tokens": MAX_TOKENS,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.Timeout:
        raise RuntimeError("The model took too long to answer. Your telescope "
                           "is unaffected and carries on observing.")
    except requests.RequestException as exc:
        logger.warning("Agent chat transport failure: %s", exc)
        raise RuntimeError("Could not reach the model just now. Your telescope "
                           "is unaffected and carries on observing.")

    if response.status_code == 429:
        raise RuntimeError("The model is busy right now. Try again in a moment.")
    if response.status_code >= 400:
        logger.warning("Agent chat upstream %s: %s",
                       response.status_code, response.text[:400])
        raise RuntimeError("The model could not be reached just now. Your "
                           "telescope is unaffected and carries on observing.")

    try:
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
    except (ValueError, IndexError, AttributeError):
        raise RuntimeError("The model sent back something unreadable.")

    usage = body.get("usage") or {}
    counts = _usage_counts(usage)
    spent = cost_micros(model, usage)
    position = credits.record_charge(user_id, spent, {
        "model": model, **counts,
    })

    prompt_total = counts["plain_input"] + counts["cache_read"]
    hit_rate = round(counts["cache_read"] / prompt_total, 3) if prompt_total else 0.0
    if prompt_total > 5_000 and hit_rate == 0.0:
        # Ten times the expected bill, and nothing else would say so.
        logger.warning("Prompt cache did not engage on a %d-token prompt "
                       "(model=%s) — cost is ~10x the modelled figure",
                       prompt_total, model)

    return {
        "stop_reason": choice.get("finish_reason"),
        "content": _reply_to_blocks(message),
        "credits": position,
        "model": model,
        "cache_hit_rate": hit_rate,
    }
