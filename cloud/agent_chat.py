"""The inference proxy: members talk to their telescope, TTN pays the model.

The node agent runs the tool loop -- it is the thing that can actually reach a
telescope -- but it does not hold an API key. It sends the conversation here,
the cloud calls Anthropic, meters the cost against the member's credit, and
sends the reply back.

Routing it this way rather than putting a key on member machines buys three
things that matter more than the extra hop: the key is in one place and can be
rotated in one place; spending can be refused *before* the call rather than
noticed afterwards; and a member never has to obtain or paste a credential to
use their own telescope.

Prompt caching is not an optimisation here, it is the economics. The tool block
is roughly 12,000 tokens and is re-sent on every turn of every conversation;
cached it costs a tenth of that. The block is therefore cached explicitly and
must stay byte-stable -- any per-request value in the tools or the system
prompt silently turns a tenth back into full price.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from . import credits

logger = logging.getLogger("cloud.agent_chat")

#: Pricing per million tokens, matching the model table. Kept here rather than
#: read from usage because we bill on our own arithmetic, not the provider's.
PRICING = {
    "claude-opus-5":   {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

#: Cache reads cost a tenth of the input rate; writes cost a quarter more.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

DEFAULT_MODEL = "claude-opus-5"

#: Enough for a long diagnosis without being able to run away.
MAX_TOKENS = 8_000


def _api_key(config: dict) -> str:
    return (str((config.get("agent_chat") or {}).get("api_key") or "")
            or os.environ.get("ANTHROPIC_API_KEY", ""))


def _model(config: dict) -> str:
    return str((config.get("agent_chat") or {}).get("model") or DEFAULT_MODEL)


def cost_micros(model: str, usage) -> int:
    """What one call cost us, before markup.

    Cache reads and writes are priced separately from ordinary input; counting
    them at the plain input rate would overcharge by roughly ten times on the
    tool block, which is most of every request.
    """
    price = PRICING.get(model) or PRICING[DEFAULT_MODEL]
    plain = int(getattr(usage, "input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    output = int(getattr(usage, "output_tokens", 0) or 0)

    dollars = (
        plain / 1e6 * price["input"]
        + cache_read / 1e6 * price["input"] * CACHE_READ_MULTIPLIER
        + cache_write / 1e6 * price["input"] * CACHE_WRITE_MULTIPLIER
        + output / 1e6 * price["output"]
    )
    return credits.to_micros(dollars)


def complete(user_id: str, messages: list, tools: list, system: str,
             config: dict) -> dict:
    """One model call on a member's behalf, paid for out of their credit.

    Raises credits.OutOfCredit before spending anything, and RuntimeError if
    the proxy is not configured. Everything else surfaces as a readable
    message rather than an exception, because the far end of this is a member
    trying to use a telescope.
    """
    credits.check_can_spend(user_id)

    key = _api_key(config)
    if not key:
        raise RuntimeError(
            "Telescope chat is not configured on the server "
            "(ANTHROPIC_API_KEY missing).")

    import anthropic
    client = anthropic.Anthropic(api_key=key)
    model = _model(config)

    # The stable prefix -- tools, then system -- is cached. Order matters: the
    # API renders tools before system before messages, and a cache breakpoint
    # only helps what precedes it.
    cached_tools = list(tools)
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1],
                            "cache_control": {"type": "ephemeral"}}

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=cached_tools,
            messages=messages,
            thinking={"type": "adaptive"},
        )
    except anthropic.RateLimitError:
        raise RuntimeError("The model is busy right now. Try again in a moment.")
    except anthropic.APIStatusError as exc:
        logger.warning("Agent chat upstream error %s: %s", exc.status_code, exc)
        raise RuntimeError(
            "The model could not be reached just now. Your telescope is "
            "unaffected and carries on observing.")

    spent = cost_micros(model, response.usage)
    position = credits.record_charge(user_id, spent, {
        "model": model,
        "input_tokens": int(getattr(response.usage, "input_tokens", 0) or 0),
        "cache_read": int(getattr(response.usage, "cache_read_input_tokens", 0) or 0),
        "output_tokens": int(getattr(response.usage, "output_tokens", 0) or 0),
    })

    return {
        "stop_reason": response.stop_reason,
        "content": [_block(b) for b in response.content],
        "credits": position,
        "model": model,
    }


def _block(block) -> dict:
    """Flatten a content block into something the node can act on and re-send."""
    kind = getattr(block, "type", "")
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name,
                "input": block.input}
    if kind == "thinking":
        # Echoed back unchanged on the next turn of the same model; the raw
        # chain of thought is never exposed either way.
        return {"type": "thinking", "thinking": getattr(block, "thinking", ""),
                "signature": getattr(block, "signature", "")}
    return {"type": kind}
